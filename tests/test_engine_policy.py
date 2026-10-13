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


def inp(intent="cancel_refund", action="answer", next_info="none", agreed=False, pending=None, declined=False,
        identity_unavailable=False) -> PolicyInput:
    return PolicyInput(intent, action, next_info, agreed, pending, declined, identity_unavailable)


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


# --- 제3자, 법정대리인, 부가 상품, 접수 거절, 본인 확인 불가 ---


def test_third_party_refused_without_asking_or_verifying_identity():
    """s17·s22: 제3자의 계정 처리 요청은 본인 확인을 묻지도 하지도 않고 거절한다(제15조 제3항)."""
    st = SessionState("org_a", TODAY)
    st.caller = "third_party"
    say(st, "u1", "아내 계정으로 연간 결제한 거 대신 해지하려고요")
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert (p.rule, p.action, p.calls, p.ask) == ("third_party", "answer", (), None)
    assert p.articles == ("A-15",) and p.detail["refusal"] == "third_party"
    # 번호와 인증번호를 대신 불러도 본인 확인을 하지 않는다(인증번호를 전달받는 우회)
    say(st, "u2", "번호는 010-0000-0003이고 인증번호는 공공공삼이래요")
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert p.rule == "third_party" and not p.calls
    # 계정과 무관한 일반 문의는 판별 결과로 답하되 본인 확인 규칙을 타지 않는다
    say(st, "u3", "환불 규정이 일반적으로 어떻게 돼요?")
    assert decide(st, inp("other", "answer"), CFG, NOTICES).rule == "decision"


def test_owner_is_not_affected_by_third_party_rule():
    st = SessionState("org_a", TODAY)
    say(st, "u1", "해지하고 환불받고 싶어요")
    assert decide(st, inp(), CFG, NOTICES).rule == "identity_required"


def test_guardian_verifies_minor_account_then_quotes_only_minor_reason():
    """s09: 법정대리인은 미성년 회원 계정의 번호로 본인 확인을 하고, 결제 당시 미성년이면 minor 사유만 견적한다."""
    st = SessionState("org_a", TODAY)
    st.caller = "guardian"
    say(st, "u1", "고등학생 딸이 몰래 연간 결제를 했어요. 취소해 주세요")
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert (p.rule, p.ask) == ("identity_required", "phone") and p.articles == ("A-13", "A-15", "A-19")
    st = verified("C009", plan="annual", birth="2009-06-01",
                  pays=[{"payment_id": "P009", "subscription_id": "S009", "date": "2026-09-20", "amount": 99000,
                         "method": "card", "dispute": False}])
    st.caller = "guardian"
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert (p.rule, p.quote_reasons) == ("quote", ("minor",))  # 연 구독 중도 해지 등 다른 사유는 묻지 않는다
    st.apply_tool_result(ToolResult("quote_refund", {"reason": "minor"}, False, error_code="HANDOFF_REQUIRED",
                                    error={"code": "HANDOFF_REQUIRED", "quote": {"handoff_reason": "minor"},
                                           "article_ids": ["A-13", "A-19"]}))
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert (p.rule, p.action) == ("handoff", "handoff")
    args = p.writes[0].args
    assert (args["customer_id"], args["reason"]) == ("C009", "minor") and "법정대리인" in args["summary"]


def test_guardian_of_adult_member_is_refused_like_third_party():
    st = verified("C012", birth="1996-10-03")
    st.caller = "guardian"
    p = decide(st, inp("cancel"), CFG, NOTICES)
    assert (p.rule, p.detail["refusal"], p.quote_reasons, p.writes) == ("third_party", "guardian_not_applicable", (), ())


def _c014() -> SessionState:
    st = verified("C014", incidents=[], pays=[
        {"payment_id": "P014", "subscription_id": "S014", "date": "2026-09-30", "amount": 9900, "method": "card",
         "dispute": False},
        {"payment_id": "P014F", "subscription_id": "S014F", "date": "2026-09-30", "amount": 3900, "method": "card",
         "dispute": False}])
    st.facts.subscriptions = [
        {"subscription_id": "S014", "plan": "monthly", "price": 9900, "start": "2026-09-30", "next_billing": "2026-10-30",
         "status": "active"},
        {"subscription_id": "S014F", "plan": "family_addon", "price": 3900, "start": "2026-09-30",
         "next_billing": "2026-10-30", "status": "active"}]
    return st


def test_addon_only_cancels_addon_without_quote_or_refund():
    """s14: 부가 상품만 해지하면 견적 없이 부가 상품 해지만 안내하고(제18조 제2항), 동의하면 그 구독만 해지한다."""
    st = _c014()
    st.addon_only = True
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)  # 고객이 환불을 기대해도 부가 상품 요금은 환불하지 않는다
    assert (p.rule, p.quote_reasons) == ("proposal", ()) and p.articles == ("A-18",)
    assert [(w.tool, w.args["subscription_id"]) for w in p.proposal.writes] == [("create_cancellation", "S014F")]
    assert p.proposal.summary["refund_none_articles"] == ["A-18"] and "refund" not in p.proposal.summary
    w = decide(st, inp("plan_change", agreed=True, pending=p.proposal), CFG, NOTICES)  # 라벨이 흔들려도 같은 접수
    assert w.rule == "writes" and w.writes[0].target == "cancel:S014F"
    st.facts.receipts["cancel:S014F"] = {"subscription_id": "S014F", "last_usable_date": "2026-10-29"}
    assert decide(st, inp("cancel"), CFG, NOTICES).rule == "received"
    # 부가 상품만이 아니면 본 구독을 대상으로 견적한다
    st = _c014()
    p = decide(st, inp("cancel"), CFG, NOTICES)
    assert p.rule == "quote" and "addon_only" not in p.detail


def test_declined_proposal_is_not_asked_again_and_not_written():
    """s11: 환불 불가 안내 뒤 '그럼 해지 안 해요'면 해지를 접수하지 않고 같은 내용을 다시 묻지 않는다."""
    st = verified("C011", plan="annual", incidents=[])
    st.facts.quotes = {"withdrawal": not_eligible("withdrawal"),
                       "annual_cancel": not_eligible("annual_cancel", ("A-16", "A-08"))}
    p = decide(st, inp("cancel_refund"), CFG, NOTICES)
    assert p.rule == "proposal" and p.proposal.kind == "cancel"
    d = decide(st, inp("cancel_refund", declined=True, pending=p.proposal), CFG, NOTICES)
    assert (d.rule, d.writes, d.proposal) == ("declined", (), None) and d.detail["declined"] == "cancel"
    assert "refund_unavailable" in d.detail
    # 다음 발화에서 다시 요청하면(거절 표시 없음) 다시 안내한다
    assert decide(st, inp("cancel_refund", pending=None), CFG, NOTICES).rule == "proposal"


def test_identity_unavailable_holds_but_new_otp_is_still_verified():
    """s25: 본인 확인에 실패한 뒤 더 확인할 방법이 없다고 하면 보류. 인증번호를 새로 말하면 그것부터 확인한다."""
    st = SessionState("org_a", TODAY)
    say(st, "u1", "모르는 구독료가 찍혔어요. 돌려주세요")
    say(st, "u2", "010-0000-0099요")
    st.identity.failed = 1
    say(st, "u3", "문자가 안 와요. 이 번호밖에 없어요")
    p = decide(st, inp("refund", identity_unavailable=True), CFG, NOTICES)
    assert (p.rule, p.action, p.calls) == ("identity_unavailable", "hold", ())
    say(st, "u4", "아 왔네요, 공공구구요")
    p = decide(st, inp("refund", identity_unavailable=True), CFG, NOTICES)
    assert p.rule == "verify_identity"
    # 이미 보낸 인증번호를 다시 말해도(판별이 확인 불가로 잘못 봐도) 번호가 있는 발화는 보류하지 않는다
    st.identity.tried.add(("010-0000-0099", "0099"))
    say(st, "u5", "공공구구 맞다니까요")
    p = decide(st, inp("refund", identity_unavailable=True), CFG, NOTICES)
    assert (p.rule, p.ask) == ("identity_required", "otp_retry")


def test_handoff_without_customer_id_carries_phone_in_summary():
    """s18: 본인 확인 2회 실패면 업무 API가 고객 ID를 주지 않으므로 고객이 말한 번호를 인계 요약에 남긴다."""
    st = SessionState("org_a", TODAY)
    say(st, "u1", "010-0000-0005요")
    st.facts.handoff_reason = "identity_failed"
    p = decide(st, inp(), CFG, NOTICES)
    args = p.writes[0].args
    assert "customer_id" not in args and args["reason"] == "identity_failed" and "010-0000-0005" in args["summary"]
