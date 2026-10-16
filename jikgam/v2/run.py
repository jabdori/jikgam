"""v2 검색 구성 하나를 질문 세트에 돌려 채점하고 단계 기록을 남긴다(docs/evaluation-v2.md §3.6~§3.8, §4.2).

구성은 분할(구간 파일 이름), 구간 정보(없음, full, noexpr), 검색(R0 규칙, BM25, 하이브리드)으로 정한다.

결과:
- 수치: results/v2/<분할>/<이름>.json(저장소)
- 단계 기록: data/v2/local/runs/<분할>/<이름>.jsonl(질문 id, 검색·리랭킹 시간, 상위 구간, 따라간 구간)

test는 설정을 결정 기록으로 고정한 뒤 한 번만 돈다. --split test에는 --frozen <결정 기록>이 있어야 한다.

    uv run --group embed python -m jikgam.v2.run --chunks b-W4000-C800-raw --meta full --embed kure1 --fusion rrf --split val
    uv run python -m jikgam.v2.run --chunks article256 --r0 --split val
"""

import argparse
import asyncio
import json
import statistics
import time

from jikgam.ingest import chunk as chunking
from jikgam.ingest import metadata
from jikgam.ingest.corpus import ROOT, load_docs
from jikgam.v2.evaluate import Scorer, evaluate_rankings, load_questions
from jikgam.v2.hybrid import Hybrid, HybridConfig
from jikgam.v2.search import ChunkSet, rank_r0

RESULTS = ROOT / "results" / "v2"
RUNS = ROOT / "data" / "v2" / "local" / "runs"


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 2)


async def run(args) -> dict:
    docs = load_docs()
    chunks = chunking.load(args.chunks)
    meta = metadata.load(args.chunks, args.meta) if args.meta != "none" else {}
    if args.meta != "none":
        missing = [c.id for c in chunks if c.id not in meta]
        if missing:
            raise SystemExit(f"구간 정보가 없는 구간 {len(missing)}개({args.meta}). 먼저 jikgam.ingest.metadata를 돌린다")
    extra = {cid: metadata.text_for_index(m) for cid, m in meta.items()}
    cs = ChunkSet(args.chunks, chunks, docs, extra)
    questions = load_questions(args.split)
    if args.split == "test" and not args.frozen:
        raise SystemExit("test는 설정을 고정한 결정 기록(--frozen)이 있어야 돈다")
    traces, rankings = {}, {}
    info = {}
    if args.r0:
        name = f"R0-{args.chunks}"
        for q in questions:
            t0 = time.perf_counter()
            rankings[q["id"]] = [c for c, _ in rank_r0(cs, q["question"])]
            traces[q["id"]] = {"retrieval_ms": (time.perf_counter() - t0) * 1000}
    else:
        cfg = HybridConfig(args.embed, args.fusion, args.related, args.rerank)
        name = f"{args.chunks}--{args.meta}--{cfg.name}"
        emb = None
        if args.embed:
            from jikgam.v2.embed import EmbeddingIndex

            texts = [cs.text(c) + ("\n" + extra[c.id] if c.id in extra else "") for c in chunks]
            titles = [docs[c.doc].title for c in chunks]
            emb = EmbeddingIndex(args.embed, [c.id for c in chunks], texts, titles, f"{args.chunks}--{args.meta}")
            emb.rank("예열 질의입니다.")  # 모델을 올리고 첫 호출의 지연을 뺀다
            info["embedding"] = emb.info()
        jev = None
        if args.rerank:
            from jikgam.decision.registry import make_client

            jev = make_client("jev")
            await jev.decide({"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})
        hy = Hybrid(cs, cfg, meta, emb, jev)
        try:
            for q in questions:
                ranked, tr = await hy.rank(q["question"])
                rankings[q["id"]] = [c for c, _ in ranked]
                traces[q["id"]] = {"retrieval_ms": tr.retrieval_ms, "rerank_ms": tr.rerank_ms,
                                   "rerank_tokens": tr.rerank_tokens, "rerank_cost_usd": tr.rerank_cost_usd,
                                   "rerank_error": tr.rerank_error, "related_added": tr.related_added}
        finally:
            if jev:
                await jev.aclose()
    metrics = evaluate_rankings(cs, questions, rankings, Scorer(docs))
    ret = [t["retrieval_ms"] for t in traces.values()]
    rr = [t["rerank_ms"] for t in traces.values() if t.get("rerank_ms") is not None]
    out = {"name": name, "split": args.split, "chunks": args.chunks, "meta": args.meta,
           "config": {"r0": args.r0, "embed": args.embed, "fusion": args.fusion, "related": args.related,
                      "rerank": args.rerank},
           "metrics": metrics,
           "latency_ms": {"retrieval_p50": pct(ret, 0.5), "retrieval_p95": pct(ret, 0.95),
                          "rerank_p50": pct(rr, 0.5), "rerank_p95": pct(rr, 0.95)},
           "rerank": {"cost_usd": round(sum(t.get("rerank_cost_usd") or 0 for t in traces.values()), 5),
                      "errors": sum(1 for t in traces.values() if t.get("rerank_error"))} if rr else None,
           **info}
    (RESULTS / args.split).mkdir(parents=True, exist_ok=True)
    (RESULTS / args.split / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                                                        encoding="utf-8", newline="\n")
    (RUNS / args.split).mkdir(parents=True, exist_ok=True)
    with (RUNS / args.split / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for qid, tr in traces.items():
            fh.write(json.dumps({"id": qid, **tr, "top10": rankings[qid][:10]}, ensure_ascii=False) + "\n")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--meta", choices=["none", "full", "noexpr"], default="none")
    ap.add_argument("--r0", action="store_true", help="기준선 R0 규칙(조 단위 구간에 쓴다)")
    ap.add_argument("--embed", choices=["kure1", "snow", "kure2", "gemini"])
    ap.add_argument("--fusion", default="rrf", choices=["rrf", "w0.3", "w0.5", "w0.7"])
    ap.add_argument("--related", action="store_true")
    ap.add_argument("--rerank", type=int, default=0)
    ap.add_argument("--split", choices=["train", "val", "test"], default="val")
    ap.add_argument("--frozen")
    args = ap.parse_args()
    out = asyncio.run(run(args))
    m = out["metrics"]
    print(json.dumps({"name": out["name"], "all@2000": m["all"]["items@2000"],
                      "single@2000": m.get("single", {}).get("items@2000"), "latency": out["latency_ms"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
