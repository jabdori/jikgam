from datetime import date

from jikgam.engine import policy
from jikgam.engine.bizapi import ToolResult
from jikgam.engine.claims import extract
from jikgam.engine.org import load_org
from jikgam.engine.policy import PolicyInput, choose_quote, decide, plausible_reasons
from jikgam.engine.state import QuoteOutcome, SessionState

ORG = load_org("org_a")
CFG = ORG.policy
NOTICES = ORG.required_notices
TODAY = date(2026, 10, 12)


def say(st: SessionState, uid: str, text: str) -> None:
    st.version += 1
    st.add_turn(uid, "customer", text)
    st.add_claims(uid, extract(text, TODAY))


def inp(intent="cancel_refund", action="answer", next_info="none", agreed=False, pending=None) -> PolicyInput:
    return PolicyInput(intent, action, next_info, agreed, pending)


def verified(cid="C010", plan="monthly", pays=None, usage=None, incidents=None, birth="1991-02-27") -> SessionState:
    st = SessionState("org_a", TODAY)
    st.identity.verified, st.identity.customer_id = True, cid
    sid = "S" + cid[1:]
    st.facts.customer = {"customer_id": cid, "name": "오시우", "birth": birth, "status": "active"}
    st.facts.subscriptions = [{"subscription_id": sid, "plan": plan, "price": 9900, "promo": False, "trial": False,
                               "start": "2026-10-06", "next_billing": "2026-11-06", "status": "active"}]
    st.facts.payments = pays if pays is not None else [
        {"payment_id": "P" + cid[1:], "subscription_id": sid, "date": "2026-10-06", "amount": 9900, "method": "card",
         "dispute": False}]
    st.facts.usage = usage if usage is not None else [{"date": "2026-10-07", "type": "play", "during_trial": False}]
    st.facts.incidents = incidents if incidents is not None else [
        {"incident_id": "INC-2026-1008", "start": "2026-10-08T13:00", "end": "2026-10-08T18:00", "hours": 5,
         "cause": "company"}]
    return st


def eligible(reason, amount=9900, pid="P010", method="card", account_required=False, articles=("A-09",)):
    return QuoteOutcome(reason, "eligible", {
        "quote_id": f"Q-{reason}", "eligible": True, "refund_amount": amount, "approval_required": amount > 50000,
        "refunds": [{"payment_id": pid, "subscription_id": "S" + pid[1:4], "amount": amount, "payment_method": method,
                     "refund_method": "card_cancel", "account_required": account_required,
                     "expected_period": "3~7영업일"}], "last_usable_date": "2026-10-12"}, "ok", articles)


def not_eligible(reason, articles=("A-05", "A-06")):
    return QuoteOutcome(reason, "not_eligible", {"reason": reason}, "대상 아님", articles)


def test_handoff_reason_creates_handoff_once():
    st = verified()
    st.facts.handoff_reason = "dispute"
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.action, [w.tool for w in p.writes]) == ("handoff", "handoff", ["create_handoff"])
    assert p.writes[0].args["reason"] == "dispute" and p.writes[0].target == "handoff:dispute"
    st.facts.receipts["handoff"] = {"handoff_id": "HO-1"}
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.action, p.writes) == ("handoff", "handoff", ())


def test_api_failure_holds():
    st = verified()
    st.facts.api_failures["quote_refund"] = "TIMEOUT"
    assert decide(st, inp(), CFG, NOTICES).action == "hold"


def test_identity_ask_then_verify_then_retry():
    st = SessionState("org_a", TODAY)
    say(st, "u1", "해지하고 환불받고 싶어요")
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.action, p.next_info, p.ask) == ("identity_required", "ask", "identity", "phone")
    say(st, "u2", "010-0000-0010이요")
    assert decide(st, inp(), CFG, NOTICES).ask == "otp"
    say(st, "u3", "공공일공이요")
    p = decide(st, inp(), CFG, NOTICES)
    assert p.rule == "verify_identity" and p.calls[0].args == {"phone": "010-0000-0010", "otp": "0010"}
    st.identity.tried.add(("010-0000-0010", "0010"))
    st.identity.failed = 1
    assert decide(st, inp(), CFG, NOTICES).ask == "otp_retry"  # 같은 번호 쌍은 다시 보내지 않는다


def test_identity_not_asked_for_general_question():
    st = SessionState("org_a", TODAY)
    say(st, "u1", "가입 전에 궁금한 게 있어요")
    p = decide(st, inp(intent="other", action="answer"), CFG, NOTICES)
    assert (p.rule, p.action) == ("decision", "answer")


def test_lookups_after_verification():
    st = SessionState("org_a", TODAY)
    st.identity.verified, st.identity.customer_id = True, "C001"
    assert decide(st, inp(), CFG, NOTICES).lookups


def test_plausible_reasons_from_system_facts():
    st = verified()
    sub = st.facts.subscriptions[0]
    assert plausible_reasons("cancel_refund", sub, st, CFG) == ["withdrawal", "incident"]
    st.facts.incidents = []
    assert plausible_reasons("cancel_refund", sub, st, CFG) == ["withdrawal"]
    assert plausible_reasons("cancel", sub, st, CFG) == ["withdrawal"]  # 해지만 해도 환불 여부를 안내해야 한다(제20조)
    assert plausible_reasons("compensation", sub, st, CFG) == []


def test_cancel_only_request_informs_available_refund_without_writing_it():
    st = verified("C001", usage=[], incidents=[])
    st.facts.quotes = {"withdrawal": eligible("withdrawal", pid="P001", articles=("A-05",))}
    p = decide(st, inp("cancel"), CFG, NOTICES)
    assert p.rule == "proposal" and [w.tool for w in p.proposal.writes] == ["create_cancellation"]
    assert p.proposal.summary["refund_available"]["refund_amount"] == 9900


def test_annual_cancel_includes_refund_even_for_cancel_request():
    st = verified("C003", plan="annual", incidents=[])
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"),
                       "annual_cancel": eligible("annual_cancel", 62370, pid="P003", articles=("A-08",))}
    p = decide(st, inp("cancel"), CFG, NOTICES)
    assert [w.tool for w in p.proposal.writes] == ["create_refund_request", "create_cancellation"]


def test_signature_ignores_explanation_fields():
    """s20: 환불 불가 안내 뒤 '그래도 해지해 주세요'로 요청 라벨이 cancel_refund→cancel로 바뀌어도 같은 접수 내용이다."""
    st = verified("C016")
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"), "incident": not_eligible("incident", ("A-09",))}
    a = decide(st, inp("cancel_refund"), CFG, NOTICES).proposal
    b = decide(st, inp("cancel"), CFG, NOTICES).proposal
    assert a.signature == b.signature
    # 금액이 바뀌면 서명도 바뀐다
    assert policy._signature(list(a.writes), {"refund": {"refund_amount": 1}}) != a.signature
    w = decide(st, inp("cancel", agreed=True, pending=a), CFG, NOTICES)
    assert w.rule == "writes" and [c.tool for c in w.writes] == ["create_cancellation"]


def test_minor_reason_uses_age_at_payment():
    st = verified(birth="2009-06-01", plan="annual")
    assert "minor" in plausible_reasons("cancel_refund", st.facts.subscriptions[0], st, CFG)
    st = verified(birth="1990-01-01", plan="annual")
    assert "minor" not in plausible_reasons("cancel_refund", st.facts.subscriptions[0], st, CFG)


def test_duplicate_reason_from_same_day_payments_or_claim():
    pays = [{"payment_id": "P006a", "subscription_id": "S006", "date": "2026-09-25", "amount": 9900, "method": "card"},
            {"payment_id": "P006b", "subscription_id": "S006", "date": "2026-09-25", "amount": 9900, "method": "card"}]
    st = verified("C006", pays=pays, incidents=[])
    assert "duplicate" in plausible_reasons("refund", st.facts.subscriptions[0], st, CFG)


def test_quote_calls_then_choice_eligible_first_then_priority():
    st = verified()
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.quote_reasons) == ("quote", ("withdrawal", "incident"))
    quotes = {"withdrawal": eligible("withdrawal", articles=("A-05",)), "incident": eligible("incident")}
    assert choose_quote(["withdrawal", "incident"], quotes, CFG).reason == "withdrawal"  # 같은 대상이면 우선순위
    quotes["withdrawal"] = not_eligible("withdrawal")
    assert choose_quote(["withdrawal", "incident"], quotes, CFG).reason == "incident"  # 대상인 것이 먼저
    quotes["incident"] = not_eligible("incident")
    assert choose_quote(["withdrawal", "incident"], quotes, CFG) is None


def test_proposal_then_writes_only_with_agreement_on_same_content():
    st = verified()
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"), "incident": eligible("incident")}
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.action, p.writes) == ("proposal", "answer", ())
    assert [w.tool for w in p.proposal.writes] == ["create_refund_request", "create_cancellation"]
    assert p.notices == NOTICES and p.proposal.kind == "refund_cancel"
    # 동의 없음 → 다시 안내
    assert decide(st, inp(pending=p.proposal), CFG, NOTICES).rule == "proposal"
    # 동의 → 같은 내용이면 쓰기
    w = decide(st, inp(agreed=True, pending=p.proposal), CFG, NOTICES)
    assert w.rule == "writes" and [c.tool for c in w.writes] == ["create_refund_request", "create_cancellation"]
    assert w.writes[0].args == {"customer_id": "C010", "payment_id": "P010", "quote_id": "Q-incident"}
    assert w.writes[1].target == "cancel:S010"
    # 안내한 뒤 내용이 바뀌었으면(다른 견적) 동의해도 쓰지 않는다
    st.facts.quotes["incident"] = eligible("incident", amount=8800)
    assert decide(st, inp(agreed=True, pending=p.proposal), CFG, NOTICES).rule == "proposal"


def test_received_after_writes():
    st = verified()
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"), "incident": eligible("incident")}
    st.facts.receipts["refund:P010"] = {"refund_id": "RF-1", "payment_id": "P010"}
    st.facts.receipts["cancel:S010"] = {"cancellation_id": "CN-1", "subscription_id": "S010"}
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.writes, p.proposal) == ("received", (), None)


def test_cancel_only_when_refund_not_eligible():
    st = verified("C016")
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"), "incident": not_eligible("incident", ("A-09",))}
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert p.rule == "proposal" and [w.tool for w in p.proposal.writes] == ["create_cancellation"]
    assert set(p.proposal.summary["refund_unavailable"]) == {"withdrawal", "incident"}
    st.facts.payments = [dict(st.facts.payments[0]), dict(st.facts.payments[0], payment_id="P016b")]
    st.facts.incidents = []
    st.facts.quotes = {"duplicate": not_eligible("duplicate", ("A-10",)), "withdrawal": not_eligible("withdrawal")}
    p = decide(st, inp("refund"), CFG, NOTICES)
    assert p.rule == "not_eligible"


def test_account_required_asks_then_registers_first():
    pays = [{"payment_id": "P008a", "subscription_id": "S008", "date": "2026-09-15", "amount": 9900, "method": "mobile"},
            {"payment_id": "P008b", "subscription_id": "S008", "date": "2026-09-15", "amount": 9900, "method": "mobile"}]
    st = verified("C008", pays=pays, incidents=[])
    st.facts.customer["name"] = "임태윤"
    st.facts.quotes = {"duplicate": eligible("duplicate", pid="P008b", method="mobile", account_required=True,
                                             articles=("A-10",)), "withdrawal": not_eligible("withdrawal")}
    say(st, "u5", "네 그거 돌려주세요")
    p = decide(st, inp("refund"), CFG, NOTICES)
    assert (p.rule, p.ask) == ("account_required", "account")
    say(st, "u6", "아내 명의 계좌로 받을게요. 농협 352-1234-5678-91이요")
    assert decide(st, inp("refund"), CFG, NOTICES).ask == "account_holder"
    say(st, "u7", "아 제 거로 할게요. 우리은행 1002-123-456789요")
    p = decide(st, inp("refund"), CFG, NOTICES)
    assert p.rule == "proposal"
    assert [w.tool for w in p.proposal.writes] == ["register_refund_account", "create_refund_request"]
    assert p.proposal.writes[0].args["holder"] == "임태윤" and p.proposal.writes[0].args["bank"] == "우리"
    # 예금주가 거절되면 같은 계좌로 다시 제안하지 않는다
    st.apply_tool_result(ToolResult("register_refund_account", p.proposal.writes[0].args, True,
                                    data={"registered": False, "reject_reason": "holder_not_customer"}))
    assert decide(st, inp("refund"), CFG, NOTICES).ask == "own_account"


def test_target_ambiguous_asks_plan_type():
    st = verified()
    st.facts.subscriptions.append(dict(st.facts.subscriptions[0], subscription_id="S999"))
    p = decide(st, inp(), CFG, NOTICES)
    assert (p.rule, p.ask) == ("target_ambiguous", "plan_type")


def test_falls_back_to_decision_or_hold():
    st = verified()
    assert decide(st, inp("billing_inquiry", "answer", "none"), CFG, NOTICES).rule == "decision"
    assert decide(st, inp("billing_inquiry", None, None), CFG, NOTICES).rule == "decision_unavailable"


def test_incident_window_starts_at_current_payment():
    st = verified()
    assert policy.incident_window(st) == ("2026-10-06", "2026-10-12")
