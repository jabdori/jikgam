"""검색 후보 재현율을 train·val 판별 데이터로 잰다. test는 읽지 않는다(CLAUDE.md, docs/evaluation.md §8).

    uv run python -m jikgam.engine.retrieval_eval --org org_a

정의: 사례마다 정답 applies:<조항>이 "true"인 조항 중 엔진 검색 후보(전체 약관에서 고른 max_candidates개)에 든 비율.
정답은 사례의 article_ids 안에서만 있으므로, 그 밖의 조항은 재현율 계산에 들어가지 않는다.

질의는 엔진과 같게 만든다: 사례 대화의 최근 고객 발화 + known_facts.request. 상태로 붙이는 절차 조항 규칙은
state.identity_verified와 요청 intent로 판단한다. 데이터의 request는 대부분 글이라(예: "연 구독 해지 및 잔액 환불")
intent 라벨일 때만 intent로 쓰고, 아니면 질의 글로만 쓴다.

구성별로 따로 보고한다.
- bm25: 키워드 검색 상위만(구조 확장·규칙 없음)
- structure: bm25 + 구조 확장(참조, 예외·부칙·별표)
- full: structure + 상태 규칙(엔진 실행과 같은 구성)
- full_oracle_intent: full에서 intent를 사례 정답 intent로 준 것. 엔진은 직전 판단의 intent를 쓰므로 상한 참고용이다.
"""

import argparse
import dataclasses
import json
from collections import Counter

from .org import ROOT, load_org
from .retrieval import ArticleIndex, RetrievalContext, parse_terms

SPLITS = ("train", "val")
INTENT_LABELS = ("refund", "cancel", "cancel_refund", "plan_change", "billing_inquiry", "compensation", "other", "unclear")


def load_split(split: str) -> list[dict]:
    if split not in SPLITS:
        raise ValueError("검색 평가는 train과 val만 쓴다")
    path = ROOT / f"data/build/{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_context(row: dict, oracle_intent: bool) -> tuple[RetrievalContext, list[str]]:
    state = json.loads(row["state"])
    gold = json.loads(row["gold"])
    request = str(state["known_facts"].get("request") or "").replace("(고객 진술)", "").strip() or None
    intent = request if request in INTENT_LABELS else None
    if oracle_intent and "intent" in gold:
        intent = gold["intent"]["label"]
    ctx = RetrievalContext(
        customer_texts=tuple(u["text"] for u in state["conversation"] if u["speaker"] == "customer"),
        intent=intent, request_text=None if request in INTENT_LABELS else request,
        identity_verified=bool(state["identity_verified"]),
    )
    gold_true = [q.split(":", 1)[1] for q, g in gold.items() if q.startswith("applies:") and g["label"] == "true"]
    return ctx, gold_true


def evaluate(rows: list[dict], index: ArticleIndex, oracle_intent: bool = False) -> dict:
    hit = total = full_cases = 0
    by_type: Counter = Counter()
    by_type_hit: Counter = Counter()
    missed: Counter = Counter()
    n_cand = []
    for row in rows:
        ctx, gold_true = case_context(row, oracle_intent)
        cands = set(index.search(ctx).candidates)
        n_cand.append(len(cands))
        h = [a for a in gold_true if a in cands]
        hit, total = hit + len(h), total + len(gold_true)
        full_cases += len(h) == len(gold_true)
        for a in gold_true:
            t = index.articles[a].type
            by_type[t] += 1
            by_type_hit[t] += a in cands
            if a not in cands:
                missed[a] += 1
    return {
        "cases": len(rows), "gold_true": total, "hit": hit, "recall": round(hit / total, 3) if total else None,
        "cases_full_recall": full_cases, "mean_candidates": round(sum(n_cand) / len(n_cand), 2),
        "by_type": {t: f"{by_type_hit[t]}/{by_type[t]}" for t in sorted(by_type)},
        "missed": dict(missed.most_common()),
    }


def variants(index: ArticleIndex) -> dict[str, ArticleIndex]:
    cfg = index.cfg
    return {
        "bm25": ArticleIndex(index.articles, dataclasses.replace(cfg, seed_top_k=0, rules=())),
        "structure": ArticleIndex(index.articles, dataclasses.replace(cfg, rules=())),
        "full": index,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--max-candidates", type=int, default=0, help="설정값 대신 쓸 후보 수(비교용)")
    args = ap.parse_args()
    org = load_org(args.org)
    cfg = org.retrieval
    if args.max_candidates:
        cfg = dataclasses.replace(cfg, max_candidates=args.max_candidates)
    index = ArticleIndex(parse_terms(org.terms), cfg)
    print(f"조직 {org.org}, 후보 상한 {cfg.max_candidates}, 출발 조항 {cfg.seed_top_k}, 최근 고객 발화 {cfg.query_turns}")
    for split in SPLITS:
        rows = load_split(split)
        for name, idx in variants(index).items():
            r = evaluate(rows, idx)
            print(f"{split:5s} {name:18s} 재현율 {r['hit']}/{r['gold_true']} = {r['recall']}  "
                  f"전부 찾은 사례 {r['cases_full_recall']}/{r['cases']}  평균 후보 {r['mean_candidates']}  "
                  f"유형별 {r['by_type']}")
        r = evaluate(rows, index, oracle_intent=True)
        print(f"{split:5s} {'full_oracle_intent':18s} 재현율 {r['hit']}/{r['gold_true']} = {r['recall']}  "
              f"전부 찾은 사례 {r['cases_full_recall']}/{r['cases']}  유형별 {r['by_type']}")
        print(f"{split:5s} full에서 놓친 조항(횟수): {evaluate(rows, index)['missed']}")


if __name__ == "__main__":
    main()
