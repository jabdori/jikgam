"""v2 답변 품질(docs/evaluation-v2.md §4.3, §8 보완).

1. 답변 만들기: 검색 구성이 고른 예산 2,000토큰의 글(§4.2와 같은 순서)을 `gemini-3.8-flash`에 넘겨 고객에게 할 답을
   만든다. 넘긴 글에 있는 내용만 말하고 근거 구간을 표시하게 한다. 글로 확인할 수 없으면 확인할 수 없다고 말한다.
   Jev의 충분성 판단은 쓰지 않는다.
2. 판정: `gemini-3.1-pro-preview`가 답을 하나의 사실을 말하는 주장들로 나눈다. 주장마다
   - 근거 있음/없음: 생성에 넘긴 글에 그 주장을 뒷받침하는 내용이 있는가
   - 정답과 맞음/어긋남/판단 불가: 질문의 정답 근거와 맞는가
3. 지표: 근거 없는 주장 비율(주장 기준, 답 기준), 정답률, 보류율, 근거 없음 질문의 단정률.

판정에 쓰는 질문은 단일 근거와 근거 없음이다(§8 보완). 여러 근거는 보고만 한다.
"""

import json

from jikgam.ingest import tokens
from jikgam.v2.search import ChunkSet
from jikgam.v2.retry import ask_json

GEN_MODEL = "gemini-flash"
JUDGE_MODEL = "gemini-pro"
BUDGET = 2000

GEN_SYSTEM = """너는 소비자 상담원이다. 고객의 질문에, 주어진 근거 글에 있는 내용만으로 답한다.
- 근거 글은 법령, 고시, 표준약관에서 검색한 구간들이다. 구간마다 [근거 n]으로 표시되어 있다.
- 근거 글에 있는 내용만 말한다. 근거 글에 없는 사실, 수치, 절차를 지어내거나 일반 상식으로 채우지 않는다.
- 말한 내용마다 어느 근거에서 왔는지 [근거 n]으로 표시한다.
- 근거 글로 확인할 수 없으면 확인할 수 없다고 말한다. 일부만 확인되면 확인되는 부분만 말하고 나머지는 확인할 수 없다고 한다.
- 고객에게 쓰는 말투로, 짧게 답한다."""

GEN_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

JUDGE_SYSTEM = """너는 상담 답변을 판정한다.
1. 답변을 하나의 사실을 말하는 주장들로 나눈다. 인사, 공감, "확인할 수 없다"는 말, 다른 곳에 문의하라는 안내처럼 사실을 말하지 않는 문장은 주장이 아니다.
2. 주장마다 두 가지를 판정한다.
   - supported: 답변을 만들 때 넘긴 근거 글에 그 주장을 뒷받침하는 내용이 있으면 true, 없으면 false.
   - correctness: 질문의 정답 근거(과 정답 요약)에 비추어 맞으면 "match", 어긋나면 "contradict", 정답 근거로 판단할 수 없으면 "unknown".
3. 판정 근거를 짧게 적는다."""

JUDGE_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {
    "type": "object", "properties": {"claim": {"type": "string"}, "supported": {"type": "boolean"},
                                     "correctness": {"type": "string", "enum": ["match", "contradict", "unknown"]},
                                     "reason": {"type": "string"}},
    "required": ["claim", "supported", "correctness", "reason"]}}}, "required": ["claims"]}


def evidence_block(cs: ChunkSet, ranked: list[str], budget: int = BUDGET) -> str:
    """예산 안의 글을 [근거 n]으로 묶어 이어 붙인다. 자르는 규칙은 검색 채점(budget_coverage)과 같다."""
    parts, left = [], budget
    for cid in ranked:
        if left <= 0:
            break
        c = cs.by_id[cid]
        text = cs.text(c)
        n = c.tokens if c.tokens else tokens.count(text)
        if n > left:
            text = text[:tokens.cut(text, left)]
            n = left
        left -= n
        parts.append(f"[근거 {len(parts) + 1}] 「{cs.docs[c.doc].title}」\n{text}")
    return "\n\n".join(parts)


def gold_block(cs: ChunkSet, item: dict) -> str:
    if item["type"] == "none":
        return "(이 질문은 주어진 문서들로 답할 수 없는 질문이다. 정답 근거가 없다.)"
    out = [f"정답 요약: {item.get('answer') or ''}"]
    for k, ev in enumerate(item["evidence"], 1):
        a = ev["alternatives"][0]
        out.append(f"정답 근거 {k} 「{cs.docs[a['doc']].title}」: {cs.docs[a['doc']].text[a['start']:a['end']]}")
    return "\n".join(out)


async def generate(client, question: str, evidence: str, seed: int) -> dict:
    user = f"## 근거 글\n{evidence or '(검색된 글이 없다)'}\n\n## 고객 질문\n{question}"
    r = await ask_json(client, GEN_SYSTEM, user, GEN_SCHEMA, seed=seed)
    return {"answer": (r.data or {}).get("answer", ""), "usage": r.usage()}


async def judge(client, question: str, evidence: str, gold: str, answer: str) -> dict:
    user = (f"## 질문\n{question}\n\n## 답변을 만들 때 넘긴 근거 글\n{evidence or '(없음)'}\n\n## 정답\n{gold}\n\n"
            f"## 판정할 답변\n{answer}")
    r = await ask_json(client, JUDGE_SYSTEM, user, JUDGE_SCHEMA)
    return {"claims": (r.data or {}).get("claims") or [], "usage": r.usage(), "error": r.error}


def answer_metrics(rows: list[dict]) -> dict:
    """주장 비율은 모든 주장을 합친 분모, 답 기준 지표는 질문별 반복 평균으로 계산한다."""
    by_q: dict[str, list[dict]] = {}
    for r in rows:
        by_q.setdefault(r["id"], []).append(r)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None

    claims = [c for r in rows for c in r["claims"]]
    ans_unsup, acc, hold, assert_none = [], [], [], []
    for qid, reps in by_q.items():
        t = reps[0]["type"]
        au, ac, ho, an = [], [], [], []
        for r in reps:
            cl = r["claims"]
            au.append(float(any(not c["supported"] for c in cl)))
            if t == "none":
                an.append(float(bool(cl)))
            elif t == "single":
                ho.append(float(not cl))
                ac.append(float(any(c["correctness"] == "match" for c in cl)
                                and not any(c["correctness"] == "contradict" for c in cl)))
        ans_unsup.append(mean(au))
        if t == "none":
            assert_none.append(mean(an))
        elif t == "single":
            acc.append(mean(ac))
            hold.append(mean(ho))
    return {"questions": len(by_q), "claims": len(claims), "unsupported_claims": sum(not c["supported"] for c in claims),
            "unsupported_claim_rate": sum(not c["supported"] for c in claims) / len(claims) if claims else None,
            "answers_with_unsupported": mean(ans_unsup),
            "accuracy": mean(acc), "hold_rate": mean(hold), "none_assertion_rate": mean(assert_none)}


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)


async def run(name: str, split: str, reps: int, types: set[str], limit: int | None) -> dict:
    import asyncio
    import random

    from jikgam.ingest import chunk as chunking
    from jikgam.ingest.corpus import ROOT, load_docs
    from jikgam.sim.gemini import GeminiJson
    from jikgam.v2.evaluate import load_questions

    res = json.loads((ROOT / "results" / "v2" / split / f"{name}.json").read_text(encoding="utf-8"))
    log = {json.loads(line)["id"]: json.loads(line) for line in
           (ROOT / "data" / "v2" / "local" / "runs" / split / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()}
    docs = load_docs()
    cs = ChunkSet(res["chunks"], chunking.load(res["chunks"]), docs)
    qs = [q for q in load_questions(split, include_none=True) if q["type"] in types]
    if any(q["id"] not in log for q in qs):
        raise ValueError("검색 단계 기록에 필요한 질문이 빠졌습니다")
    if limit:
        qs = random.Random(f"answer:{split}").sample(qs, min(limit, len(qs)))
    from jikgam.v2.claim_check import invalidate_review

    invalidate_review(ROOT, split)
    gen = GeminiJson.from_config(GEN_MODEL, retry_delays_s=())
    jud = GeminiJson.from_config(JUDGE_MODEL, timeout_s=600.0, retry_delays_s=())
    sem = asyncio.Semaphore(8)

    async def one(q: dict, rep: int) -> dict:
        evidence = evidence_block(cs, log[q["id"]]["budget4000"])
        async with sem:
            g = await generate(gen, q["question"], evidence, seed=1000 + rep)
        async with sem:
            j = await judge(jud, q["question"], evidence, gold_block(cs, q), g["answer"])
        return {"id": q["id"], "type": q["type"], "group": q["group"], "rep": rep, "answer": g["answer"],
                "claims": j["claims"], "gen_usage": g["usage"], "judge_usage": j["usage"], "judge_error": j["error"]}

    try:
        rows = await asyncio.gather(*(one(q, r) for q in qs for r in range(reps)))
    finally:
        await gen.aclose()
        await jud.aclose()
    out_dir = ROOT / "data" / "v2" / "local" / "answers" / split
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.jsonl").write_text("".join(dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    summary = {"name": name, "split": split, "reps": reps, "types": sorted(types), "questions": len(qs),
               "gen_model": GEN_MODEL, "judge_model": JUDGE_MODEL,
               "by_type": {t: answer_metrics([r for r in rows if r["type"] == t]) for t in sorted(types)},
               "judgment": answer_metrics([r for r in rows if r["type"] in ("single", "none")]),
               "judge_errors": sum(1 for r in rows if r["judge_error"]),
               "cost_usd": round(sum(r["gen_usage"]["cost_usd"] + r["judge_usage"]["cost_usd"] for r in rows), 4)}
    (ROOT / "results" / "v2" / split / f"answers-{name}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summary


def main() -> None:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="검색 실행 이름(results/v2/<분할>/<이름>.json)")
    ap.add_argument("--split", default="val", choices=["train", "val", "test"])
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--types", default="single,none")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--frozen")
    args = ap.parse_args()
    if args.split == "test" and not args.frozen:
        raise SystemExit("test는 설정을 고정한 결정 기록(--frozen)이 있어야 돈다")
    print(dumps(asyncio.run(run(args.run, args.split, args.reps, set(args.types.split(",")), args.limit))))


if __name__ == "__main__":
    main()
