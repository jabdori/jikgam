"""v2 보고용 통계(docs/evaluation-v2.md §4.1, §4.2, §6).

- 경계 일치(§4.1, 보고만): 덩어리 안의 경계(덩어리 첫 줄 제외)를 비교한다. 예측과 참고 경계를 거리가 가까운 순서로
  한 쌍씩 짝짓고 한 줄 차이까지 맞은 것으로 본다. 정밀도·재현율·F1과 Pk, WindowDiff(창 = 참고 구간 평균 길이의 절반).
- 부트스트랩: 덩어리 단위(경계 일치), 질문 단위로 짝지은 차이(검색 재현율). 10,000회, 95% 구간.
"""

import random
from fractions import Fraction
from collections.abc import Callable, Sequence


def match(pred: Sequence[int], gold: Sequence[int], tol: int = 1) -> int:
    """거리가 가까운 쌍부터 한 쌍씩 짝지은 수(한 줄 차이까지)."""
    pairs = sorted((abs(p - g), p, g) for p in pred for g in gold if abs(p - g) <= tol)
    used_p, used_g, n = set(), set(), 0
    for _, p, g in pairs:
        if p not in used_p and g not in used_g:
            used_p.add(p)
            used_g.add(g)
            n += 1
    return n


def prf(tp: int, n_pred: int, n_gold: int) -> dict[str, float | None]:
    p = tp / n_pred if n_pred else None
    r = tp / n_gold if n_gold else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"precision": None if p is None else round(p, 4), "recall": None if r is None else round(r, 4),
            "f1": None if f is None else round(f, 4)}


def segments_of(bounds: set[int], start: int, end: int) -> list[int]:
    """줄 start~end에서 줄마다 속한 구간 번호."""
    out, k = [], 0
    for n in range(start, end + 1):
        if n in bounds and n != start:
            k += 1
        out.append(k)
    return out


def pk_windowdiff(pred: set[int], gold: set[int], start: int, end: int, k: int) -> tuple[float, float]:
    sp, sg = segments_of(pred, start, end), segments_of(gold, start, end)
    n = len(sp)
    if n <= k:
        return 0.0, 0.0
    pk_err = wd_err = 0
    for i in range(n - k):
        same_p, same_g = sp[i] == sp[i + k], sg[i] == sg[i + k]
        pk_err += same_p != same_g
        wd_err += abs((sp[i + k] - sp[i]) - (sg[i + k] - sg[i])) > 0
    return pk_err / (n - k), wd_err / (n - k)


def boundary_agreement(segs: list[dict], pred: dict[str, list[int]], gold: dict[str, list[int]]) -> dict:
    """segs: [{"id","start","end"}]. pred/gold: 덩어리 id → 덩어리 안의 경계 줄(첫 줄 제외)."""
    tp = n_pred = n_gold = 0
    lens = []
    for s in segs:
        g = [x for x in gold[s["id"]] if s["start"] < x <= s["end"]]
        cuts = [s["start"]] + g + [s["end"] + 1]
        lens += [b - a for a, b in zip(cuts, cuts[1:])]
    k = max(1, round(sum(lens) / len(lens) / 2)) if lens else 1
    per = {}
    pks, wds = [], []
    for s in segs:
        p = [x for x in pred.get(s["id"], []) if s["start"] < x <= s["end"]]
        g = [x for x in gold[s["id"]] if s["start"] < x <= s["end"]]
        m = match(p, g)
        tp, n_pred, n_gold = tp + m, n_pred + len(p), n_gold + len(g)
        pk, wd = pk_windowdiff(set(p), set(g), s["start"], s["end"], k)
        pks.append(pk)
        wds.append(wd)
        per[s["id"]] = (m, len(p), len(g))
    exact = sum(match([x for x in pred.get(s["id"], [])], gold[s["id"]], tol=0) for s in segs)
    out = {**prf(tp, n_pred, n_gold), "exact": prf(exact, n_pred, n_gold), "pk": round(sum(pks) / len(pks), 4),
           "windowdiff": round(sum(wds) / len(wds), 4), "window": k, "pred": n_pred, "gold": n_gold}
    # 덩어리 단위 부트스트랩(F1)
    ids = [s["id"] for s in segs]

    def f1_of(sample):
        a = sum(per[i][0] for i in sample)
        b = sum(per[i][1] for i in sample)
        c = sum(per[i][2] for i in sample)
        return prf(a, b, c)["f1"] or 0.0

    out["f1_ci95"] = bootstrap_ci(ids, f1_of)
    return out


def bootstrap_ci(units: list, stat: Callable[[list], float], n: int = 10000, seed: int = 20261016) -> list[float]:
    rng = random.Random(seed)
    vals = sorted(stat([units[rng.randrange(len(units))] for _ in units]) for _ in range(n))
    return [vals[int(0.025 * n)], vals[int(0.975 * n) - 1]]


def paired_recall_diff(per_q_a: dict[str, list[bool]], per_q_b: dict[str, list[bool]], n: int = 10000,
                       seed: int = 20261016) -> dict:
    """질문마다 근거 항목별 찾음 목록(a, b 같은 질문). 항목을 합친 재현율의 차이(a − b)와 질문 단위 짝지은 부트스트랩 구간."""
    qs = sorted(set(per_q_a) & set(per_q_b))

    def rec(sample, d):
        items = [x for q in sample for x in d[q]]
        return Fraction(sum(items), len(items)) if items else Fraction(0)

    point = rec(qs, per_q_a) - rec(qs, per_q_b)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        s = [qs[rng.randrange(len(qs))] for _ in qs]
        diffs.append(float(rec(s, per_q_a) - rec(s, per_q_b)))
    diffs.sort()
    return {"diff": float(point), "ci95": [diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1]],
            "questions": len(qs)}
