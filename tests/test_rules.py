"""규칙 기준선(jikgam.decision.rules)의 형식 검사, 확률 표 검사, train 정답률 보고.

    uv run pytest tests/test_rules.py -q        # 검사
    uv run pytest tests/test_rules.py -q -s     # 질문 유형별 train 정답률도 출력
    uv run python tests/test_rules.py           # 정답률과 다시 센 BRANCH_COUNTS·BASE_COUNTS 출력

train 분할만 읽는다. 튜닝과 표 계산에 val·test를 쓰지 않는다.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from jikgam.decision import rules
from jikgam.decision.types import label_and_prob

TRAIN = Path(__file__).resolve().parent.parent / "data/build/train.jsonl"
ROWS = [json.loads(line) for line in TRAIN.read_text(encoding="utf-8").splitlines() if line.strip()]
QTYPES = ("intent", "condition_changed", "applies", "sufficient", "next_info", "suggestion_valid", "action")


def _parse(row: dict[str, Any]) -> tuple[dict, dict, dict]:
    return json.loads(row["state"]), json.loads(row["questions"]), json.loads(row["gold"])


def count_train(rows: list[dict[str, Any]]) -> tuple[dict, dict]:
    """train으로 (분기별 (맞힌 수, 쓰인 수), choice 질문의 정답 분포)를 rules의 표와 같은 형태로 센다."""
    branches: dict[str, dict[str, list[int]]] = {}
    base: dict[str, dict[str, int]] = {}
    for row in rows:
        state, questions, gold = _parse(row)
        for qid, (label, branch) in rules.rule_branches(state, questions).items():
            qtype, answer = qid.partition(":")[0], gold[qid]["label"]
            kn = branches.setdefault(qtype, {}).setdefault(f"{label}/{branch}", [0, 0])
            kn[0], kn[1] = kn[0] + (label == answer), kn[1] + 1
            if questions[qid]["type"] == "choice":
                dist = base.setdefault(qtype, {})
                dist[answer] = dist.get(answer, 0) + 1
    return ({q: {b: tuple(v) for b, v in sorted(d.items())} for q, d in sorted(branches.items())},
            {q: dict(sorted(d.items())) for q, d in sorted(base.items())})


def train_accuracy(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """decide 결과에서 확률이 가장 큰 라벨(types.label_and_prob)의 질문 유형별 (정답 수, 질문 수)."""
    acc = {q: [0, 0] for q in QTYPES}
    for row in rows:
        state, questions, gold = _parse(row)
        for qid, answer in rules.decide(state, questions).items():
            kn = acc[qid.partition(":")[0]]
            kn[0], kn[1] = kn[0] + (label_and_prob(answer)[0] == gold[qid]["label"]), kn[1] + 1
    return {q: (k, n) for q, (k, n) in acc.items()}


def report(rows: list[dict[str, Any]]) -> str:
    lines = [f"train {len(rows)}건, 질문 유형별 정답 (규칙을 train을 보며 만들었으므로 표본 내 수치)"]
    lines += [f"  {q:<18} {k}/{n} ({k / n:.0%})" for q, (k, n) in train_accuracy(rows).items() if n]
    return "\n".join(lines)


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_answer_format(row: dict[str, Any]) -> None:
    state, questions, _ = _parse(row)
    answers = rules.decide(state, questions)
    assert set(answers) == set(questions)
    for qid, q in questions.items():
        a = answers[qid]
        assert a["type"] == q["type"], qid
        if q["type"] == "noul":
            assert 0.0 <= a["noul"] <= 1.0, qid
        else:
            probs = a["probabilities"]
            assert a["choice"] in q["criteria"], qid
            assert set(probs) == set(q["criteria"]), qid
            assert all(0.0 <= p <= 1.0 for p in probs.values()), qid
            assert sum(probs.values()) == pytest.approx(1.0), qid
            assert probs[a["choice"]] == max(probs.values()), qid


def test_decide_is_pure() -> None:
    state, questions, _ = _parse(ROWS[0])
    before = copy.deepcopy(state)
    assert rules.decide(state, questions) == rules.decide(state, questions)
    assert state == before


def test_unknown_question_is_rejected() -> None:
    state, _, _ = _parse(ROWS[0])
    with pytest.raises(ValueError):
        rules.decide(state, {"mystery": {"type": "noul", "instructions": ""}})


@pytest.mark.parametrize(("start", "today", "paid", "promo", "expected"), [
    # 별표 2(T-02)의 계산 예시
    ((2026, 7, 15), "2026-10-12", 99000, False, 62370),  # 3개월, 위약금 6,930원
    ((2026, 7, 15), "2026-10-12", 79000, True, 44370),  # 프로모션도 정상 월 요금으로 차감
    ((2026, 6, 15), "2026-09-12", 99000, False, 69300),  # 2026-06-30 이전 결제: 위약금 없음
    ((2026, 7, 15), "2027-06-01", 99000, False, 0),  # 11개월: 차감액이 결제 금액보다 크다
])
def test_refund_estimate_matches_table_2(start, today, paid, promo, expected) -> None:
    c = rules.SimpleNamespace(quote=None, refund_topic=True, suspended=False, duplicate=False, dup_only=False,
                              plan="annual", pay=start, today=rules.date.fromisoformat(today), amount=paid,
                              promo=promo, before_addendum=start[:2] <= rules.ADDENDUM_LAST_MONTH)
    assert rules._refund_estimate(c) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("칠월 십오일이었어요", [(2026, 7, 15)]),
    ("팔월 이십일? 그쯤이었던", [(2026, 8, 20)]),
    ("지난달 이십오일에 두 번", [(2026, 9, 25)]),
    ("이번 달 오일에 결제된 거", [(2026, 10, 5)]),
    ("유월 말에 했으니까", [(2026, 6, None)]),
    ("공칠년 삼월생인데", []),  # 생년월일은 결제 시점이 아니다
])
def test_text_dates(text, expected) -> None:
    assert rules._text_dates(text, rules.date(2026, 10, 12)) == expected


def test_negated_request_is_excluded() -> None:
    """docs/data-format.md의 intent 예시."""
    assert rules._intent(["해지하려는 건 아니고요, 이번 달 거만 돌려받고 싶어요"]) == "refund"


def test_unseen_branch_backs_off_to_same_label() -> None:
    """train에 없는 분기는 같은 라벨을 낸 분기들의 합계를, 그 라벨도 없으면 0.5를 쓴다."""
    same = [kn for key, kn in rules.BRANCH_COUNTS["sufficient"].items() if key.startswith("false/")]
    k, n = sum(k for k, _ in same), sum(n for _, n in same)
    answer = rules._to_answer("sufficient", {"type": "noul"}, "false", "never_seen")
    assert answer["noul"] == pytest.approx(1 - (k + 1) / (n + 2))
    question = json.loads(ROWS[0]["questions"])["intent"]
    unseen = rules._to_answer("intent", question, "unclear", "none")
    assert unseen["probabilities"]["unclear"] == pytest.approx(0.5)


def test_probability_tables_match_train() -> None:
    branches, base = count_train(ROWS)
    hint = "rules.py의 표가 train과 다르다. `uv run python tests/test_rules.py` 출력으로 바꾼다."
    assert branches == rules.BRANCH_COUNTS, hint
    assert base == rules.BASE_COUNTS, hint


def test_train_accuracy_report() -> None:
    acc = train_accuracy(ROWS)
    assert all(n > 0 for _, n in acc.values())
    print("\n" + report(ROWS))


if __name__ == "__main__":
    import sys
    from pprint import pformat

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(report(ROWS))
    branch_counts, base_counts = count_train(ROWS)
    print("\nBRANCH_COUNTS =", pformat(branch_counts, width=110))
    print("BASE_COUNTS =", pformat(base_counts, width=110))
