"""개발용 v2 로그에서 법령 검색어 재작성(최근 발화만, 생성형 LLM 재작성)을 오프라인 비교한다.

엔진과 고정 설정은 바꾸지 않는다. 비교 방식은 jikgam/v2/law_variants.py와 같다(근거를 고른 검색 이벤트 × 정답 법령 항목마다 상위
k개 구간이 그 항목을 덮는지). 유료 호출은 f·g의 재작성뿐이다. 기준과 합격선은 docs/v2-law-query-rewrite.md에 실행 전에 적었다.

- a 지금 그대로: 고정 규칙(전체 고객 발화 + 의도·표현 확장)
- e1, e2: 같은 규칙에서 고객 발화를 최근 1개, 2개만 쓴다
- f: 엔진이 쓰는 생성형 LLM(config/decision_models.toml의 gemini-flash)이 그 시점까지의 대화를 보고 독립된 검색 문장 하나로 다시 쓴다.
  호출이 실패하면 a를 쓰고 그 수를 센다
- g: b(적용 문서 제한, law_variants와 같은 목록) + f

    uv run --group embed python -m jikgam.v2.law_rewrite <run_dir...> --documents ... --estimate   # 호출 없이 예상 비용
    uv run --group embed python -m jikgam.v2.law_rewrite <run_dir...> --documents ...              # 실행
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT, load_org
from jikgam.engine.retrieval import ArticleIndex, RetrievalContext
from jikgam.sim.metrics import percentile_inf
from jikgam.sim.scenario import load_scenarios
from jikgam.v2.e2e_logs import read_log
from jikgam.v2.e2e_queries import found
from jikgam.v2.law_variants import SCENARIOS, frozen_query, in_scope, ranking

VARIANTS = ("a", "e1", "e2", "f", "g")
KS = (8, 16)
GENERATOR = "gemini-flash"
# 재작성 지시. 법령 이름, 번호 체계, 문서 형식에 기대는 말은 넣지 않는다(실행 전에 고정, docs/v2-law-query-rewrite.md)
PROMPT = ("다음은 고객센터 상담 대화다. 이 대화에서 고객의 상황과 요청을, 관련 규정을 찾는 데 쓸 독립된 검색 문장 하나로 다시 써라.\n"
          "- 고객이 말한 구체적인 단서(상품이나 서비스, 기간과 날짜, 금액, 결제 방법, 상황)는 빠뜨리지 말고 남긴다.\n"
          "- 대화에 없는 사실은 더하지 않는다. 고객 이름, 전화번호, 인증번호 같은 개인 정보는 넣지 않는다.\n"
          "- 설명 없이 검색 문장 한 줄만 답한다.")
# 예상 비용 계산의 가정(실행 전에 고정): 입력은 글자 1개를 토큰 1개로, 출력은 사고 토큰을 넣어 호출당 600토큰으로 넉넉히 잡는다
EST_TOKENS_PER_CHAR = 1.0
EST_OUTPUT_TOKENS = 600


def recent_query(org, customer: list[str], intent_label: str | None, turns: int) -> str:
    """고정 규칙에서 고객 발화를 최근 turns개만 쓴 검색어."""
    dummy = SimpleNamespace(cfg=replace(org.retrieval, query_turns=turns))
    return ArticleIndex.query_text(dummy, RetrievalContext(tuple(customer), intent=intent_label))


def dialogue(turns: list[tuple[str, str]]) -> str:
    return "\n".join(f"{'고객' if who == 'customer' else '상담사'}: {text}" for who, text in turns)


def events(run_dirs: list[Path]):
    """근거를 고른 검색 이벤트마다 (실행 정보, 정답, 그 시점의 고객 발화, 대화, 직전 의도)를 낸다."""
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / SCENARIOS)}
    for directory in run_dirs:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if summary.get("scenario_file") != SCENARIOS or summary["law"] not in ("r0", "r4"):
            raise ValueError("개발용 v2 시나리오의 R0·R4 실행만 지원합니다")
        for sess in summary["sessions"]:
            gold = scenarios[sess["scenario"]]["expected"].get("law_evidence") or []
            if not gold:
                continue
            es = read_log(directory / f"{sess['session']}.jsonl")
            evidence_versions = {e["version"] for e in es if e["type"] == "evidence.updated"}
            texts = {e["suggestion_id"]: e.get("text", "") for e in es if e["type"] == "suggestion.created"}
            customer: list[str] = []
            turns: list[tuple[str, str]] = []
            intent_label = None
            for e in es:
                if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
                    customer.append(e["text"])
                    turns.append(("customer", e["text"]))
                if e["type"] == "suggestion.exposed" and e.get("suggestion_id") in texts:
                    turns.append(("agent", texts[e["suggestion_id"]]))
                if e["type"] == "evidence.updated":
                    intent_label = e.get("intent")
                if e["type"] == "retrieval.done" and e["version"] in evidence_versions:
                    yield {"profile": summary["law"], "run": summary["run_id"], "scenario": sess["scenario"],
                           "session": sess["session"], "version": e["version"], "gold": gold, "customer": list(customer),
                           "dialogue": dialogue(turns), "intent": intent_label, "logged_query": e.get("law_query"),
                           "logged_candidates": e.get("law_candidates")}


class Rewriter:
    """엔진 생성기 설정(모델, 키, 사고 수준, 단가)으로 재작성 한 번을 부른다. 걸린 시간은 HTTP 요청 하나다(엔진과 같다)."""

    def __init__(self):
        from jikgam.decision.registry import load_config, secrets

        cfg = load_config()[GENERATOR]
        self.model = cfg["model"]
        self.thinking = cfg.get("thinking_level")
        self.price_in, self.price_out = cfg["price_input_per_mtok"], cfg["price_output_per_mtok"]
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": secrets()[cfg["api_key_env"]]}, timeout=60.0)

    async def __call__(self, text: str) -> dict:
        gen = {"thinkingConfig": {"thinkingLevel": self.thinking}} if self.thinking else {}
        body = {"systemInstruction": {"parts": [{"text": PROMPT}]}, "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": gen}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            return {"query": None, "latency_ms": (time.perf_counter() - t0) * 1000, "error": f"{type(e).__name__}: {e}"}
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return {"query": None, "latency_ms": latency, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        total = usage.get("totalTokenCount")
        tout = int(total) - tin if total is not None else int(usage.get("candidatesTokenCount", 0))
        try:
            out = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            out = ""
        query = " ".join(out.split()) or None
        return {"query": query, "latency_ms": latency, "tokens_in": tin, "tokens_out": tout, "model": data.get("modelVersion"),
                "cost_usd": (tin * self.price_in + tout * self.price_out) / 1e6, "error": None if query else "빈 응답"}

    async def aclose(self) -> None:
        await self.http.aclose()


def estimate(rows: list[dict]) -> dict:
    from jikgam.decision.registry import load_config

    cfg = load_config()[GENERATOR]
    unique = {r["dialogue"] for r in rows}
    chars = sum(len(PROMPT) + len(d) for d in unique)
    tin = chars * EST_TOKENS_PER_CHAR
    tout = len(unique) * EST_OUTPUT_TOKENS
    return {"calls": len(unique), "input_chars": chars, "est_tokens_in": round(tin), "est_tokens_out": tout,
            "est_cost_usd": round((tin * cfg["price_input_per_mtok"] + tout * cfg["price_output_per_mtok"]) / 1e6, 3),
            "model": cfg["model"], "thinking_level": cfg.get("thinking_level")}


async def rewrite_all(rows: list[dict], cache_path: Path) -> dict[str, dict]:
    """대화마다 한 번씩, 하나씩 차례로 부른다(지연을 다른 호출과 겹치지 않게 잰다). 받은 것은 저장소 밖에 모아 두고 다시 쓴다."""
    cache: dict[str, dict] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            cache[row["key"]] = row
    rw = Rewriter()
    try:
        with cache_path.open("a", encoding="utf-8", newline="\n") as out:
            todo = sorted({r["dialogue"] for r in rows})
            for i, d in enumerate(todo, 1):
                key = hashlib.sha256(d.encode("utf-8")).hexdigest()
                if key in cache and cache[key].get("query"):
                    continue
                res = await rw(d)
                res["key"] = key
                cache[key] = res
                out.write(json.dumps(res, ensure_ascii=False) + "\n")
                out.flush()
                if i % 50 == 0:
                    print(f"재작성 {i}/{len(todo)}", flush=True)
    finally:
        await rw.aclose()
    return {d: cache[hashlib.sha256(d.encode("utf-8")).hexdigest()] for d in {r["dialogue"] for r in rows}}


def run(run_dirs: list[Path], documents: frozenset[str], name: str, only_estimate: bool) -> dict:
    rows = list(events(run_dirs))
    est = estimate(rows)
    if only_estimate:
        return {"estimate": est, "events": {p: sum(r["profile"] == p for r in rows) for p in ("r0", "r4")}}
    local = ROOT / "data/v2/local/law-rewrite" / name
    local.mkdir(parents=True, exist_ok=True)
    rewrites = asyncio.run(rewrite_all(rows, local / "rewrites.jsonl"))
    org = load_org("org_a")
    report: dict = {"documents": sorted(documents), "runs": [d.as_posix() for d in run_dirs], "estimate": est,
                    "prompt": PROMPT, "generator": GENERATOR, "profiles": {}}
    laws = {p: bundle(load_law(p)) for p in ("r0", "r4")}
    caches: dict[str, dict[str, list[str]]] = {"r0": {}, "r4": {}}
    sessions: dict = {}
    with (local / "trace.jsonl").open("w", encoding="utf-8", newline="\n") as trace:
        for r in rows:
            p, law, cache = r["profile"], laws[r["profile"]], caches[r["profile"]]
            prof = report["profiles"].setdefault(p, {
                "events": 0, "rewrite_failed": 0, "logged_query_checked": 0, "logged_query_same": 0, "logged_top8_same": 0,
                "variants": {v: {"items": 0, **{f"found@{k}": 0 for k in KS}} for v in VARIANTS}})

            def ranked(q: str) -> list[str]:
                if q not in cache:
                    cache[q] = ranking(law, q)
                return cache[q]

            base = frozen_query(org, r["customer"], r["intent"])
            if r["run"].endswith("-3"):
                prof["logged_query_checked"] += 1
                prof["logged_query_same"] += base == r["logged_query"]
                prof["logged_top8_same"] += ranked(base)[:8] == r["logged_candidates"]
            rw = rewrites[r["dialogue"]]
            if not rw.get("query"):
                prof["rewrite_failed"] += 1
            fq = rw.get("query") or base
            queries = {"a": base, "e1": recent_query(org, r["customer"], r["intent"], 1),
                       "e2": recent_query(org, r["customer"], r["intent"], 2), "f": fq, "g": fq}
            prof["events"] += 1
            sess = sessions.setdefault((p, r["session"]), {"gold": r["gold"], **{v: {k: set() for k in KS} for v in VARIANTS}})
            row = {"profile": p, "run": r["run"], "scenario": r["scenario"], "version": r["version"], "queries": queries,
                   "found": {}}
            for v in VARIANTS:
                ids = ranked(queries[v])
                if v == "g":
                    ids = in_scope(law, ids, documents)
                stat = prof["variants"][v]
                stat["items"] += len(r["gold"])
                row["found"][v] = {}
                for k in KS:
                    hits = found(r["gold"], ids[:k])
                    stat[f"found@{k}"] += sum(hits)
                    row["found"][v][k] = hits
                    sess[v][k].update(ids[:k])
            trace.write(json.dumps(row, ensure_ascii=False) + "\n")
    for (p, _), sess in sessions.items():
        block = report["profiles"][p].setdefault("sessions", {v: {"items": 0, **{f"found@{k}": 0 for k in KS}} for v in VARIANTS})
        for v in VARIANTS:
            block[v]["items"] += len(sess["gold"])
            for k in KS:
                block[v][f"found@{k}"] += sum(found(sess["gold"], sorted(sess[v][k])))
    calls = [x for x in rewrites.values() if x.get("query")]
    lat = [x["latency_ms"] for x in calls]
    report["rewrite"] = {"calls": len(rewrites), "ok": len(calls), "failed": len(rewrites) - len(calls),
                         "latency_ms": {"p50": round(percentile_inf(lat, 50), 1), "p95": round(percentile_inf(lat, 95), 1),
                                        "max": round(max(lat), 1)} if lat else None,
                         "tokens_in": sum(x.get("tokens_in", 0) for x in calls),
                         "tokens_out": sum(x.get("tokens_out", 0) for x in calls),
                         "cost_usd": round(sum(x.get("cost_usd", 0.0) for x in rewrites.values()), 4),
                         "models": sorted({x.get("model") for x in calls if x.get("model")})}
    for prof in report["profiles"].values():
        for block in (prof["variants"], prof["sessions"]):
            for stat in block.values():
                for k in KS:
                    stat[f"rate@{k}"] = round(stat[f"found@{k}"] / stat["items"], 4) if stat["items"] else None
    dest = ROOT / "results/v2/law-rewrite" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--documents", required=True, help="조직에 적용되는 문서 id(쉼표로 구분, g에 쓴다)")
    ap.add_argument("--name", default="dev-v2")
    ap.add_argument("--estimate", action="store_true", help="호출하지 않고 예상 비용만 낸다")
    args = ap.parse_args()
    out = run(args.run_dirs, frozenset(d.strip() for d in args.documents.split(",") if d.strip()), args.name, args.estimate)
    if args.estimate:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(json.dumps({"rewrite": out["rewrite"], **{p: {v: (s["rate@8"], s["rate@16"]) for v, s in prof["variants"].items()}
                                                    for p, prof in out["profiles"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
