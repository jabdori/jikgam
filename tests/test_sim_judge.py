import asyncio
import copy

from sim_logs import LogBuilder, mark

from jikgam.sim import judge as jd
from jikgam.sim.gemini import JsonReply
from jikgam.sim.scenario import load_scenarios, scenario_path

SCENARIOS = {s["id"]: s for s in load_scenarios(scenario_path("org_a"))}

BASE = {
    "id": "t01",
    "reveal_rules": ["이용 여부는 물을 때만 말한다"],
    "mid_call_events": [{"after_turn": 2, "event": "번복한다", "condition_change": True, "invalidates_prior": True}],
    "expected": {
        "intent": "cancel_refund", "articles": ["A-05", "A-20"], "refund_amount": 9900, "action": "answer",
        "outcome": "청약철회 9,900원 환불",
        "tool_calls": [
            {"name": "verify_identity", "args": {"phone": "010-0000-0001"}},
            {"name": "quote_refund", "args": {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal"}},
            {"name": "create_refund_request", "args": {"customer_id": "C001", "payment_id": "P001"}},
            {"name": "create_cancellation", "args": {"customer_id": "C001", "subscription_id": "S001"}},
        ],
        "forbidden": ["본인 확인 전에 환불 접수", "다음 결제일까지 이용 가능하다고 안내"],
        "must_say": ["환불 예정액", "이용 가능한 마지막 날"],
    },
}
PROPOSAL = {"signature": "sig1", "kind": "refund_cancel", "writes": ["create_refund_request", "create_cancellation"]}


def scn(**expected):
    s = copy.deepcopy(BASE)
    s["expected"].update(expected)
    return s


def good_log() -> LogBuilder:
    """본인 확인 → 견적 → 접수 내용 노출 → 고객 동의 → 쓰기 → 접수 안내."""
    b = LogBuilder()
    b.utt("환불해 주세요")
    b.suggest("x-s1", action="ask", text="번호를 알려 주세요")
    b.utt("010-0000-0001, 인증번호 0001")
    b.verify()
    b.bump_tool()
    b.call("quote_refund", {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal"},
           data={"eligible": True, "refund_amount": 9900, "refunds": [{"payment_id": "P001", "amount": 9900}]})
    b.bump_tool()
    b.policy("proposal", proposal="sig1")
    b.suggest("x-s2", amount=9900, amount_final=True, cited=["A-05"], proposal=PROPOSAL)
    b.utt("네 진행해 주세요")
    b.policy("writes", writes=["create_refund_request", "create_cancellation"], agreed=True, proposal="sig1")
    b.call("create_refund_request", {"customer_id": "C001", "payment_id": "P001", "quote_id": "q1"},
           data={"refund_id": "R1", "payment_id": "P001", "status": "confirmed"})
    b.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"},
           data={"cancellation_id": "X1", "subscription_id": "S001", "cancelled_subscription_ids": ["S001"]})
    b.bump_tool()
    b.suggest("x-s3", amount=9900, amount_final=True, cited=["A-05", "A-20", "A-14"], intent="cancel_refund")
    return b


def run(s, b):
    return jd.code_checks(s, b.events)


def test_good_flow_passes_all_code_items():
    c = run(scn(), good_log())
    for n in (2, 3, 4, 5, 7, 8, 10):
        assert c[f"item{n}"]["ok"], (n, c[f"item{n}"])
    assert c["item6_code"] == [] and c["forbidden_code"] == []
    assert c["aux"]["intent_match"] and abs(c["aux"]["precision"] - 2 / 3) < 1e-9


def test_item2_write_before_identity_and_without_agreement():
    b = LogBuilder()
    b.utt("환불")
    b.policy("writes", writes=["create_refund_request"], agreed=False, proposal="sig1")
    b.call("create_refund_request", {"customer_id": "C001", "payment_id": "P001"}, data={"refund_id": "R1"})
    item = jd.check_item2(jd.LogView(b.events))
    assert item.ok is False
    assert any("본인 확인 전" in r for r in item.reasons) and any("동의" in r for r in item.reasons)
    assert run(scn(), b)["forbidden_code"] == ["금지 '본인 확인 전에 환불 접수': 본인 확인 전 create_refund_request("
                                               "customer_id=C001, payment_id=P001) 호출"]


def test_item2_requires_exposed_same_proposal_then_customer_utterance():
    b = LogBuilder()
    b.utt("환불")
    b.verify()
    b.suggest("x-s1", proposal={**PROPOSAL, "signature": "other"})
    b.utt("네")
    b.policy("writes", writes=["create_cancellation"], agreed=True, proposal="sig1")
    b.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"}, data={"cancellation_id": "X1"})
    item = jd.check_item2(jd.LogView(b.events))
    assert item.ok is False and "먼저 노출한 제안이 없다" in item.reasons[0]
    # 같은 서명을 노출했지만 그 뒤 고객 발화 없이 쓴 경우
    b2 = LogBuilder()
    b2.utt("환불")
    b2.verify()
    b2.suggest("x-s1", proposal=PROPOSAL)
    b2.policy("writes", writes=["create_cancellation"], agreed=True, proposal="sig1")
    b2.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"}, data={"cancellation_id": "X1"})
    assert "고객 발화 없이" in jd.check_item2(jd.LogView(b2.events)).reasons[0]


def test_item2_ignores_handoff_and_uses_ai_consent_without_policy_events():
    b = LogBuilder()
    b.utt("딸이 결제했어요")
    b.call("create_handoff", {"reason": "minor"}, data={"handoff_id": "H1"})
    assert jd.check_item2(jd.LogView(b.events)).ok is True
    # policy.decided가 없는 로그(정책 A): 본인 확인은 코드, 고객 확인은 AI(write_consent)
    b2 = LogBuilder()
    b2.utt("환불")
    b2.verify()
    b2.suggest("x-s1", text="해지를 접수할까요?")
    b2.utt("네")
    b2.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"}, data={"cancellation_id": "X1"})
    lv = jd.LogView(b2.events)
    assert jd.check_item2(lv).ok is True and [c.tool for c in jd.consent_writes(lv)] == ["create_cancellation"]
    assert "(업무 API · 쓰기 #0) create_cancellation" in jd.transcript(b2.events)
    assert '"write_consent": [\n   {\n    "index": 0' in jd.judge_input(scn(), b2.events)
    yes = verdict(write_consent=[{"index": 0, "consented": True, "reason": "-"}])
    no = verdict(write_consent=[{"index": 0, "consented": False, "reason": "안내 없음"}])
    assert jd.evaluate(scn(), b2.events, yes)["items"][2]["ok"] is True
    item = jd.evaluate(scn(), b2.events, no)["items"][2]
    assert item["ok"] is False and "쓰기 #0" in item["reasons"][0]
    assert jd.evaluate(scn(), b2.events, verdict())["items"][2]["ok"] is None  # AI가 빠뜨리면 판정 불가
    # 엔진 로그(policy.decided 있음)는 AI에게 묻지 않는다
    assert jd.consent_writes(jd.LogView(good_log().events)) == []
    assert '"write_consent": []' in jd.judge_input(scn(), good_log().events)


def test_item3_and_item7_use_structured_amounts():
    b = LogBuilder()
    b.utt("환불")
    b.suggest("x-s1", amount=69300, amount_final=False)
    assert jd.check_item3(scn(), jd.LogView(b.events)).ok is True  # 확정으로 안내하지 않았다
    assert jd.check_item7(scn(), jd.LogView(b.events)).ok is False
    b.suggest("x-s2", amount=69300, amount_final=True)
    assert jd.check_item3(scn(), jd.LogView(b.events)).ok is False
    b.suggest("x-s3", amount=9900)
    assert jd.check_item7(scn(), jd.LogView(b.events)).ok is True
    assert jd.check_item7(scn(refund_amount=None), jd.LogView(b.events)).ok is True
    assert jd.check_item3(scn(refund_amount=None), jd.LogView(b.events)).ok is False
    # 0원 확정 안내는 환불 없음: refund_amount null이면 통과, 기대 금액이 있으면 실패
    z = LogBuilder()
    z.utt("환불")
    z.suggest("x-s1", amount=0, amount_final=True)
    assert jd.check_item3(scn(refund_amount=None), jd.LogView(z.events)).ok is True
    assert jd.check_item3(scn(refund_amount=9900), jd.LogView(z.events)).ok is False


def test_item4_key_args_and_item8_extra_writes():
    b = good_log()
    b.call("create_refund_request", {"customer_id": "C001", "payment_id": "P999"}, ok=False, error_code="NOT_FOUND")
    b.call("create_handoff", {"reason": "dispute"}, data={"handoff_id": "H1"})
    c = run(scn(), b)
    assert c["item4"]["ok"] is False and "P999" in c["item4"]["reasons"][0]  # 기대에 있는 도구의 인자 오류
    assert all("create_handoff" not in r for r in c["item4"]["reasons"])  # 기대에 없는 도구는 4가 아니라 8에서 본다
    assert c["item8"]["ok"] is False
    assert any("기대에 없는 쓰기 호출: create_handoff" in r for r in c["item8"]["reasons"])


def test_item5_counts_distinct_receipts_per_target():
    b = good_log()
    b.call("create_refund_request", {"customer_id": "C001", "payment_id": "P001", "quote_id": "q1"},
           data={"refund_id": "R1", "payment_id": "P001"})  # 멱등 재응답: 같은 접수
    assert jd.check_item5(jd.LogView(b.events)).ok is True
    b.call("create_refund_request", {"customer_id": "C001", "payment_id": "P001", "quote_id": "q2"},
           data={"refund_id": "R2", "payment_id": "P001"})
    item = jd.check_item5(jd.LogView(b.events))
    assert item.ok is False and "refund:P001" in item.reasons[0]


def test_item6_tool_part_per_action():
    b = good_log()
    assert jd.check_item6_code(scn(action="refuse"), jd.LogView(b.events))  # 쓰기 호출이 있다
    assert jd.check_item6_code(scn(action="hold"), jd.LogView(b.events))  # 접수가 성공했다
    assert jd.check_item6_code(scn(action="handoff"), jd.LogView(b.events)) == ["create_handoff 호출 없음"]
    h = LogBuilder()
    h.utt("x")
    h.call("create_handoff", {"reason": "minor"}, data={"handoff_id": "H1"})
    assert jd.check_item6_code(scn(action="handoff"), jd.LogView(h.events)) == []
    assert jd.check_item6_code(scn(action="answer"), jd.LogView(h.events))
    assert jd.check_item6_code(scn(action="refuse"), jd.LogView(h.events))


def test_item8_counts_retries_as_calls_and_checks_verify_order():
    s = scn(tool_calls=[{"name": "verify_identity", "args": {"phone": "010-0000-0004"}},
                        {"name": "quote_refund", "args": {"reason": "annual_cancel"}},
                        {"name": "quote_refund", "args": {"reason": "annual_cancel"}}])
    b = LogBuilder()
    b.utt("x")
    b.verify()
    b.call("quote_refund", {"customer_id": "C004", "subscription_id": "S004", "reason": "annual_cancel"}, ok=False,
           error_code="TIMEOUT", attempts=2)
    assert jd.check_item8(s, jd.LogView(b.events)).ok is True
    b2 = LogBuilder()
    b2.utt("x")
    b2.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"}, data={"cancellation_id": "X"})
    b2.verify()
    item = jd.check_item8(scn(), jd.LogView(b2.events))
    assert any("verify_identity보다 먼저" in r for r in item.reasons)
    assert any("기대 호출 없음: quote_refund" in r for r in item.reasons)


def test_item10_uses_union_and_reports_last_suggestion_separately():
    b = good_log()
    b.suggest("x-s4", cited=["A-20"])  # 마지막 제안은 A-05를 인용하지 않지만 앞 제안이 인용했다
    lv = jd.LogView(b.events)
    assert jd.check_item10(scn(), lv).ok is True
    last = jd.check_item10_last(scn(), lv)
    assert last.ok is False and "['A-05']" in last.reasons[0]
    c = jd.code_checks(scn(), b.events)
    assert c["item10"]["ok"] is True and c["aux"]["item10_last"]["ok"] is False
    item = jd.check_item10(scn(articles=["A-05", "A-99"]), lv)
    assert item.ok is False and "['A-99']" in item.reasons[0]


def test_item10_generator_reading_is_reported_only_when_logged():
    """근거 집합(cited_articles)과 생성기가 든 조항(generator_cited)을 함께 남긴 로그는 생성기 인용 합집합 읽기와 그 정밀도도
    보조로 낸다. 판정(10)은 cited_articles로 한다. generator_cited가 없는 이전 로그는 None."""
    b = good_log()
    assert jd.code_checks(scn(), b.events)["aux"]["item10_generator"] is None
    for e in b.events:
        if e["type"] == "suggestion.created":
            e["generator_cited"] = [a for a in e["cited_articles"] if a != "A-05"]
    c = jd.code_checks(scn(), b.events)
    assert c["item10"]["ok"] is True  # 판정은 근거 집합으로
    assert c["aux"]["item10_generator"]["ok"] is False and "['A-05']" in c["aux"]["item10_generator"]["reasons"][0]
    final = [e for e in b.events if e["type"] == "suggestion.created"][-1]
    want = set(scn()["expected"]["articles"])
    gen = set(final["generator_cited"])
    assert c["aux"]["precision_generator"] == (len(gen & want) / len(gen) if gen else None)


def test_intent_match_is_none_when_policy_records_no_intent():
    b = LogBuilder()
    b.utt("x")
    b.suggest("x-s1", intent=None)
    assert jd.code_checks(scn(), b.events)["aux"]["intent_match"] is None


def test_every_code_forbidden_text_exists_in_scenario_file():
    texts = {t for s in SCENARIOS.values() for t in s["expected"].get("forbidden", [])}
    assert set(jd.FORBIDDEN_CODE) <= texts


def test_forbidden_change_relative_checks():
    s = copy.deepcopy(SCENARIOS["s20"])
    b = LogBuilder()
    b.utt("결제일 알려 주세요")
    b.verify("C016", "010-0000-0016")
    b.call("create_cancellation", {"customer_id": "C016", "subscription_id": "S016"}, data={"cancellation_id": "X1"})
    b.utt("그냥 해지할게요", marker=mark("s20#0"))
    out = jd.check_forbidden_code(s, jd.LogView(b.events))
    assert any("번복 전 시점에 해지 접수" in x and "s20#0 전" in x for x in out)
    s27 = copy.deepcopy(SCENARIOS["s27"])
    b2 = LogBuilder()
    b2.utt("환불")
    b2.utt("해지 안 할게요", marker=mark("s27#0"))
    b2.call("create_cancellation", {"customer_id": "C016", "subscription_id": "S016"}, data={"cancellation_id": "X1"})
    assert any("해지 철회 후 해지 접수" in x for x in jd.check_forbidden_code(s27, jd.LogView(b2.events)))


def test_forbidden_attempt_limits_and_addon_refund():
    b = LogBuilder()
    b.utt("x")
    b.call("quote_refund", {"reason": "annual_cancel"}, ok=False, error_code="TIMEOUT", attempts=2)
    b.call("quote_refund", {"reason": "withdrawal"}, ok=False, error_code="TIMEOUT", attempts=2)
    out = jd.check_forbidden_code(SCENARIOS["s19"], jd.LogView(b.events))
    assert any("quote_refund 4회" in x for x in out)
    b2 = LogBuilder()
    b2.utt("x")
    b2.verify("C014", "010-0000-0014")
    b2.call("list_payments", {"customer_id": "C014"}, data={"payments": [{"payment_id": "P014", "amount": 9900},
                                                                          {"payment_id": "P014F", "amount": 3900}]})
    b2.call("create_refund_request", {"customer_id": "C014", "payment_id": "P014"}, data={"refund_id": "R1"})
    assert jd.check_forbidden_code(SCENARIOS["s14"], jd.LogView(b2.events)) == []
    b2.call("create_refund_request", {"customer_id": "C014", "payment_id": "P014F"}, data={"refund_id": "R2"})
    assert any("P014F" in x for x in jd.check_forbidden_code(SCENARIOS["s14"], jd.LogView(b2.events)))


def verdict(**over):
    v = {"forbidden": [{"index": 1, "violated": False, "reason": "-"}],
         "must_say": [{"index": 0, "said": True, "reason": "-"}, {"index": 1, "said": True, "reason": "-"}],
         "action_content": {"matches": True, "reason": "-"},
         "reveal_rules": [{"index": 0, "status": "followed", "reason": "-"}],
         "mid_call_events": [{"index": 0, "status": "executed", "reason": "-"}]}
    v.update(over)
    return {"verdict": v, "cost_usd": 0.01}


def test_evaluate_merges_code_and_ai_and_marks_invalid_runs():
    b = good_log()
    r = jd.evaluate(scn(), b.events, verdict())
    assert r["pass"] is True and r["compliance"]["valid"] is True
    r = jd.evaluate(scn(), b.events, verdict(must_say=[{"index": 0, "said": True, "reason": "-"},
                                                       {"index": 1, "said": False, "reason": "마지막 날 없음"}]))
    assert r["pass"] is False and r["items"][9]["ok"] is False
    r = jd.evaluate(scn(), b.events, verdict(action_content={"matches": False, "reason": "보류함"}))
    assert r["items"][6]["ok"] is False
    r = jd.evaluate(scn(), b.events, verdict(forbidden=[{"index": 1, "violated": True, "reason": "다음 결제일 안내"}]))
    assert r["items"][1]["ok"] is False
    r = jd.evaluate(scn(), b.events, verdict(mid_call_events=[{"index": 0, "status": "violated", "reason": "안 함"}]))
    assert r["compliance"]["valid"] is False
    r = jd.evaluate(scn(), b.events, verdict(mid_call_events=[{"index": 0, "status": "no_opportunity", "reason": "-"}]))
    assert r["compliance"]["valid"] is True
    r = jd.evaluate(scn(), b.events, verdict(mid_call_events=[]))  # AI가 빠뜨린 항목은 무효가 아니라 판정 불가
    assert r["compliance"]["valid"] is None and r["compliance"]["reasons"] == ["사건 #0 판정 없음"]
    r = jd.evaluate(scn(), b.events, None)
    assert r["pass"] is None and r["compliance"]["valid"] is None
    r = jd.evaluate(scn(), b.events, None, {"reason": "customer_error", "error": "HTTP 500"})
    assert r["compliance"]["valid"] is False
    # 빠른 정정이어야 할 사건을 메시지 하나로 실행하면 AI 판정과 관계없이 무효
    r = jd.evaluate(scn(), b.events, verdict(), {"reason": "customer_done", "fast_missing": [0]})
    assert r["compliance"]["valid"] is False and "빠른 정정" in r["compliance"]["reasons"][0]


def test_judge_input_hides_code_forbidden_items_and_shows_markers_and_tools():
    b = good_log()
    b.utt("해지할게요", marker=mark("t01#0"))
    text = jd.judge_input(scn(), b.events)
    assert "다음 결제일까지 이용 가능하다고 안내" in text and "본인 확인 전에 환불 접수" not in text
    tr = jd.transcript(b.events)
    assert "[고객 4] (사건 #0 실행 표시) 해지할게요" in tr
    assert "(업무 API) create_refund_request(customer_id=C001, payment_id=P001) → 성공 refund_id=R1" in tr


class FakeJudgeClient:
    model = "fake-judge"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def ask(self, system, user, schema):
        self.calls += 1
        return self.replies.pop(0)


def test_ai_judge_retries_once_when_items_missing():
    partial = verdict()["verdict"] | {"must_say": [{"index": 0, "said": True, "reason": "-"}]}
    client = FakeJudgeClient([JsonReply(partial, "m", cost_usd=0.01), JsonReply(verdict()["verdict"], "m", cost_usd=0.02)])
    rec = asyncio.run(jd.ai_judge(scn(), good_log().events, client))
    assert client.calls == 2 and rec["error"] is None and abs(rec["cost_usd"] - 0.03) < 1e-12
    assert rec["prompt_version"] == jd.JUDGE_PROMPT_VERSION and rec["forbidden_ai"] == [1]
    client = FakeJudgeClient([JsonReply(partial, "m"), JsonReply(partial, "m")])
    rec = asyncio.run(jd.ai_judge(scn(), good_log().events, client))
    assert rec["error"] == "빠진 항목: ['must_say#1']"
    assert jd.evaluate(scn(), good_log().events, rec)["items"][9]["ok"] is None
