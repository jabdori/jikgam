"""개발용 v2 로그에서 정해진 메타데이터로 검색 전에 법령 후보 범위를 거르는 구성을 오프라인 비교한다.

엔진과 고정 설정은 바꾸지 않는다. 측정 단위는 jikgam/v2/law_variants.py와 같다: 엔진이 실제로 법령 검색을 한 시점(근거를 고른
retrieval.done)마다 그때까지의 대화로 R4 검색을 다시 돌려, 상위 8개(와 16위 안) 구간이 정답 법령 항목을 덮는지 본다. 기준과
합격선, 값 목록은 docs/v2-law-meta.md에 실행 전에 적었다. 유료 호출은 메타데이터 붙이기(생성형 LLM)와 Jev뿐이다.

메타데이터(구간마다, 정해진 값 목록 안에서만): 문서 종류, 적용 업종, 주제.
- 문서 종류: 법제처 국가법령정보 오픈 API의 공식 분류(법령의 법종구분, 행정규칙의 행정규칙종류)를 값 목록에 맞춘다. 표준약관처럼
  API에 없는 문서는 생성형 LLM이 목록 안에서 고른다. 소관 부처도 함께 받아 둔다(거르기에는 쓰지 않는다)
- 적용 업종, 주제: 생성형 LLM(엔진의 gemini-flash)이 문서 제목, 기존 구간 정보, 구간 본문을 보고 목록 안에서 여러 개를 고른다

구성(남은 구간 안의 순위는 지금 R4 검색 순위에서 남은 구간만 순서대로 남긴 것):
- m1 회사 설정: 조직의 적용 업종(설정 값)에 해당하는 구간만 남긴다
- m2 Jev 주제: 검색 시점마다 "지금 대화의 주제는 목록 중 무엇인가"를 Jev 한 번(객관식)으로 묻고, 고른 주제의 확률이
  JEV_THRESHOLD 이상이면 그 주제 구간만 남긴다. 미만이면 거르지 않는다
- m3 = m1 + m2
거를 때도 상위 8개 중 RESERVED 자리는 거르지 않은 지금 검색 순위로 채운다: 목록 = 거른 순위 앞 (8 − RESERVED)개 + 거르지 않은
순위에서 아직 없는 앞 RESERVED개 + 나머지 거른 순위 + 나머지 거르지 않은 순위.
"""

import argparse
import asyncio
import hashlib
import json
import random
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

import httpx

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT
from jikgam.sim.metrics import percentile_inf
from jikgam.v2.e2e_queries import found
from jikgam.v2.law_link import Calls, events
from jikgam.v2.law_variants import ranking

KS = (8, 16)
CONFIGS = ("a", "m1", "m2", "m3")
RESERVED = 2
JEV_THRESHOLD = 0.5
GENERATOR = "gemini-flash"
SAMPLE_SEED = 20261019
SAMPLE_SIZE = 20
EST_TOKENS_PER_CHAR = 1.0
EST_LABEL_OUTPUT_TOKENS = 600
FIELDS = ("문서 종류", "적용 업종", "주제")
# 법제처 공식 분류(법종구분·행정규칙종류) → 문서 종류 값. 공포 형식의 일반 이름을 맞춘다(실행 전에 고정)
OFFICIAL_TYPE = {"법률": "법률", "대통령령": "시행령", "총리령": "시행규칙", "부령": "시행규칙", "고시": "고시"}
LABEL_PROMPT = ("다음은 법령·고시·표준약관 문서의 한 구간이다. 정해진 값 목록 안에서만 골라 이 구간에 메타데이터를 붙인다.\n"
                "- 적용 업종: 이 구간의 내용이 적용되는 업종을 모두 고른다. 업종을 가리지 않고 적용되면 업종 공통을 고른다.\n"
                "- 주제: 이 구간이 다루는 주제를 모두 고른다. 가장 중심인 것부터 쓴다.\n"
                "- 문서 종류를 묻는 경우에만 문서 종류 하나를 고른다.\n"
                "- 각 값의 정의를 따르고, 목록에 없는 값은 쓰지 않는다.")
TOPIC_TASK = "고객센터 상담 대화의 주제를 고른다. 주제 목록의 정의를 보고, 지금 대화가 다루는 주제 하나를 고른다."


# --- 공식 분류(법제처 오픈 API) ------------------------------------------------------------------------


def official_classification() -> dict[str, dict]:
    """config/v2/sources.toml의 법령(law)·행정규칙(admrul) 문서마다 공식 종류와 소관 부처. 키는 .env의 LAW_API_KEY."""
    from jikgam.decision.registry import secrets

    oc = secrets()["LAW_API_KEY"]
    sources = tomllib.loads((ROOT / "config/v2/sources.toml").read_text(encoding="utf-8"))["doc"]
    out = {}
    for s in sources:
        if s.get("site") not in ("law", "admrul"):
            continue
        params = {"OC": oc, "type": "JSON", "target": s["site"], ("MST" if s["site"] == "law" else "ID"): str(s["seq"])}
        url = "https://www.law.go.kr/DRF/lawService.do?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        if s["site"] == "law":
            info = data["법령"]["기본정보"]
            out[s["id"]] = {"name": info["법령명_한글"], "kind": info["법종구분"]["content"], "ministry": info["소관부처"]["content"],
                            "source": "법제처 오픈 API(lawService, target=law)"}
        else:
            info = data["AdmRulService"]["행정규칙기본정보"]
            out[s["id"]] = {"name": info["행정규칙명"], "kind": info["행정규칙종류"], "ministry": info["소관부처명"],
                            "source": "법제처 오픈 API(lawService, target=admrul)"}
    return out


# --- 메타데이터 붙이기(생성형 LLM) ---------------------------------------------------------------------


def label_schema(values: dict, with_type: bool) -> dict:
    props = {"적용 업종": {"type": "array", "items": {"type": "string", "enum": [v["value"] for v in values["적용 업종"]]}},
             "주제": {"type": "array", "items": {"type": "string", "enum": [v["value"] for v in values["주제"]]}}}
    req = ["적용 업종", "주제"]
    if with_type:
        props["문서 종류"] = {"type": "string", "enum": [v["value"] for v in values["문서 종류"]]}
        req.append("문서 종류")
    return {"type": "object", "properties": props, "required": req}


def label_input(values: dict, doc_title: str, info: str, text: str, with_type: bool) -> str:
    lists = []
    for f in FIELDS:
        if f == "문서 종류" and not with_type:
            continue
        lists.append(f"[{f}]\n" + "\n".join(f"- {v['value']}: {v['definition']}" for v in values[f]))
    return (f"문서: {doc_title}\n\n구간 정보:\n{info or '(없음)'}\n\n구간 본문:\n{text}\n\n값 목록:\n" + "\n\n".join(lists))


class Labeler:
    """엔진 생성기 설정(모델, 키, 사고 수준, 단가)으로 구간 하나에 값을 붙인다. 응답은 값 목록을 enum으로 둔 JSON 스키마로 받는다."""

    def __init__(self):
        from jikgam.decision.registry import load_config, secrets

        cfg = load_config()[GENERATOR]
        self.model, self.thinking = cfg["model"], cfg.get("thinking_level")
        self.price_in, self.price_out = cfg["price_input_per_mtok"], cfg["price_output_per_mtok"]
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": secrets()[cfg["api_key_env"]]}, timeout=90.0)

    async def __call__(self, text: str, schema: dict) -> dict:
        gen = {"responseMimeType": "application/json", "responseJsonSchema": schema}
        if self.thinking:
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking}
        body = {"systemInstruction": {"parts": [{"text": LABEL_PROMPT}]}, "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": gen}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            return {"labels": None, "error": f"{type(e).__name__}: {e}", "latency_ms": (time.perf_counter() - t0) * 1000}
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return {"labels": None, "error": f"HTTP {r.status_code}: {r.text[:200]}", "latency_ms": latency}
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        total = usage.get("totalTokenCount")
        tout = int(total) - tin if total is not None else int(usage.get("candidatesTokenCount", 0))
        try:
            out = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
            labels = json.loads(out)
        except (KeyError, IndexError, ValueError, TypeError):
            labels = None
        return {"labels": labels, "error": None if labels else "출력 해석 실패", "latency_ms": latency, "tokens_in": tin,
                "tokens_out": tout, "cost_usd": (tin * self.price_in + tout * self.price_out) / 1e6, "model": data.get("modelVersion")}

    async def aclose(self):
        await self.http.aclose()


def valid(labels: dict | None, values: dict, with_type: bool) -> bool:
    if not isinstance(labels, dict):
        return False
    allowed = {f: {v["value"] for v in values[f]} for f in FIELDS}
    ok = all(isinstance(labels.get(f), list) and labels[f] and set(labels[f]) <= allowed[f] for f in ("적용 업종", "주제"))
    return ok and (not with_type or labels.get("문서 종류") in allowed["문서 종류"])


async def label_all(jobs: list[tuple[str, str, dict, bool]], calls: Calls, values: dict, concurrency: int = 4) -> float:
    lab = Labeler()
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()

    async def one(key, text, schema, with_type):
        async with sem:
            for _ in range(2):  # 목록 밖 값이나 해석 실패면 한 번 더 묻는다
                res = await lab(text, schema)
                if valid(res.get("labels"), values, with_type):
                    break
            else:
                res["error"] = res.get("error") or "목록 밖 값"
            calls.add({"key": key, "kind": "label", **res})

    try:
        await asyncio.gather(*[one(*j) for j in jobs if not calls.done(j[0])])
    finally:
        await lab.aclose()
    return time.perf_counter() - t0


# --- 상담 중 Jev 주제 고르기 -----------------------------------------------------------------------------


def topic_request(dialogue_text: str, values: dict) -> tuple[dict, dict, dict[str, str]]:
    keys = {f"t{i + 1}": v["value"] for i, v in enumerate(values["주제"])}
    state = {"task": TOPIC_TASK, "dialogue": dialogue_text}
    question = {"topic": {"type": "choice", "instructions": "지금 대화의 주제는 목록 중 무엇인가?",
                          "criteria": {k: f"{v['value']}: {v['definition']}" for k, v in zip(keys, values["주제"])}}}
    return state, question, keys


async def topic_all(jobs: list[tuple[str, dict, dict]], calls: Calls) -> None:
    from jikgam.decision.registry import make_client
    from jikgam.v2.retry import decide

    todo = [j for j in jobs if not calls.done(j[0])]
    if not todo:
        return
    jev = make_client("jev")
    try:
        await decide(jev, {"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})  # 측정에서 뺀다
        for i, (key, state, question) in enumerate(todo, 1):
            r = await decide(jev, state, question)
            ans = r.answers.get("topic") or {} if not r.error else {}
            calls.add({"key": key, "kind": "topic", "choice": ans.get("choice"), "probabilities": ans.get("probabilities"),
                       "latency_ms": r.latency_ms if not r.error else None, "error": r.error, "tokens": r.input_tokens,
                       "cost_usd": r.cost_usd})
            if i % 50 == 0:
                print(f"주제 {i}/{len(todo)}", flush=True)
    finally:
        await jev.aclose()


# --- 거르기와 평가 -------------------------------------------------------------------------------------


def with_reserved(filtered: list[str], unfiltered: list[str], k: int = 8, reserved: int = RESERVED) -> list[str]:
    """거른 순위 앞 k−reserved개 + 거르지 않은 순위에서 아직 없는 앞 reserved개 + 나머지 거른 순위 + 나머지 거르지 않은 순위."""
    head = filtered[:k - reserved]
    extra = [c for c in unfiltered if c not in head][:reserved]
    seen = set(head) | set(extra)
    return head + extra + [c for c in filtered if c not in seen] + [c for c in unfiltered if c not in seen and c not in filtered]


def run(run_dirs: list[Path], values_path: Path, org_industries: list[str], name: str, only_estimate: bool) -> dict:
    values = json.loads(values_path.read_text(encoding="utf-8"))
    for f in FIELDS:
        if not values.get(f):
            raise ValueError(f"값 목록에 {f}가 없습니다")
    if not set(org_industries) <= {v["value"] for v in values["적용 업종"]}:
        raise ValueError("조직의 적용 업종이 값 목록에 없습니다")
    law = bundle(load_law("r4"))
    cs = law.cs
    from jikgam.ingest import metadata

    info = {cid: metadata.text_for_index(m) for cid, m in metadata.load(law.cfg.chunks, law.cfg.meta).items()}
    dest = ROOT / "results/v2/law-meta" / name
    dest.mkdir(parents=True, exist_ok=True)
    off_path = dest / "official.json"
    official = json.loads(off_path.read_text(encoding="utf-8")) if off_path.exists() else None
    api_docs = set(official) if official else {d for d in cs.docs if cs.docs[d].kind != "표준약관"}
    label_jobs = []
    for c in cs.chunks:
        with_type = c.doc not in api_docs
        text = label_input(values, cs.docs[c.doc].title, info.get(c.id, ""), cs.text(c), with_type)
        label_jobs.append((Calls.key("label", c.id, text), text, label_schema(values, with_type), with_type))
    rows = list(events(run_dirs, set()))
    dialogues = sorted({r["dialogue"] for r in rows})
    topic_jobs = []
    for d in dialogues:
        state, question, _ = topic_request(d, values)
        topic_jobs.append((Calls.key("topic", d), state, question))
    from jikgam.decision.registry import load_config

    cfg = load_config()
    label_chars = sum(len(LABEL_PROMPT) + len(t) + len(json.dumps(s, ensure_ascii=False)) for _, t, s, _ in label_jobs)
    topic_chars = sum(len(json.dumps(s, ensure_ascii=False)) + len(json.dumps(q, ensure_ascii=False)) for _, s, q in topic_jobs)
    g = cfg[GENERATOR]
    est = {"label_calls": len(label_jobs), "label_chars": label_chars,
           "label_est_cost_usd": round((label_chars * EST_TOKENS_PER_CHAR * g["price_input_per_mtok"]
                                        + len(label_jobs) * EST_LABEL_OUTPUT_TOKENS * g["price_output_per_mtok"]) / 1e6 * 2, 3),
           "topic_calls": len(topic_jobs), "topic_chars": topic_chars,
           "topic_est_cost_usd": round(topic_chars * EST_TOKENS_PER_CHAR * cfg["jev"]["price_input_per_mtok"] / 1e6, 3)}
    est["est_cost_usd"] = round(est["label_est_cost_usd"] + est["topic_est_cost_usd"], 3)  # 붙이기는 재질문 한 번까지 두 배로 잡는다
    if only_estimate:
        return {"events": len(rows), "estimate": est}
    if official is None:
        raise ValueError("공식 분류가 없습니다. --official로 먼저 받는다")
    local = ROOT / "data/v2/local/law-meta" / name
    local.mkdir(parents=True, exist_ok=True)
    calls = Calls(local / "calls.jsonl")
    label_wall = asyncio.run(label_all(label_jobs, calls, values))
    asyncio.run(topic_all(topic_jobs, calls))
    # 구간 메타데이터
    meta: dict[str, dict] = {}
    label_rows = []
    for (key, _, _, with_type), c in zip(label_jobs, cs.chunks):
        row = calls.rows[key]
        label_rows.append(row)
        lab = row.get("labels") if not row.get("error") else None
        doc_type = (lab or {}).get("문서 종류") if with_type else OFFICIAL_TYPE.get(official[c.doc]["kind"])
        meta[c.id] = {"doc": c.doc, "문서 종류": doc_type, "적용 업종": (lab or {}).get("적용 업종") or [],
                      "주제": (lab or {}).get("주제") or [], "labelled": lab is not None}
    # 평가
    org_ind = set(org_industries)
    stats = {c: {"items": 0, **{f"found@{k}": 0 for k in KS}, "filtered_events": 0, "gold_dropped_items": 0,
                 "gold_dropped_events": 0} for c in CONFIGS}
    sess = {}
    topic_lat, m1_ms = [], []
    cache: dict[str, list[str]] = {}
    checked = same = 0
    topic_keys = {f"t{i + 1}": v["value"] for i, v in enumerate(values["주제"])}
    for r in rows:
        if r["query"] not in cache:
            cache[r["query"]] = ranking(law, r["query"])
        ranked = cache[r["query"]]
        if r["run"].endswith("-3"):
            checked += 1
            same += ranked[:8] == r["logged_candidates"]
        t0 = time.perf_counter()
        keep1 = {c for c in ranked if org_ind & set(meta[c]["적용 업종"])}
        m1_ms.append((time.perf_counter() - t0) * 1000)
        tr = calls.rows[Calls.key("topic", r["dialogue"])]
        topic, p = None, 0.0
        if not tr.get("error") and tr.get("choice"):
            topic, p = topic_keys.get(tr["choice"]), float((tr.get("probabilities") or {}).get(tr["choice"], 0.0))
        use_topic = topic is not None and p >= JEV_THRESHOLD
        keep2 = {c for c in ranked if topic in meta[c]["주제"]} if use_topic else None
        filters = {"a": None, "m1": keep1, "m2": keep2, "m3": (keep1 & keep2) if keep2 is not None else keep1}
        s = sess.setdefault(r["session"], {"gold": r["gold"], **{c: set() for c in CONFIGS}})
        for c, keep in filters.items():
            st = stats[c]
            if keep is None:
                order = ranked
            else:
                st["filtered_events"] += 1
                filtered = [x for x in ranked if x in keep]
                order = with_reserved(filtered, ranked)
                coverable = found(r["gold"], sorted(keep))
                dropped = sum(1 for ok, all_ok in zip(coverable, found(r["gold"], ranked)) if all_ok and not ok)
                st["gold_dropped_items"] += dropped
                st["gold_dropped_events"] += dropped > 0
            st["items"] += len(r["gold"])
            for k in KS:
                st[f"found@{k}"] += sum(found(r["gold"], order[:k]))
            s[c].update(order[:8])
        if not tr.get("error") and tr.get("latency_ms") is not None:
            topic_lat.append(tr["latency_ms"])
    sessions = {c: {"items": 0, "found@8": 0} for c in CONFIGS}
    for s in sess.values():
        for c in CONFIGS:
            sessions[c]["items"] += len(s["gold"])
            sessions[c]["found@8"] += sum(found(s["gold"], sorted(s[c])))
    for block in (stats, sessions):
        for st in block.values():
            for k in KS:
                if f"found@{k}" in st:
                    st[f"rate@{k}"] = round(st[f"found@{k}"] / st["items"], 4)
    for c in CONFIGS:
        stats[c]["filtered_rate"] = round(stats[c]["filtered_events"] / len(rows), 4)
    sample_ids = random.Random(SAMPLE_SEED).sample([c.id for c in cs.chunks], SAMPLE_SIZE)
    sample = [{"segment": cid, "document": cs.docs[cs.by_id[cid].doc].title, "lines": f"{cs.by_id[cid].start_line}~{cs.by_id[cid].end_line}",
               **{f: meta[cid][f] for f in FIELDS}} for cid in sample_ids]
    with (local / "labels-sample.md").open("w", encoding="utf-8", newline="\n") as f:
        for i, x in enumerate(sample, 1):
            f.write(f"## {i}. {x['segment']} ({x['document']} {x['lines']}줄)\n\n문서 종류: {x['문서 종류']}\n\n"
                    f"적용 업종: {', '.join(x['적용 업종'])}\n\n주제: {', '.join(x['주제'])}\n\n"
                    f"구간 본문:\n\n{cs.text(cs.by_id[x['segment']])}\n\n판단(맞음/틀림/모름, 고칠 값): \n\n")
    lat = lambda xs: {"p50": round(percentile_inf(xs, 50), 3), "p95": round(percentile_inf(xs, 95), 3)} if xs else None  # noqa: E731
    report = {"profile": "r4", "runs": [d.as_posix() for d in run_dirs], "events": len(rows), "estimate": est,
              "logged_top8_checked": checked, "logged_top8_same": same, "org_industries": sorted(org_ind),
              "labelling": {"segments": len(label_rows), "failed": sum(1 for x in label_rows if x.get("error")),
                            "cost_usd": round(sum(x.get("cost_usd") or 0 for x in label_rows), 4), "wall_s": round(label_wall, 1),
                            "models": sorted({x.get("model") for x in label_rows if x.get("model")})},
              "topic_pick": {"calls": len(topic_jobs), "failed": sum(1 for k, *_ in topic_jobs if calls.rows[k].get("error")),
                             "latency_ms": lat(topic_lat), "cost_usd": round(sum(calls.rows[k].get("cost_usd") or 0 for k, *_ in topic_jobs), 4)},
              "m1_filter_ms": lat(m1_ms), "configs": stats, "sessions_top8": sessions, "labels_sample": sample}
    (dest / "segments.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="*", type=Path)
    ap.add_argument("--values", type=Path, default=ROOT / "results/v2/law-meta/values.json")
    ap.add_argument("--industries", default="", help="조직의 적용 업종(값 목록의 값, 쉼표로 구분)")
    ap.add_argument("--name", default="dev-v2")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--official", action="store_true", help="법제처 오픈 API로 공식 분류만 받아 저장한다")
    args = ap.parse_args()
    if args.official:
        out = official_classification()
        dest = ROOT / "results/v2/law-meta" / args.name
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "official.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(out, ensure_ascii=False))
        return
    out = run(args.run_dirs, args.values, [x.strip() for x in args.industries.split(",") if x.strip()], args.name, args.estimate)
    print(json.dumps(out if args.estimate else {c: (s["rate@8"], s["rate@16"]) for c, s in out["configs"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
