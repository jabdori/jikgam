"""v2 엔드투엔드의 법령·고시 묶음(docs/evaluation-v2.md §4.4, config/v2/law.toml)."""

from datetime import date

from jikgam.engine.generate import Allowed, GenerationResult, check
from jikgam.engine.law import LAW_TYPE, bundle, guard_violations, load_law
from jikgam.sim.judge import law_problems


def guards():
    return [{"id": g.id, "pattern": g.pattern.pattern, "min": g.min, "message": g.message} for g in load_law("r0").guards]


def test_profiles_load_and_r4_uses_the_val_choice():
    r0, r4 = load_law("r0"), load_law("r4")
    assert (r0.mode, r0.chunks, r0.max_candidates) == ("r0", "article256", 8)
    assert (r4.mode, r4.chunks, r4.meta, r4.embed, r4.fusion) == ("r4", "b-W4000-C800-raw", "full", "snow", "rrf")


def test_withdrawal_guard_flags_periods_shorter_than_the_legal_floor():
    assert guard_violations("청약철회는 결제일부터 3일 안에만 가능합니다.", guards())
    assert not guard_violations("청약철회는 결제일로부터 7일 이내에 할 수 있습니다.", guards())
    assert not guard_violations("환불은 3영업일 안에 처리됩니다.", guards())  # 청약철회 기간이 아니다


def test_generation_check_adds_law_guard_only_when_configured():
    r = GenerationResult("청약철회는 결제 후 5일까지만 됩니다.", [], None, False, "m")
    base = Allowed(frozenset(), frozenset(), frozenset())
    assert check(r, base, date(2026, 10, 12)) == []
    with_law = Allowed(frozenset(), frozenset(), frozenset(), law_guards=tuple(guards()))
    assert any("7일" in p for p in check(r, with_law, date(2026, 10, 12)))


def test_r0_bundle_returns_law_articles_for_a_query():
    b = bundle(load_law("r0"))
    ids, ms = b.search("인터넷으로 산 물건 청약철회는 며칠 안에 할 수 있나요")
    assert 0 < len(ids) <= 8 and ms >= 0
    a = b.articles[ids[0]]
    assert a.type == LAW_TYPE and a.title.startswith("「") and a.text


def test_judge_law_problems_use_half_coverage_of_gold_spans():
    b = bundle(load_law("r0"))
    cid = next(iter(b.articles))
    c = b.cs.by_id[cid]
    gold = [{"alternatives": [{"doc": c.doc, "start": c.start, "end": c.end}]}]
    assert law_problems(gold, {cid}) == []
    assert law_problems(gold, set())  # 근거 집합에 없으면 문제다
