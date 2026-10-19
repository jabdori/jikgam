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


def test_session_asks_law_applies_for_law_chunks_only(tmp_path):
    from dataclasses import replace

    from jikgam.engine.org import load_org
    from jikgam.engine.session import Session
    from jikgam.eval.questions import render

    s = Session(replace(load_org("org_a"), law=load_law("r0")), "t", tmp_path)
    law_id = next(a for a, x in s.articles.items() if x.type == LAW_TYPE)
    assert s._applies_qid(law_id) == f"law_applies:{law_id}"
    assert s._applies_qid("A-05") == "applies:A-05"
    q = render([s._applies_qid(law_id)], s.bank)[f"law_applies:{law_id}"]
    assert law_id in q["instructions"] and q["type"] == "noul"
    v1 = Session(load_org("org_a"), "t1", tmp_path)  # 법령 묶음이 없으면 질문 세트도 v1 그대로다
    assert "law_applies" not in v1.bank


def test_r5_profile_adds_filtering_and_leaves_r0_r4_unchanged():
    r0, r4, r5 = load_law("r0"), load_law("r4"), load_law("r5")
    for p in (r0, r4):
        assert (p.labels, p.topic_question_set, p.reserved) == (None, None, 0)
    assert (r5.mode, r5.chunks, r5.meta, r5.embed, r5.fusion, r5.max_candidates) == ("r5", r4.chunks, r4.meta, r4.embed, r4.fusion, 8)
    assert r5.labels.exists() and r5.topic_question_set.exists() and (r5.reserved, r5.topic_threshold) == (2, 0.5)


def test_r5_filter_keeps_sector_and_topic_and_reserves_two_unfiltered_slots():
    from jikgam.engine.law import filter_candidates

    ranked = [f"x{i}" for i in range(1, 13)]
    labels = {c: {"적용 업종": ["업종 공통"], "주제": ["청약철회"]} for c in ranked}
    labels["x1"] = {"적용 업종": ["온라인게임"], "주제": ["청약철회"]}      # 업종 밖
    labels["x2"] = {"적용 업종": ["업종 공통"], "주제": ["개인정보 보호"]}   # 주제 밖
    ids, info = filter_candidates(ranked, labels, ("업종 공통",), "청약철회", 2, 8)
    assert ids[:6] == ["x3", "x4", "x5", "x6", "x7", "x8"]  # 거른 순위 앞 6개
    assert ids[6:] == ["x1", "x2"]  # 거르지 않은 순위에서 아직 없는 앞 2개
    assert info == {"filtered": True, "topic": "청약철회", "kept": 10}
    ids, info = filter_candidates(ranked, labels, ("업종 공통",), None, 2, 8)  # 주제가 없으면 업종만
    assert ids[:6] == ["x2", "x3", "x4", "x5", "x6", "x7"] and ids[6:] == ["x1", "x8"]
    assert filter_candidates(ranked, labels, (), None, 2, 8) == (ranked[:8], {"filtered": False, "topic": None, "kept": 12})


def test_r5_session_asks_the_topic_in_the_first_judge_request_only(tmp_path):
    import json
    from dataclasses import replace

    from jikgam.engine.org import load_org
    from jikgam.engine.session import Session
    from jikgam.eval.questions import render

    # 임베딩 없이 가볍게: R0 구간 세트로 r5 동작만 본다
    labels = tmp_path / "labels.json"
    r0 = load_law("r0")
    chunk = next(iter(bundle(r0).articles))
    labels.write_text(json.dumps({"segments": {chunk: {"적용 업종": ["업종 공통"], "주제": ["청약철회"]}}}, ensure_ascii=False),
                      encoding="utf-8")
    cfg = replace(load_law("r5"), profile="r5-test", chunks=r0.chunks, meta="none", embed=None, labels=labels)
    s = Session(replace(load_org("org_a"), law=cfg), "t", tmp_path)
    assert "law_topic" in s.bank and s.law.topics["t1"] and s.law.labels[chunk]["주제"] == ["청약철회"]
    assert "law_topic" in s._question_ids([], True, None)
    assert "law_topic" not in s._question_ids([], False, None)  # 같은 턴의 재판별에는 묻지 않는다
    q = render(["law_topic"], s.bank)["law_topic"]
    assert q["type"] == "choice" and len(q["criteria"]) == len(s.law.topics) == 20
    query = s.articles[chunk].text[:60]  # 키워드 검색만 쓰므로 그 구간이 순위에 들도록 그 본문으로 찾는다
    ids, _, info = s.law.search_filtered(query, s.org.law_industries, "청약철회")
    assert ids[0] == chunk and info["filtered"] and info["kept"] == 1
    plain = Session(replace(load_org("org_a"), law=r0), "t0", tmp_path)
    assert "law_topic" not in plain.bank and "law_topic" not in plain._question_ids([], True, None)


def test_org_setting_can_turn_on_a_law_profile(tmp_path):
    from jikgam.engine.org import ROOT, load_org

    assert load_org("org_a").law is None  # 기본은 꺼져 있다
    text = (ROOT / "config/orgs/org_a.toml").read_text(encoding="utf-8").replace('# law_profile = "r5"', 'law_profile = "r0"')
    path = tmp_path / "org_x.toml"
    path.write_text(text, encoding="utf-8")
    org = load_org(path)
    assert org.law is not None and org.law.profile == "r0" and "디지털콘텐츠" in org.law_industries
