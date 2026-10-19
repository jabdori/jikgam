"""개발용 v2 로그에서 법령 후보를 넓게 뽑은 뒤 리랭커로 다시 고르는 구성을 오프라인 비교한다.

엔진과 고정 설정은 바꾸지 않는다. 측정 단위는 jikgam/v2/law_variants.py와 같다: 엔진이 실제로 법령 검색을 한 시점(근거를 고른
retrieval.done)마다 그때까지의 대화로 만든 지금 검색어(고정 규칙)로 R4 하이브리드 검색을 다시 돌리고, 상위 N개(16, 32, 50)를
리랭커로 다시 줄 세운 뒤 상위 8개(와 16위 안)가 정답 법령 항목을 덮는지 본다. 기준과 합격선은 docs/v2-law-rerank.md에 실행 전에 적었다.

- r1: Jev(config/decision_models.toml의 jev). v2 검색 실험의 리랭킹(jikgam/v2/hybrid.py Hybrid.rerank)을 그대로 쓴다. 유료다.
- r2: 공개 한국어 지원 리랭커(cross-encoder) 하나를 이 PC GPU에서 돌린다. 모델은 실행 전에 하나만 골랐다(R2_MODEL).
- r3: 튜닝 2B 판별 모델(q35-2b-ko-direct, WSL2 서버). r1과 같은 질의 형식이다. 서버가 뜰 때만 돈다.

리랭킹은 한 번에 하나씩 차례로 부르고(지연을 겹치지 않게 잰다), 한 번의 지연은 요청 하나(r1·r3, 재시도 대기 제외) 또는
토크나이즈부터 점수까지(r2)다. 실패하면 검색 순서를 그대로 쓰고 실패 수를 센다.

    uv run --group embed python -m jikgam.v2.law_rerank results/engine/scenarios/dev-v2-r4-{1,2,3} --estimate
    uv run --group embed python -m jikgam.v2.law_rerank results/engine/scenarios/dev-v2-r4-{1,2,3} --backends r1,r3
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT, load_org
from jikgam.sim.metrics import percentile_inf
from jikgam.v2.e2e_queries import found
from jikgam.v2.hybrid import Hybrid, Trace
from jikgam.v2.law_rewrite import events
from jikgam.v2.law_variants import frozen_query

NS = (16, 32, 50)
KS = (8, 16)
BACKENDS = {"r1": "jev", "r3": "q35-2b-ko-direct"}
# r2 모델(실행 전에 고름): 공개(Apache-2.0) 다국어 cross-encoder로 한국어를 학습 자료에 포함한다. 리비전을 고정한다
R2_MODEL = "BAAI/bge-reranker-v2-m3"
R2_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
R2_MAX_LENGTH = 512
# r1 예상 비용의 가정(실행 전에 고정): 상태(작업 설명, 질문, 후보 문서 제목과 본문)와 후보별 질문의 글자 1개를 토큰 1개로 잡는다
EST_TOKENS_PER_CHAR = 1.0


def rerank_state_chars(law, query: str, head: list[str]) -> int:
    """Hybrid.rerank가 Jev에 보내는 상태와 질문의 글자 수(예상 비용 계산용)."""
    cs = law.cs
    state = len(query) + 60 + sum(len(cs.docs[cs.by_id[c].doc].title) + len(cs.text(cs.by_id[c])) + 20 for c in head)
    return state + len(head) * 40


class CrossEncoderReranker:
    """r2: 로컬 GPU cross-encoder. 한 번 = 질문 하나와 후보 N개 쌍을 한 묶음으로 점수 낸다."""

    def __init__(self, law):
        import torch
        from sentence_transformers import CrossEncoder

        self.law, self.torch = law, torch
        self.model = CrossEncoder(R2_MODEL, revision=R2_REVISION, max_length=R2_MAX_LENGTH, device="cuda",
                                  model_kwargs={"torch_dtype": torch.float16}, local_files_only=True)
        self.model.predict([("예열", "예열 문장")])

    def __call__(self, query: str, head: list[str]) -> tuple[list[float], float]:
        cs = self.law.cs
        pairs = [(query, cs.text(cs.by_id[c])) for c in head]
        self.torch.cuda.synchronize()
        t0 = time.perf_counter()
        scores = self.model.predict(pairs, batch_size=len(pairs), show_progress_bar=False)
        self.torch.cuda.synchronize()
        return [float(s) for s in scores], (time.perf_counter() - t0) * 1000


async def rerank_remote(law, backend, query: str, ranked: list[tuple[str, float]], n: int) -> dict:
    h = Hybrid(law.cs, replace(law.hybrid.cfg, rerank=n), law.hybrid.meta, law.hybrid.emb, jev=backend)
    tr = Trace()
    out = await h.rerank(query, ranked, tr)
    return {"order": [c for c, _ in out[:max(KS)]], "latency_ms": tr.rerank_ms, "error": tr.rerank_error,
            "cost_usd": tr.rerank_cost_usd, "tokens": tr.rerank_tokens}


def run(run_dirs: list[Path], backends: list[str], name: str, only_estimate: bool) -> dict:
    org = load_org("org_a")
    law = bundle(load_law("r4"))
    rows = [r for r in events(run_dirs) if r["profile"] == "r4"]
    for r in rows:
        r["query"] = frozen_query(org, r["customer"], r["intent"])
    queries = sorted({r["query"] for r in rows})
    ranked = {q: law.hybrid.retrieve(q)[0] for q in queries}
    if only_estimate:
        from jikgam.decision.registry import load_config

        price = load_config()["jev"]["price_input_per_mtok"]
        chars = sum(rerank_state_chars(law, q, [c for c, _ in ranked[q][:n]]) for q in queries for n in NS)
        return {"events": len(rows), "unique_queries": len(queries), "r1_calls": len(queries) * len(NS),
                "r1_input_chars": chars, "r1_est_cost_usd": round(chars * EST_TOKENS_PER_CHAR * price / 1e6, 3)}
    local = ROOT / "data/v2/local/law-rerank" / name
    local.mkdir(parents=True, exist_ok=True)
    cache_path = local / "calls.jsonl"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            cache[row["key"]] = row

    def key(b: str, n: int, q: str) -> str:
        return hashlib.sha256(f"{b}|{n}|{q}".encode("utf-8")).hexdigest()

    with cache_path.open("a", encoding="utf-8", newline="\n") as out:
        for b in backends:
            todo = [(n, q) for n in NS for q in queries if key(b, n, q) not in cache or cache[key(b, n, q)].get("error")]
            if b == "r2":
                ce = CrossEncoderReranker(law)
                for i, (n, q) in enumerate(todo, 1):
                    head = [c for c, _ in ranked[q][:n]]
                    scores, ms = ce(q, head)
                    pos = {c: j for j, c in enumerate(head)}
                    new = sorted(head, key=lambda c: (-scores[pos[c]], pos[c])) + [c for c, _ in ranked[q][n:]]
                    row = {"key": key(b, n, q), "backend": b, "n": n, "order": new[:max(KS)], "latency_ms": ms, "error": None}
                    cache[row["key"]] = row
                    out.write(json.dumps(row) + "\n")
                    if i % 100 == 0:
                        print(f"{b} {i}/{len(todo)}", flush=True)
                continue
            from jikgam.decision.registry import make_client
            from jikgam.v2.retry import decide

            async def go():
                be = make_client(BACKENDS[b])
                await decide(be, {"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})  # 측정에서 뺀다
                try:
                    for i, (n, q) in enumerate(todo, 1):
                        res = await rerank_remote(law, be, q, ranked[q], n)
                        row = {"key": key(b, n, q), "backend": b, "n": n, **res}
                        cache[row["key"]] = row
                        out.write(json.dumps(row) + "\n")
                        out.flush()
                        if i % 50 == 0:
                            print(f"{b} {i}/{len(todo)}", flush=True)
                finally:
                    close = getattr(be, "aclose", None)
                    if close is not None:
                        await close()
            asyncio.run(go())
    report: dict = {"profile": "r4", "runs": [d.as_posix() for d in run_dirs], "events": len(rows), "unique_queries": len(queries),
                    "r2_model": {"name": R2_MODEL, "revision": R2_REVISION, "max_length": R2_MAX_LENGTH}, "configs": {}}
    base = {"items": 0, **{f"found@{k}": 0 for k in KS}}
    for r in rows:
        ids = [c for c, _ in ranked[r["query"]]]
        base["items"] += len(r["gold"])
        for k in KS:
            base[f"found@{k}"] += sum(found(r["gold"], ids[:k]))
    report["configs"]["a"] = base
    for b in backends:
        for n in NS:
            calls = [cache[key(b, n, q)] for q in queries]
            ok = [c for c in calls if not c.get("error") and c.get("latency_ms") is not None]
            stat = {"items": 0, **{f"found@{k}": 0 for k in KS}, "calls": len(calls), "failed": len(calls) - len(ok),
                    "latency_ms": {"p50": round(percentile_inf([c["latency_ms"] for c in ok], 50), 1),
                                   "p95": round(percentile_inf([c["latency_ms"] for c in ok], 95), 1)} if ok else None,
                    "cost_usd": round(sum(c.get("cost_usd") or 0.0 for c in calls), 4)}
            for r in rows:
                c = cache[key(b, n, r["query"])]
                ids = c["order"] if not c.get("error") else [x for x, _ in ranked[r["query"]]]
                stat["items"] += len(r["gold"])
                for k in KS:
                    stat[f"found@{k}"] += sum(found(r["gold"], ids[:k]))
            report["configs"][f"{b}-N{n}"] = stat
    for stat in report["configs"].values():
        for k in KS:
            stat[f"rate@{k}"] = round(stat[f"found@{k}"] / stat["items"], 4)
    dest = ROOT / "results/v2/law-rerank" / name
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "report.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    report["configs"] = {**old.get("configs", {}), **report["configs"]}  # 백엔드를 나눠 돌려도 한 보고에 모은다
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--backends", default="r1", help="쉼표로 구분: r1, r2, r3")
    ap.add_argument("--name", default="dev-v2")
    ap.add_argument("--estimate", action="store_true", help="호출하지 않고 r1 예상 비용만 낸다")
    args = ap.parse_args()
    out = run(args.run_dirs, [b.strip() for b in args.backends.split(",") if b.strip()], args.name, args.estimate)
    print(json.dumps(out if args.estimate else {k: (v["rate@8"], v["rate@16"], v.get("latency_ms")) for k, v in out["configs"].items()},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
