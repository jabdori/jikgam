"""판별 평가 지표. 정의는 docs/evaluation.md를 따른다."""

import math


def percentile(values: list[float], q: float) -> float:
    """선형 보간 백분위수(numpy 기본값과 같음). q는 0~100."""
    if not values:
        return math.nan
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def ece(confidences: list[float], correct: list[bool], n_bins: int = 10) -> float:
    """top-label ECE. 예측 라벨의 확률을 [0,1] 등간격 10구간으로 나눠, 구간별 |정확도 − 평균 확률|을 표본 수로 가중 평균한다.
    확률 1.0은 마지막 구간에 넣는다."""
    if not confidences:
        return math.nan
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for p, c in zip(confidences, correct):
        bins[min(int(p * n_bins), n_bins - 1)].append((p, c))
    total = len(confidences)
    err = 0.0
    for b in bins:
        if b:
            acc = sum(c for _, c in b) / len(b)
            conf = sum(p for p, _ in b) / len(b)
            err += len(b) / total * abs(acc - conf)
    return err


def balanced_accuracy(pred: list[str], gold: list[str]) -> float:
    """noul 질문용. true 재현율과 false 재현율의 평균. 한쪽 정답이 없으면 있는 쪽만 쓴다."""
    rates = []
    for cls in ("true", "false"):
        idx = [i for i, g in enumerate(gold) if g == cls]
        if idx:
            rates.append(sum(pred[i] == cls for i in idx) / len(idx))
    return sum(rates) / len(rates) if rates else math.nan
