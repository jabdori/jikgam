"""v2 검색 채점(docs/evaluation-v2.md §4.2).

- 예산 안의 글: 결과 구간의 원문을 순서대로 이어 붙여 예산(Qwen3.5 토큰)에서 자른 것. 구간 정보는 넣지 않는다.
  겹친 글은 있는 그대로 세고, 마지막 구간은 잘린 부분까지만 본다.
- 찾음: 정답 근거 항목은 대체 근거 중 하나라도, 예산 안의 글이 그 글자(공백 제외)의 절반 이상을 덮으면 찾은 것이다.
- 근거 재현율@B: 모든 질문의 정답 근거 항목을 합쳐 찾은 비율. 완전 재현율@B: 항목을 모두 찾은 질문의 비율.
- 개수 기준 recall@k, nDCG@10: 상위 k 구간만으로 같은 규칙. nDCG는 항목을 하나라도 덮은 구간을 관련 있음(1)으로 본다.
- 판정은 단일 근거 질문으로 한다(§8 보완). 여러 근거는 보고만 한다.

test는 설정을 결정 기록으로 고정한 뒤 한 번만 돈다. 이 모듈은 --split test에 --frozen <결정 기록>을 요구한다.

    uv run python -m jikgam.v2.evaluate --chunks fixed512 --ranker bm25 --split val
"""

import argparse
import bisect
import json
import math
from collections import defaultdict

from jikgam.ingest import tokens
from jikgam.ingest.corpus import IGNORED, ROOT, load_docs
from jikgam.v2.search import RANKERS, ChunkSet

BUDGETS = (1000, 2000, 4000)
QA_FILES = [ROOT / "data" / "v2" / "qa" / "items.jsonl", ROOT / "data" / "v2" / "local" / "qa" / "items.jsonl"]
SPLITS = ROOT / "data" / "v2" / "qa" / "splits.json"
# 점검용 질문(§2.4, 다른 계열 모델이 씀). 모두 test 때 한 번만 쓰므로 분할 이름 "check"로 부른다
CHECK_FILES = [ROOT / "data" / "v2" / "check" / "items.jsonl", ROOT / "data" / "v2" / "local" / "check" / "items.jsonl"]
FROZEN_SPLITS = ("test", "check")  # 설정을 고정한 결정 기록(--frozen)이 있어야 도는 질문


def _rows(paths) -> list[dict]:
    return [json.loads(line) for p in paths for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_questions(split: str, include_none: bool = False) -> list[dict]:
    """분할의 질문. 검색 채점은 답이 있는 질문만 쓰고, 답변 품질은 근거 없음 질문도 쓴다."""
    if split == "check":
        rows = _rows(CHECK_FILES)
    else:
        sp = json.loads(SPLITS.read_text(encoding="utf-8"))["splits"]
        rows = [r for r in _rows(QA_FILES) if sp.get(r["id"]) == split]
    return sorted((r for r in rows if include_none or r["type"] != "none"), key=lambda r: r["id"])


class Coverage:
    """문서별로 덮인 글자 범위. 겹친 범위는 합친다."""

    def __init__(self) -> None:
        self.ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)

    def add(self, doc: str, a: int, b: int) -> None:
        if b > a:
            self.ranges[doc].append((a, b))

    def merged(self, doc: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for a, b in sorted(self.ranges.get(doc, [])):
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        return out


class Scorer:
    def __init__(self, docs):
        self.docs = docs
        # 문서별 공백 제외 글자의 누적 수(덮인 글자 수를 빠르게 세려고)
        self.prefix = {}
        for did, d in docs.items():
            acc, arr = 0, [0]
            for ch in d.text:
                acc += ch not in IGNORED
                arr.append(acc)
            self.prefix[did] = arr

    def nonspace(self, doc: str, a: int, b: int) -> int:
        p = self.prefix[doc]
        return p[b] - p[a]

    def covered(self, alt: dict, merged: list[tuple[int, int]]) -> bool:
        need = self.nonspace(alt["doc"], alt["start"], alt["end"])
        if need == 0:
            return False
        got = 0
        i = bisect.bisect_left(merged, (alt["start"], -1))
        i = max(0, i - 1)
        for a, b in merged[i:]:
            if a >= alt["end"]:
                break
            lo, hi = max(a, alt["start"]), min(b, alt["end"])
            if hi > lo:
                got += self.nonspace(alt["doc"], lo, hi)
        return 2 * got >= need

    def found(self, item: dict, cov: Coverage) -> list[bool]:
        merged = {d: cov.merged(d) for d in cov.ranges}
        return [any(self.covered(a, merged.get(a["doc"], [])) for a in ev["alternatives"]) for ev in item["evidence"]]


def budget_coverage(cs: ChunkSet, ranked: list[str], budget: int) -> Coverage:
    cov, left = Coverage(), budget
    for cid in ranked:
        if left <= 0:
            break
        c = cs.by_id[cid]
        text = cs.text(c)
        n = c.tokens if c.tokens else tokens.count(text)
        if n <= left:
            cov.add(c.doc, c.start, c.end)
            left -= n
        else:
            cov.add(c.doc, c.start, c.start + tokens.cut(text, left))
            left = 0
    return cov


def topk_coverage(cs: ChunkSet, ranked: list[str], k: int) -> Coverage:
    cov = Coverage()
    for cid in ranked[:k]:
        c = cs.by_id[cid]
        cov.add(c.doc, c.start, c.end)
    return cov


def evaluate(cs: ChunkSet, questions: list[dict], ranker, scorer: Scorer) -> dict:
    return evaluate_rankings(cs, questions, {q["id"]: [cid for cid, _ in ranker(cs, q["question"])] for q in questions},
                             scorer)


def evaluate_rankings(cs: ChunkSet, questions: list[dict], rankings: dict[str, list[str]], scorer: Scorer) -> dict:
    """질문 id → 구간 순위로 채점한다."""
    per = {"single": defaultdict(list), "multi": defaultdict(list)}
    for q in questions:
        if q["type"] == "none":
            continue
        ranked = rankings[q["id"]]
        res = per[q["type"]]
        for b in BUDGETS:
            f = scorer.found(q, budget_coverage(cs, ranked, b))
            res[f"items@{b}"] += f
            res[f"complete@{b}"].append(all(f))
        for k in (5, 10):
            res[f"recall@{k}"] += scorer.found(q, topk_coverage(cs, ranked, k))
        # nDCG@10: 항목을 하나라도 덮은 구간 = 관련 있음
        rel = []
        for cid in ranked[:10]:
            cov = topk_coverage(cs, [cid], 1)
            rel.append(1 if any(scorer.found(q, cov)) else 0)
        ideal = sorted(rel, reverse=True)
        dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel))
        idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal)) or 1.0
        res["ndcg@10"].append(dcg / idcg if any(rel) else 0.0)
    out = {}
    for t, res in per.items():
        if not res:
            continue
        out[t] = {"questions": len(res["ndcg@10"]), "evidence_items": len(res["items@2000"])}
        for key, vals in res.items():
            out[t][key] = round(sum(vals) / len(vals), 4) if vals else None
    # val에서 값을 고를 때는 답이 있는 질문 전부를 합친 값을 쓴다(§8 보완)
    pooled = defaultdict(list)
    for res in per.values():
        for key, vals in res.items():
            pooled[key] += vals
    out["all"] = {"questions": len(pooled["ndcg@10"]), "evidence_items": len(pooled["items@2000"])}
    out["all"].update({k: round(sum(v) / len(v), 4) for k, v in pooled.items() if v})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--ranker", choices=list(RANKERS), default="bm25")
    ap.add_argument("--split", choices=["train", "val", "test", "check"], default="val")
    ap.add_argument("--frozen", help="test를 돌릴 때 설정을 고정한 결정 기록 번호")
    args = ap.parse_args()
    if args.split in FROZEN_SPLITS and not args.frozen:
        raise SystemExit("test는 설정을 고정한 결정 기록(--frozen)이 있어야 돈다")
    from jikgam.ingest.chunk import load

    docs = load_docs()
    cs = ChunkSet(args.chunks, load(args.chunks), docs)
    result = evaluate(cs, load_questions(args.split), RANKERS[args.ranker], Scorer(docs))
    print(json.dumps({"chunks": args.chunks, "ranker": args.ranker, "split": args.split, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()


def per_question_found(cs: ChunkSet, questions: list[dict], rankings: dict[str, list[str]], scorer: Scorer,
                       budget: int = 2000) -> dict[str, dict[str, list[bool]]]:
    """유형 → 질문 id → 근거 항목별 찾음(짝지은 부트스트랩용). 내용은 없고 id와 참거짓만 담는다."""
    out: dict[str, dict[str, list[bool]]] = {"single": {}, "multi": {}}
    for q in questions:
        if q["type"] == "none":
            continue
        out[q["type"]][q["id"]] = scorer.found(q, budget_coverage(cs, rankings[q["id"]], budget))
    return out


def group_metrics(cs, questions, rankings, scorer):
    """유형별과 같은 지표를 질문의 묶음별로 집계한다."""
    return {g: evaluate_rankings(cs, [q for q in questions if q["group"] == g], rankings, scorer)["all"]
            for g in sorted({q["group"] for q in questions})}


def word_overlap(questions, docs):
    """정답 근거가 있는 질문의 공백 제외 두 글자 조각 겹침을 질문마다 계산한다."""
    values = []
    for q in questions:
        if q["type"] == "none":
            continue
        gold = "".join(docs[a["doc"]].text[a["start"]:a["end"]]
                       for ev in q["evidence"] for a in ev["alternatives"][:1])
        query = "".join(c for c in q["question"] if not c.isspace())
        gold = "".join(c for c in gold if not c.isspace())
        grams = [query[i:i + 2] for i in range(len(query) - 1)]
        if grams:
            values.append((q["type"], q["group"], sum(g in gold for g in grams) / len(grams)))

    def mean(rows):
        return {"questions": len(rows), "overlap": round(sum(rows) / len(rows), 4) if rows else None}

    return {"all": mean([v for _, _, v in values]),
            "by_type": {t: mean([v for kind, _, v in values if kind == t]) for t in ("single", "multi")},
            "by_group": {g: mean([v for _, group, v in values if group == g]) for g in sorted({q["group"] for q in questions})},
            "excluded_none": sum(q["type"] == "none" for q in questions)}
