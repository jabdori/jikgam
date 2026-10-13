import math

from jikgam.decision.types import label_and_prob
from jikgam.eval.metrics import balanced_accuracy, ece, percentile


def test_percentile_matches_linear_interpolation():
    xs = [10, 20, 30, 40, 50]
    assert percentile(xs, 50) == 30
    assert percentile(xs, 95) == 48
    assert percentile([7], 95) == 7
    assert math.isnan(percentile([], 50))


def test_ece_perfectly_calibrated_and_overconfident():
    # 확률 0.75로 4개 중 3개 정답 → 오차 0
    assert ece([0.75] * 4, [True, True, True, False]) == 0
    # 확률 1.0으로 2개 중 1개 정답 → 오차 0.5
    assert abs(ece([1.0, 1.0], [True, False]) - 0.5) < 1e-12


def test_ece_weights_bins_by_count():
    conf = [0.95, 0.95, 0.55, 0.55]
    corr = [True, True, False, False]
    # 0.9 구간: |1 − 0.95| = 0.05, 0.5 구간: |0 − 0.55| = 0.55 → 평균 0.3
    assert abs(ece(conf, corr) - 0.3) < 1e-12


def test_balanced_accuracy_catches_always_true():
    gold = ["true"] * 6 + ["false"] * 4
    assert balanced_accuracy(["true"] * 10, gold) == 0.5


def test_label_and_prob():
    assert label_and_prob({"type": "noul", "noul": 0.2}) == ("false", 0.8, {"true": 0.2, "false": 0.8})
    label, p, _ = label_and_prob({"type": "choice", "choice": "b", "probabilities": {"a": 0.5, "b": 0.5}})
    assert (label, p) == ("b", 0.5)
