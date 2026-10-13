import asyncio
import dataclasses
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from jikgam.engine import events as ev
from jikgam.engine.bizapi import ToolResult
from jikgam.engine.generate import GenerationResult, allowed_from, check, generate_checked, refund_claims, sanitize
from jikgam.engine.org import load_org
from jikgam.engine.retrieval import Article, ArticleIndex, RetrievalContext, parse_terms, tokens
from jikgam.engine.state import SessionState
from jikgam.eval.questions import load_bank, render

ROOT = Path(__file__).resolve().parents[1]
ORG = load_org("org_a")
TODAY = date(2026, 10, 12)


# --- 이벤트 ---

def test_parse_utterance_defaults_and_validation():
    u = ev.parse_utterance({"type": "utterance", "utt_id": "u1", "text": " 안녕하세요 "}, 12.5)
    assert (u.speaker, u.text, u.final, u.t_end_ms, u.marker) == ("customer", "안녕하세요", True, 12.5, None)
    u = ev.parse_utterance({"type": "utterance", "utt_id": "u2", "text": "아", "final": False, "t_end_ms": 3,
                            "marker": {"condition_change": True}}, 99.0)
    assert (u.final, u.t_end_ms, u.marker) == (False, 3.0, {"condition_change": True})
    for bad in ({"type": "x", "utt_id": "u", "text": "a"}, {"type": "utterance", "text": "a"},
                {"type": "utterance", "utt_id": "u", "text": " "}, {"type": "utterance", "utt_id": "u", "text": "a",
                                                                   "speaker": "bot"},
                {"type": "utterance", "utt_id": "u", "text": "a", "final": "yes"}):
        with pytest.raises(ValueError):
            ev.parse_utterance(bad, 0.0)


def test_monotonic_clock_and_wall():
    a, b = ev.now_ms(), ev.now_ms()
    assert b >= a
    assert ev.wall_at(1000.0) > ev.wall_at(0.0)


def test_turn_latencies_measures_first_refresh_at_or_after_version():
    log = [
        {"type": ev.UTTERANCE_END, "t_ms": 100.0, "version": 3, "utt_id": "u4", "speaker": "customer", "text": "x",
         "marker": {"condition_change": True}},
        {"type": ev.VERSION_BUMPED, "t_ms": 100.1, "version": 4, "to": 4, "utt_id": "u4"},
        {"type": ev.EVIDENCE_UPDATED, "t_ms": 150.0, "version": 3},  # 지난 버전(취소 직전 경합)은 세지 않는다
        {"type": ev.RETRIEVAL_DONE, "t_ms": 101.0, "version": 4, "duration_ms": 0.5},
        {"type": ev.DECISION_RESPONSE, "t_ms": 400.0, "version": 4, "latency_ms": 290.0},
        {"type": ev.EVIDENCE_UPDATED, "t_ms": 401.0, "version": 4},
        {"type": ev.TOOL_RESULT, "t_ms": 410.0, "version": 4, "tool": "quote_refund", "latency_ms": 4.0},
        {"type": ev.GENERATION_RESPONSE, "t_ms": 3000.0, "version": 5, "latency_ms": 2500.0},
        {"type": ev.AGENT_SAY, "t_ms": 3001.0, "version": 5, "text": "안내"},
        {"type": ev.UTTERANCE_END, "t_ms": 5000.0, "version": 5, "utt_id": "u5", "speaker": "customer", "text": "네"},
    ]
    t = ev.turn_latencies(log)
    assert t[0]["evidence_ms"] == 301.0 and t[0]["say_ms"] == 2901.0 and t[0]["decision_ms"] == [290.0]
    assert t[0]["marker"] == {"condition_change": True} and t[0]["tool_ms"] == [("quote_refund", 4.0)]
    assert t[1]["evidence_ms"] is None  # 다음 발화나 세션 끝까지 갱신이 없으면 미갱신


# --- 검색 ---

def test_parse_terms_matches_build_script():
    spec = importlib.util.spec_from_file_location("build_dataset", ROOT / "scripts/build_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ref = mod.parse_terms(ROOT / "domain/org_a/terms.md")
    mine = parse_terms(ORG.terms)
    assert set(ref) == set(mine) and len(mine) == 23
    for aid, a in ref.items():
        assert (mine[aid].title, mine[aid].type, list(mine[aid].refs), mine[aid].text) == \
            (a["title"], a["type"], a["refs"], a["text"])


def test_tokens_are_bigrams_within_words():
    assert tokens("해지하고 환불") == ["해지", "지하", "하고", "환불"]
    assert tokens("9,900원 A") == ["9900", "원", "a"]


def test_modifiers_are_exceptions_addenda_tables_that_refer_to_the_article():
    index = ArticleIndex(parse_terms(ORG.terms), ORG.retrieval)
    assert set(index.modifiers["A-08"]) == {"A-11", "A-16", "A-21", "T-02"}
    assert set(index.modifiers["A-05"]) == {"A-06", "A-09", "A-12", "A-16"}


def test_search_structure_and_rules():
    index = ArticleIndex(parse_terms(ORG.terms), ORG.retrieval)
    r = index.search(RetrievalContext(("연간 결제한 거 해지하고 남은 돈 돌려받고 싶어요",), intent=None))
    assert len(r.candidates) == ORG.retrieval.max_candidates
    assert {"A-08", "A-15"} <= set(r.candidates) and r.sources["A-15"] == "rule"  # 본인 확인 전 절차 조항
    # 업무 API가 근거로 준 조항이 먼저, 그다음 상태 규칙 조항. 본인 확인 뒤에는 제15조를 붙이지 않는다.
    r = index.search(RetrievalContext(("네",), intent="cancel_refund", identity_verified=True,
                                      system_article_ids=("A-09",)))
    assert r.candidates[:3] == ["A-09", "A-14", "A-20"] and r.sources.get("A-15") != "rule"


def test_structure_expansion_attaches_modifiers_and_refs():
    arts = {"G": Article("G", "제1조 (청약철회)", "general", ("R",), "결제 후 청약철회를 할 수 있다"),
            "E": Article("E", "제2조 (제한)", "exception", ("G",), "이용 이력이 있으면 제외한다"),
            "R": Article("R", "제3조 (방법)", "procedure", (), "원래 수단으로 돌려준다"),
            "X": Article("X", "제4조 (요금)", "general", (), "요금표와 같다")}
    cfg = dataclasses.replace(ORG.retrieval, rules=(), expansions=(), intent_terms={}, max_candidates=3, seed_top_k=1)
    r = ArticleIndex(arts, cfg).search(RetrievalContext(("청약철회 하고 싶어요",)))
    assert r.candidates == ["G", "E", "R"] and r.sources == {"G": "bm25", "E": "modifies:G", "R": "ref:G"}
    assert abs(r.scores["E"] - r.scores["G"] * cfg.expansion_decay) < 0.01


# --- 생성 검사 ---

PAYLOAD = {
    "plan": {"action": "answer", "ask": None},
    "articles": [{"id": "A-19", "type": "procedure", "text": "환불 예정액이 50,000원을 넘는 경우"},
                 {"id": "T-02", "type": "table", "text": "| 99,000원 | 3 | 29,700원 | 6,930원 | 62,370원 |"}],
    "quotes": {"annual_cancel": {"status": "eligible", "refund_amount": 53280, "refunds": [{"amount": 53280}],
                                 "breakdown": {"annual_cancel": {"deduction": 19800, "penalty": 5920}}},
               "withdrawal": {"status": "not_eligible", "refund_amount": 0}},
    "system_facts": {"payments": [{"amount": 79000}]},
}


def test_allowed_amounts_exclude_table_examples():
    allowed = allowed_from(PAYLOAD, TODAY)
    assert {53280, 19800, 5920, 79000, 50000} <= allowed.amounts and 62370 not in allowed.amounts
    assert allowed.quote_amounts == {53280}
    ok = GenerationResult("환불 예정액은 53,280원입니다. 5만 원을 넘어 승인 후 확정됩니다.", ["A-19", "A-99"], 53280, True, "m")
    assert check(ok, allowed, TODAY) == []
    sanitize(ok, allowed)
    assert ok.cited_articles == ["A-19"] and ok.amount_final
    bad = GenerationResult("예시처럼 62,370원이 환불됩니다.", [], 62370, True, "m")
    assert len(check(bad, allowed, TODAY)) == 2
    none = GenerationResult("환불 대상이 아니라 환불 예정액은 0원입니다.", [], 0, True, "m")
    assert check(none, allowed, TODAY) == []
    sanitize(none, allowed)
    assert (none.amount, none.amount_final) == (None, False)  # 환불 없음은 확정 금액 안내가 아니다


class _BadThenGood:
    model = "fake"

    def __init__(self, texts):
        self.texts = list(texts)
        self.feedback = []

    async def generate(self, payload, feedback=None):
        self.feedback.append(feedback)
        return GenerationResult(self.texts.pop(0), [], None, False, "fake", latency_ms=10.0)


def test_generate_checked_retries_with_feedback_then_falls_back():
    g = _BadThenGood(["대략 60,000원쯤 됩니다.", "환불 예정액은 53,280원입니다."])
    r = asyncio.run(generate_checked(g, PAYLOAD, TODAY))
    assert r.say == "환불 예정액은 53,280원입니다." and r.attempts == 2 and not r.fallback
    assert g.feedback[0] is None and "60,000원" in g.feedback[1] and r.latency_ms == 20.0
    g = _BadThenGood(["대략 60,000원쯤 됩니다.", "아마 61,000원일 거예요."])
    r = asyncio.run(generate_checked(g, PAYLOAD, TODAY))
    assert r.fallback and "원" not in r.say and r.amount is None and len(r.violations) == 2


def test_refund_claims_detect_conditional_and_plain_claims_but_not_negations():
    claims = ["원하시면 해지하고 전액 환불받으실 수 있습니다.", "연장 보상을 원하지 않으시면 해지하고 환불받으실 수 있습니다.",
              "환불이 가능합니다.", "해지하시면 9,900원 전액 환불해 드립니다.", "청약철회로 환불됩니다.", "환불 대상입니다.",
              "돌려받으실 수 있어요.", "환불받으실 수 있지만 위약금이 있습니다.", "남은 금액은 돌려드립니다.",
              "확인 후 환불해 드리겠습니다."]
    not_claims =["환불되지 않습니다.", "환불은 불가합니다.", "환불해 드릴 수 없습니다.", "환불이 어렵습니다.",
                  "환불 가능 여부를 확인하기 위해 본인 확인이 필요합니다.", "환불이 가능한지 확인해 드리겠습니다.",
                  "해지·환불은 계정 소유자 본인만 신청하실 수 있습니다.", "환불 대상에 해당하지 않습니다.",
                  "이미 결제한 부가 상품 요금은 환불되지 않습니다.", "환불은 원래 결제 수단으로 진행되며 3~7영업일이 걸립니다.",
                  # 개발용 재생 s20(try1)에서 위반으로 잘못 잡은 문장: 규정 이름("장애 전액 환불")과 기한 경과
                  "월 구독은 제7조 제3항에 따라 이미 결제된 요금이 환불되지 않으며, 제5조 제1항의 청약철회 기간과 제9조 "
                  "제2항의 장애 전액 환불 신청 기간(2026-10-09까지)이 지나 이번 달 요금은 환불되지 않습니다.",
                  "환불 가능 기간이 지났습니다.", "환불받을 수 있는 기간이 지났습니다.", "환불받는 방법은 결제 수단에 따라 다릅니다."]
    assert all(refund_claims(s) for s in claims), [s for s in claims if not refund_claims(s)]
    assert not any(refund_claims(s) for s in not_claims), [s for s in not_claims if refund_claims(s)]


def test_refund_claim_without_quote_is_a_violation_only_for_account_cases():
    """s21: 연장 보상을 안내하며 견적 없이 '해지하면 전액 환불'을 말하면 위반이다. 대상 견적이 있거나 계정과 무관한
    일반 안내면 위반이 아니다."""
    say = GenerationResult("연장 보상이 가능하며, 원하시면 해지하고 전액 환불받으실 수 있습니다.", [], None, False, "m")
    no_quote = {"plan": {"action": "answer"}, "articles": [], "quotes": {}, "system_facts": {"payments": [{"amount": 9900}]},
                "refund_claims_need_quote": True}
    problems = check(say, allowed_from(no_quote, TODAY), TODAY)
    assert len(problems) == 1 and "환불" in problems[0]
    zero = dict(no_quote, quotes={"withdrawal": {"status": "not_eligible"}, "trial_cancel": {"status": "zero"}})
    assert check(say, allowed_from(zero, TODAY), TODAY)  # 대상 아님·0원 견적은 근거가 아니다
    assert check(say, allowed_from(PAYLOAD, TODAY), TODAY) == []  # 대상 견적이 있다
    receipt = dict(no_quote, system_facts={"receipts": {"refund:P010": {"amount": 9900}}})
    assert check(say, allowed_from(receipt, TODAY), TODAY) == []
    general = dict(no_quote, refund_claims_need_quote=False)  # 가입 전 일반 문의: 약관 일반 내용으로 설명할 수 있다
    assert check(say, allowed_from(general, TODAY), TODAY) == []
    ok = GenerationResult("이용 기간 연장으로 보상해 드릴 수 있습니다. 환불 가능 여부는 따로 확인해야 합니다.", [], None, False, "m")
    assert check(ok, allowed_from(no_quote, TODAY), TODAY) == []


def test_generate_checked_regenerates_then_falls_back_on_unbacked_refund_claim():
    payload = {"plan": {"action": "answer", "ask": None}, "articles": [], "quotes": {}, "system_facts": {},
               "refund_claims_need_quote": True}
    g = _BadThenGood(["해지하시면 전액 환불됩니다.", "연장 보상으로 처리할 수 있습니다."])
    r = asyncio.run(generate_checked(g, payload, TODAY))
    assert r.say == "연장 보상으로 처리할 수 있습니다." and r.attempts == 2 and "환불" in g.feedback[1]
    g = _BadThenGood(["해지하시면 전액 환불됩니다.", "그래도 환불받으실 수 있어요."])
    r = asyncio.run(generate_checked(g, payload, TODAY))
    assert r.fallback and not refund_claims(r.say)


def test_deduction_amount_is_not_a_refund_amount():
    """s16: 요금제 변경 차감액(6,380원)은 문장에 쓸 수 있지만 amount·amount_final이 되지 않는다."""
    payload = {"plan": {"action": "answer"}, "articles": [], "system_facts": {}, "refund_claims_need_quote": True,
               "quotes": {"plan_change": {"status": "eligible", "kind": "deduction", "refund_amount": 6380,
                                          "breakdown": {"plan_change": {"annual_charge": 92620}}}}}
    allowed = allowed_from(payload, TODAY)
    assert allowed.quote_amounts == frozenset() and allowed.deduction_amounts == {6380}
    r = GenerationResult("남은 20일분 6,380원을 빼 92,620원을 결제합니다.", [], 6380, True, "m")
    assert check(r, allowed, TODAY) == []
    sanitize(r, allowed)
    assert (r.amount, r.amount_final) == (None, False)
    # 차감액 견적은 환불 근거가 아니다
    claim = GenerationResult("연 구독으로 바꾸시면 환불받으실 수 있습니다.", [], None, False, "m")
    assert check(claim, allowed, TODAY)


def test_must_mention_is_retried_once_then_kept_with_violation():
    """s03·s27: plan.must_mention이 빠지면 한 번 다시 만들고, 그래도 빠지면 고정 문구 대신 두 번째 결과를 쓴다."""
    payload = {"plan": {"action": "answer", "ask": None, "must_mention": ["승인"]}, "articles": [], "quotes": {},
               "system_facts": {}}
    g = _BadThenGood(["환불 접수를 진행할까요?", "상담사 승인 후 환불이 확정됩니다. 진행할까요?"])
    r = asyncio.run(generate_checked(g, payload, TODAY))
    assert r.attempts == 2 and "승인" in r.say and r.violations == ["'승인'을(를) 안내하지 않았다"]
    assert "승인" in g.feedback[1]
    g = _BadThenGood(["환불 접수를 진행할까요?", "접수를 진행할까요?"])
    r = asyncio.run(generate_checked(g, payload, TODAY))
    assert not r.fallback and r.say == "접수를 진행할까요?" and len(r.violations) == 2
    g = _BadThenGood(["상담사 승 인 후 확정됩니다."])  # 띄어쓰기는 보지 않는다
    assert asyncio.run(generate_checked(g, payload, TODAY)).attempts == 1


def test_bizapi_url_env_override(monkeypatch):
    monkeypatch.setenv("JIKGAM_BIZAPI_URL", "http://127.0.0.1:8083/")
    assert load_org("org_a").bizapi_url == "http://127.0.0.1:8083"
    monkeypatch.delenv("JIKGAM_BIZAPI_URL")
    assert load_org("org_a").bizapi_url == "http://127.0.0.1:8081"


def test_org_policy_config_for_caller_and_addon():
    p = ORG.policy
    assert p.third_party_articles == ("A-15",) and p.guardian_reasons == ("minor",)
    assert p.addon_articles == ("A-18",) and "cancel" in p.addon_intents
    assert ORG.caller_threshold < ORG.owner_return_threshold


# --- 상태 ---

def test_apply_tool_results_and_known_facts():
    st = SessionState("org_a", TODAY)
    st.apply_tool_result(ToolResult("verify_identity", {}, True, {"verified": True, "customer_id": "C010", "attempts": 1,
                                                                   "failed_attempts": 0}))
    st.apply_tool_result(ToolResult("quote_refund", {"reason": "withdrawal"}, False, error_code="NOT_ELIGIBLE",
                                    error={"code": "NOT_ELIGIBLE", "message": "이용 이력", "article_ids": ["A-05", "A-06"],
                                           "quote": {"reason": "withdrawal"}}))
    st.apply_tool_result(ToolResult("quote_refund", {"reason": "incident"}, False, error_code="TIMEOUT"))
    assert st.identity.verified and st.facts.quotes["withdrawal"].status == "not_eligible"
    assert st.facts.api_failures == {"quote_refund": "TIMEOUT"} and st.facts.quotes["incident"].status == "failed"
    st.apply_tool_result(ToolResult("create_cancellation", {}, False, error_code="DUPLICATE_REQUEST",
                                    error={"code": "DUPLICATE_REQUEST", "receipt": {"subscription_id": "S010",
                                                                                    "cancellation_id": "CN-1"}}))
    assert st.facts.receipts["cancel:S010"]["cancellation_id"] == "CN-1"
    st.apply_tool_result(ToolResult("create_refund_request", {}, False, error_code="INVALID_REQUEST",
                                    error={"code": "INVALID_REQUEST", "message": "x"}))
    assert st.facts.handoff_reason == "engine_error"
    kf = st.known_facts()
    assert kf["customer_id"] == "C010" and kf["refund_quotes"]["withdrawal"]["article_ids"] == ["A-05", "A-06"]


def test_quote_handoff_sets_reason():
    st = SessionState("org_a", TODAY)
    st.apply_tool_result(ToolResult("quote_refund", {"reason": "minor"}, False, error_code="HANDOFF_REQUIRED",
                                    error={"code": "HANDOFF_REQUIRED", "quote": {"handoff_reason": "minor"},
                                           "article_ids": ["A-13", "A-19"]}))
    assert st.facts.handoff_reason == "minor" and st.facts.quotes["minor"].status == "handoff"


# --- 추가 질문 세트 ---

def test_extra_question_set_is_disjoint_and_renders():
    _, base = load_bank(ORG.question_set)
    version, extra = load_bank(ORG.extra_question_set)
    assert version.startswith("engine-extra-") and not set(base) & set(extra)
    q = render(["customer_agreed"], {**base, **extra})["customer_agreed"]
    assert q["type"] == "noul" and "previous_suggestion" in q["instructions"]
    caller = render(["caller"], extra)["caller"]
    assert caller["type"] == "choice" and set(caller["criteria"]) == {"owner", "third_party", "guardian"}
    assert {"customer_declined", "addon_only", "identity_unavailable"} <= set(extra)
    raw = json.loads(ORG.extra_question_set.read_text(encoding="utf-8"))
    assert set(raw) == {"_note", "version", "questions"}
