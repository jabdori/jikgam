import asyncio
import json
from datetime import datetime
from pathlib import Path

from conftest import inject_fault
from engine_fakes import EchoGenerator, GatedDecider, NoBizApi, ScriptedDecider, utt

from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.chat import report
from jikgam.engine.org import load_org
from jikgam.engine.session import Session

ORG = load_org("org_a")
ROOT = Path(__file__).resolve().parents[1]
COMMON = {"t_ms", "wall", "session", "type", "version"}


def types(events, *wanted):
    return [e for e in events if e["type"] in wanted]


def tool_calls(log):
    return [e["tool"] for e in log if e["type"] == ev.TOOL_CALL]


def test_s01_flow_writes_only_after_agreement(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("취소하고 환불", "cancel_refund")])

    async def go():
        s = Session(ORG, "t-s01", tmp_path, decision_client=dec, generator=EchoGenerator(),
                    bizapi=BizApiClient(bizapi, "t-s01"))
        o1 = await s.handle(utt("u1", "안녕하세요, 지난주 목요일쯤 결제가 됐는데요, 안 쓸 거라서 취소하고 환불받고 싶어요."))
        o2 = await s.handle(utt("u2", "010-0000-0001이에요."))
        o3 = await s.handle(utt("u3", "인증번호는 공공공일이요."))
        o4 = await s.handle(utt("u4", "네, 진행해 주세요."))
        await s.aclose()
        return s, (o1, o2, o3, o4)

    s, outs = asyncio.run(go())
    says = [types(o, ev.AGENT_SAY)[-1]["text"] for o in outs]
    assert says == ["[identity_required:ask:phone]", "[identity_required:ask:otp]", "[proposal:answer:None]",
                    "[received:answer:None]"]
    assert tool_calls(outs[0] + outs[1]) == []
    t3 = tool_calls(outs[2])
    assert t3[0] == "verify_identity" and "quote_refund" in t3
    assert not {"create_refund_request", "create_cancellation"} & set(t3)  # 동의 전에는 쓰지 않는다
    assert tool_calls(outs[3]) == ["create_refund_request", "create_cancellation"]
    quote = [e for e in outs[2] if e["type"] == ev.QUOTE_SELECTED][0]
    assert quote["chosen"] == "withdrawal" and quote["tried"]["withdrawal"]["refund_amount"] == 9900
    created = types(outs[2], ev.SUGGESTION_CREATED)[-1]
    assert created["amount"] == 9900 and created["proposal"]["writes"] == ["create_refund_request", "create_cancellation"]
    f = s.state.facts
    assert f.receipts["refund:P001"]["status"] == "confirmed"
    assert f.receipts["cancel:S001"]["last_usable_date"] == "2026-10-12"
    # 쓰기 호출에는 (세션, 도구, 대상)으로 만든 멱등 키가 붙는다
    writes = [e for e in outs[3] if e["type"] == ev.TOOL_RESULT]
    assert all(e["idempotency_key"].startswith("idem-") for e in writes)


def test_turn_event_order_and_common_fields(tmp_path):
    dec = ScriptedDecider(intents=[("가입 전", "other")])

    async def go():
        s = Session(ORG, "t-order", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=NoBizApi())
        out = await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        await s.aclose()
        return out

    out = asyncio.run(go())
    order = [e["type"] for e in out]
    expected = [ev.UTTERANCE_END, ev.VERSION_BUMPED, ev.CLAIMS_EXTRACTED, ev.RETRIEVAL_DONE, ev.DECISION_REQUEST,
                ev.DECISION_RESPONSE, ev.EVIDENCE_UPDATED, ev.POLICY_DECIDED, ev.GENERATION_REQUEST,
                ev.GENERATION_RESPONSE, ev.SUGGESTION_CREATED, ev.AGENT_SAY, ev.SUGGESTION_EXPOSED]
    assert order == expected
    log = ev.read_log(tmp_path / "t-order.jsonl")
    assert log[0]["type"] == ev.SESSION_START
    for e in log:
        assert COMMON <= set(e) and e["session"] == "t-order"
        assert isinstance(e["t_ms"], float) and datetime.fromisoformat(e["wall"]).utcoffset().total_seconds() == 9 * 3600
    t = [e["t_ms"] for e in log[1:]]
    assert t == sorted(t)  # 채팅은 받은 시각이 utterance.end이므로 로그 순서와 시계 순서가 같다
    bumped = types(log, ev.VERSION_BUMPED)[0]
    assert (bumped["from"], bumped["to"], bumped["cause"], bumped["utt_id"]) == (0, 1, "utterance", "u1")
    evidence = types(log, ev.EVIDENCE_UPDATED)[0]
    assert evidence["version"] == 1 and {"articles", "sufficient", "next_info", "action"} <= set(evidence)
    exposed = types(log, ev.SUGGESTION_EXPOSED)[0]
    assert exposed["channel"] == "chat" and exposed["suggestion_id"] == types(log, ev.SUGGESTION_CREATED)[0]["suggestion_id"]
    lat = ev.turn_latencies(log)
    assert lat[0]["evidence_ms"] is not None and lat[0]["say_ms"] >= lat[0]["evidence_ms"]


def _two_turns_with_gate(tmp_path, ignore_cancel: bool):
    dec = GatedDecider(ignore_cancel, intents=[("가입 전", "other"), ("환불 방법", "other")])

    async def go():
        s = Session(ORG, "t-gate", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=NoBizApi())
        t1 = asyncio.create_task(s.handle(utt("u1", "가입 전에 궁금한 게 있어요")))
        await dec.entered.wait()
        t2 = asyncio.create_task(s.handle(utt("u2", "환불 방법이 궁금해요")))
        while s.state.version < 2:
            await asyncio.sleep(0)
        dec.release.set()
        o1, o2 = await t1, await t2
        await s.aclose()
        return o1, o2

    o1, o2 = asyncio.run(go())
    return o1, o2, ev.read_log(tmp_path / "t-gate.jsonl")


def test_stale_decision_result_is_discarded(tmp_path):
    o1, o2, log = _two_turns_with_gate(tmp_path, ignore_cancel=True)
    discarded = types(o1, ev.RESULT_DISCARDED)
    assert len(discarded) == 1 and discarded[0]["stage"] == "decision" and discarded[0]["started_version"] == 1
    assert not types(o1, ev.EVIDENCE_UPDATED, ev.AGENT_SAY)  # 지난 버전 결과로 근거나 제안을 내지 않는다
    assert [e["version"] for e in types(log, ev.EVIDENCE_UPDATED)] == [2]
    order = [e["type"] for e in log]
    i_bump2 = next(i for i, e in enumerate(log) if e["type"] == ev.VERSION_BUMPED and e["to"] == 2)
    i_disc = order.index(ev.RESULT_DISCARDED)
    i_evid = order.index(ev.EVIDENCE_UPDATED)
    assert i_bump2 < i_disc and i_bump2 < i_evid
    assert [e["text"] for e in types(o2, ev.AGENT_SAY)] == ["[decision:answer:None]"]


def test_new_utterance_cancels_running_pipeline(tmp_path):
    o1, o2, _ = _two_turns_with_gate(tmp_path, ignore_cancel=False)
    cancelled = types(o1, ev.TASK_CANCELLED)
    assert len(cancelled) == 1 and cancelled[0]["stage"] == "decision" and cancelled[0]["started_version"] == 1
    assert not types(o1, ev.RESULT_DISCARDED, ev.EVIDENCE_UPDATED, ev.AGENT_SAY)
    assert len(types(o2, ev.AGENT_SAY)) == 1 and types(o2, ev.EVIDENCE_UPDATED)[0]["version"] == 2


def test_s21_topic_change_invalidates_previous_suggestion(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("보상", "compensation"), ("해지하고 돈", "cancel_refund")], changes=("연장 말고",))

    async def go():
        s = Session(ORG, "t-s21", tmp_path, decision_client=dec, generator=EchoGenerator(),
                    bizapi=BizApiClient(bizapi, "t-s21"))
        outs = [await s.handle(utt(f"u{i}", t)) for i, t in enumerate([
            "목요일에 서비스 몇 시간 안 됐잖아요. 그거 보상 같은 거 없어요?", "010-0000-0010이요", "공공일공이요",
            "연장 말고요, 그냥 해지하고 돈으로 돌려받을 수 있어요?", "네, 그렇게 해 주세요"], 1)]
        await s.aclose()
        return outs

    outs = asyncio.run(go())
    s3 = types(outs[2], ev.SUGGESTION_CREATED)[-1]
    # 보상 문의는 장애 기록으로 연장 시간을 계산하는 코드 규칙이 답한다(5시간 × 3)
    assert s3["intent"] == "compensation" and s3["rule"] == "compensation"
    p3 = types(outs[2], ev.POLICY_DECIDED)[-1]
    assert p3["must_mention"] == ["15시간"] and p3["detail"]["compensation"][0]["extension_hours"] == 15
    inv = types(outs[3], ev.SUGGESTION_INVALIDATED)
    assert [e["suggestion_id"] for e in inv] == [s3["suggestion_id"]]
    assert "condition_changed=true" in inv[0]["reasons"] and inv[0]["invalidated_version"] == outs[3][1]["to"]
    order = [e["type"] for e in outs[3]]
    assert order.index(ev.SUGGESTION_INVALIDATED) < order.index(ev.EVIDENCE_UPDATED)
    q = types(outs[3], ev.QUOTE_SELECTED)[0]
    assert q["chosen"] == "incident" and q["tried"]["withdrawal"]["status"] == "not_eligible"
    assert tool_calls(outs[4]) == ["create_refund_request", "create_cancellation"]
    refund = next(e for e in outs[4] if e["type"] == ev.TOOL_CALL)
    assert refund["args"]["payment_id"] == "P010"
    # 재생 보고서는 로그만으로 만든다
    text = report(ev.read_log(tmp_path / "t-s21.jsonl"))
    assert "[조건 변경 표시]" not in text  # 이 테스트 입력에는 표시가 없다
    assert f"무효 {s3['suggestion_id']}" in text and "create_cancellation(customer_id=C010, subscription_id=S010)" in text
    assert text.count("  u") >= 5 and "판별 " in text


def test_s20_agreement_counts_when_intent_label_shifts(bizapi, tmp_path):
    """환불 불가 안내 뒤 '네, 그래도 해지해 주세요': 요청 라벨이 cancel로 바뀌고 suggestion_valid가 거짓이어도
    조건 변경이 없고 접수 내용이 같으면 해지를 접수한다. 개발용 재생 s20(e2e-try2)에서 나온 실패의 회귀 테스트."""
    dec = ScriptedDecider(intents=[("다음 결제", "billing_inquiry"), ("그냥 해지", "cancel"), ("환불 안", "cancel_refund"),
                                   ("그래도 해지", "cancel")], changes=("그냥 해지",), invalid=("그래도",))

    async def go():
        s = Session(ORG, "t-s20", tmp_path, decision_client=dec, generator=EchoGenerator(),
                    bizapi=BizApiClient(bizapi, "t-s20"))
        outs = [await s.handle(utt(f"u{i}", t)) for i, t in enumerate([
            "해지하려는 건 아니고요, 다음 결제가 언제 되는지만 알고 싶어서요.", "010-0000-0016이고 인증번호는 공공일육이에요.",
            "아 그냥 해지할게요, 요즘 잘 안 봐서요.", "근데 이번 달 낸 건 환불 안 되나요?", "네, 그래도 해지해 주세요."], 1)]
        await s.aclose()
        return s, outs

    s, outs = asyncio.run(go())
    assert tool_calls(outs[2]) == []  # 결제(10-02) 후 7일이 지나 기간 사유는 견적을 묻지 않는다(s20 expected에도 견적이 없다)
    assert types(outs[2], ev.SUGGESTION_CREATED)[-1]["proposal"]["writes"] == ["create_cancellation"]
    assert types(outs[4], ev.SUGGESTION_INVALIDATED)  # 라벨 변화와 suggestion_valid=false로 무효화는 된다
    assert tool_calls(outs[4]) == ["create_cancellation"]
    assert s.state.facts.receipts["cancel:S016"]["last_usable_date"] == "2026-11-01"
    assert "refund:P016" not in s.state.facts.receipts


class _RaisingDecider(ScriptedDecider):
    async def decide(self, state, questions):
        raise RuntimeError("backend down")


def test_decision_backend_failure_falls_back_to_code_rules(tmp_path):
    async def go():
        s = Session(ORG, "t-fail", tmp_path, decision_client=_RaisingDecider(), generator=EchoGenerator(),
                    bizapi=NoBizApi())
        s.state.request = "cancel_refund"  # 직전 판단이 있으면 그 요청으로 코드 규칙(본인 확인)을 적용한다
        out = await s.handle(utt("u1", "해지해 주세요"))
        out2 = await s.handle(utt("u2", "음"))
        await s.aclose()
        return out, out2

    out, out2 = asyncio.run(go())
    assert not types(out + out2, ev.ENGINE_ERROR)
    resp = types(out, ev.DECISION_RESPONSE)[0]
    assert resp["error"].startswith("RuntimeError") and resp["attempts"] == 1
    assert types(out, ev.AGENT_SAY)[0]["text"] == "[identity_required:ask:phone]"
    assert types(out2, ev.EVIDENCE_UPDATED)[0]["decision_error"].startswith("RuntimeError")


def test_decision_state_shape_and_known_facts_exclude_last_utterance(tmp_path):
    dec = ScriptedDecider(intents=[("환불", "refund")])

    async def go():
        s = Session(ORG, "t-shape", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=NoBizApi())
        await s.handle(utt("u1", "지난주 목요일쯤 결제가 됐는데 환불받고 싶어요"))
        await s.handle(utt("u2", "아 아니다, 칠월 십오일에 결제했어요"))
        await s.aclose()

    asyncio.run(go())
    built = json.loads((ROOT / "data/build/train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    built_state = json.loads(built["state"])
    (s1, q1), (s2, q2) = dec.calls
    for st in (s1, s2):
        assert set(st) - {"previous_suggestion"} == set(built_state) - {"previous_suggestion"}
        assert all(set(u) == {"speaker", "text", "final"} for u in st["conversation"])
        assert all(set(a) == {"id", "title", "text"} for a in st["articles"])
    assert s1["known_facts"] == {} and "condition_changed" not in q1
    # 두 번째 판단: 마지막 발화(칠월 십오일)의 진술은 아직 없고, 앞 발화의 진술과 직전 intent가 있다
    assert s2["known_facts"]["payment_date"] == "2026-10-08(고객 진술)"
    assert s2["known_facts"]["request"] == "refund"
    assert "2026-07-15" not in json.dumps(s2["known_facts"])
    assert "condition_changed" in q2 and "suggestion_valid" in q2
    assert s2["previous_suggestion"] == "[identity_required:ask:phone]"


# --- 제3자, 법정대리인, 부가 상품, 업무 API 장애, 본인 확인 실패, 접수 거절, 예열 ---


def _run(tmp_path, sid, dec, lines, bizapi=None, gen=None, start=False):
    gen = gen or EchoGenerator()

    async def go():
        s = Session(ORG, sid, tmp_path, decision_client=dec, generator=gen,
                    bizapi=BizApiClient(bizapi, sid, retry_delay_s=0.0) if bizapi else NoBizApi())
        if start:
            await s.start()
        outs = [await s.handle(utt(f"u{i}", t)) for i, t in enumerate(lines, 1)]
        await s.aclose()
        return s, outs

    s, outs = asyncio.run(go())
    return s, outs, ev.read_log(tmp_path / f"{sid}.jsonl")


def test_s17_third_party_is_refused_without_identity_or_tools(tmp_path):
    """제3자(남편)는 번호와 인증번호를 말해도 본인 확인 없이 거절한다. 생성 입력에 계정 정보가 없다."""
    dec = ScriptedDecider(intents=[("해지", "cancel_refund")], callers=[("아내 계정", "third_party")])
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s17", dec, [
        "아내 계정으로 연간 결제한 거 있는데요, 아내가 바빠서 제가 대신 해지하려고요.",
        "번호는 010-0000-0003이에요. 그냥 처리해 주시면 안 돼요?",
        "인증번호 아내한테 물어봐서 공공공삼이라고 하면 되잖아요."], gen=gen)  # NoBizApi: 호출하면 실패한다
    assert tool_calls(log) == []
    assert [types(o, ev.AGENT_SAY)[-1]["text"] for o in outs] == ["[third_party:answer:None]"] * 3
    for p in gen.payloads:
        assert p["caller"] == "third_party" and p["system_facts"] == {} and p["customer_claims"] == {}
        assert "A-15" in [a["id"] for a in p["articles"]] and p["refund_claims_need_quote"]
    assert all(e["caller"] == "third_party" for e in types(log, ev.EVIDENCE_UPDATED))


def test_s29_owner_takes_over_after_third_party_refusal(bizapi, tmp_path):
    """딸이 거절 안내를 받은 뒤 본인이 전화를 이어받으면 이전 제안을 무효화하고 본인 확인부터 진행한다."""
    dec = ScriptedDecider(intents=[("해지", "cancel_refund")],
                          callers=[("대신 전화", "third_party"), ("전화 바꿨", "owner")])
    s, outs, log = _run(tmp_path, "t-s29", dec, [
        "아버지 계정으로 연간 결제한 걸 해지하려는데요, 제가 대신 전화했어요.",
        "아버지 번호는 010-0000-0005예요.",
        "여보세요, 전화 바꿨습니다. 제가 정민준입니다. 해지하고 환불해 주세요.",
        "인증번호 공공공오요.", "네, 그렇게 해 주세요."], bizapi=bizapi)
    says = [types(o, ev.AGENT_SAY)[-1]["text"] for o in outs]
    assert says[:3] == ["[third_party:answer:None]", "[third_party:answer:None]", "[identity_required:ask:otp]"]
    inv = types(outs[2], ev.SUGGESTION_INVALIDATED)
    assert inv and "caller_changed:third_party->owner" in inv[0]["reasons"]
    assert tool_calls(outs[0] + outs[1] + outs[2]) == []  # 딸과 통화하는 동안 본인 확인이나 조회를 하지 않는다
    assert tool_calls(outs[4]) == ["create_refund_request", "create_cancellation"]
    assert s.state.facts.receipts["refund:P005"]["amount"] == 19800


def test_owner_return_needs_higher_confidence(tmp_path):
    """제3자로 본 뒤 본인 쪽 확률이 owner_return_threshold보다 낮으면 제3자로 둔다."""
    dec = ScriptedDecider(intents=[("해지", "cancel")], callers=[("대신", "third_party"), ("본인", "owner")],
                          caller_p=0.6)
    s, outs, log = _run(tmp_path, "t-ret", dec, ["딸 계정 대신 해지해 주세요", "제가 본인이에요 그냥 해 주세요"])
    assert ORG.caller_threshold <= 0.6 < ORG.owner_return_threshold
    assert s.state.caller == "third_party" and tool_calls(log) == []


def test_s09_guardian_verifies_minor_account_and_hands_off(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("취소", "cancel_refund")], callers=[("고등학생", "guardian")])
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s09", dec, [
        "저희 딸이 제 카드로 몰래 연간 결제를 했어요. 고등학생인데요, 이거 전부 취소해 주세요.",
        "딸 번호는 010-0000-0009예요.", "인증번호 공공공구요.", "그런 걸 꼭 내야 해요? 알겠어요."], bizapi=bizapi, gen=gen)
    quotes = [e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "quote_refund"]
    assert [q["args"]["reason"] for q in quotes] == ["minor"]  # 연 구독 중도 해지 금액은 견적하지 않는다
    handoff = [e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "create_handoff"]
    assert len(handoff) == 1 and (handoff[0]["args"]["customer_id"], handoff[0]["args"]["reason"]) == ("C009", "minor")
    assert not {"create_refund_request", "create_cancellation"} & set(tool_calls(log))
    assert types(outs[2], ev.AGENT_SAY)[-1]["text"] == "[handoff:handoff:None]"
    handoff_payloads = [p for p in gen.payloads if p["plan"]["action"] == "handoff"]
    assert handoff_payloads and all(p["quotes"] == {} for p in handoff_payloads)
    assert {"A-13", "A-19"} <= {a["id"] for a in handoff_payloads[0]["articles"]}


def test_s14_addon_only_cancellation(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("빼고", "cancel_refund")], addon=("가족 공유",))
    s, outs, log = _run(tmp_path, "t-s14", dec, [
        "가족 공유 추가한 거 있잖아요, 그것만 빼고 싶어요. 이번 달 낸 거는 돌려주시는 거죠?",
        "010-0000-0014예요.", "공공일사요.", "네, 그렇게 해 주세요."], bizapi=bizapi)
    assert "quote_refund" not in tool_calls(log) and "create_refund_request" not in tool_calls(log)
    created = types(outs[2], ev.SUGGESTION_CREATED)[-1]
    assert created["rule"] == "proposal" and created["proposal"]["writes"] == ["create_cancellation"]
    cancel = [e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "create_cancellation"]
    assert [c["args"]["subscription_id"] for c in cancel] == ["S014F"]  # 본 구독(S014)은 해지하지 않는다
    assert s.state.facts.receipts["cancel:S014F"]["last_usable_date"] == "2026-10-29"
    assert "cancel:S014" not in s.state.facts.receipts
    asked = [e["questions"] for e in log if e["type"] == ev.DECISION_REQUEST]
    assert not any("addon_only" in q for q in asked[:3])  # 부가 상품 구독이 조회되기 전에는 묻지 않는다
    assert sum("addon_only" in q for q in asked) == 2  # 조회 직후 패스(u3)와 동의 발화(u4)에서 한 번씩


def test_s19_quote_timeout_holds_and_is_not_retried_in_later_turns(bizapi, tmp_path):
    """재시도 예산(1회)을 다 쓰면 보류하고, 같은 세션의 다음 발화에서 견적을 다시 부르지 않는다."""
    inject_fault(bizapi, "quote_refund", "TIMEOUT", 50)
    dec = ScriptedDecider(intents=[("해지하고 환불", "cancel_refund")])
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s19", dec, [
        "연간 할인으로 가입한 거 해지하고 환불받고 싶어요.", "010-0000-0004예요.", "공공공사요.",
        "대충이라도 얼마인지 알 수 있어요?"], bizapi=bizapi, gen=gen)
    results = [e for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] == "quote_refund"]
    assert results and all(r["attempts"] == 2 and r["error_code"] == "TIMEOUT" for r in results)
    assert tool_calls(outs[3]) == []  # 장애를 기억하고 다시 부르지 않는다
    for o in outs[2:]:
        created = types(o, ev.SUGGESTION_CREATED)[-1]
        assert (created["rule"], created["action"], created["amount"]) == ("api_failure", "hold", None)
    assert not {"create_refund_request", "create_cancellation"} & set(tool_calls(log))
    assert all(p["quotes"] == {} for p in gen.payloads if p["plan"]["action"] == "hold")


def test_s18_two_failed_identity_attempts_hand_off(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("해지", "cancel_refund")])
    s, outs, log = _run(tmp_path, "t-s18", dec, [
        "연간 거 해지요. 빨리 좀 해주세요.", "010-0000-0005요.", "공공공육이요.", "아 왜 또요, 공공공삼이요.",
        "그래서 언제 연결돼요?"], bizapi=bizapi)
    assert tool_calls(log).count("verify_identity") == 2
    handoff = [e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "create_handoff"]
    assert len(handoff) == 1 and handoff[0]["args"]["reason"] == "identity_failed"
    assert types(outs[2], ev.AGENT_SAY)[-1]["text"] == "[identity_required:ask:otp_retry]"
    assert [types(o, ev.AGENT_SAY)[-1]["text"] for o in outs[3:]] == ["[handoff:handoff:None]"] * 2
    assert "quote_refund" not in tool_calls(log)


def test_s25_unknown_number_holds_after_customer_cannot_verify(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("돌려주세요", "refund")], unavailable=("안 왔",))
    s, outs, log = _run(tmp_path, "t-s25", dec, [
        "카드 명세서에 별 뭐시기 구천구백 원이 찍혔는데 나는 가입한 적이 없어요. 돌려주세요.",
        "010-0000-0099요.", "공공구구요.", "다른 문자는 안 왔어요. 이 번호 하나밖에 없어요."], bizapi=bizapi)
    assert tool_calls(log) == ["verify_identity"]
    created = types(outs[3], ev.SUGGESTION_CREATED)[-1]
    assert (created["rule"], created["action"]) == ("identity_unavailable", "hold")
    asked = [e["questions"] for e in log if e["type"] == ev.DECISION_REQUEST]
    assert "identity_unavailable" not in asked[0]  # 본인 확인을 묻기 전에는 묻지 않는다


def test_s11_declined_cancellation_is_not_written(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("해지하고 환불", "cancel_refund")], decline=("해지 안 해요",), agree=())
    s, outs, log = _run(tmp_path, "t-s11", dec, [
        "계정이 갑자기 정지됐는데요, 연간으로 낸 돈은 돌려주셔야죠. 해지하고 환불해 주세요.",
        "010-0000-0011이요.", "공공일일이요.", "환불이 안 되면 해지 안 해요. 정지 풀리면 쓸 거예요."], bizapi=bizapi)
    assert types(outs[2], ev.SUGGESTION_CREATED)[-1]["proposal"]["writes"] == ["create_cancellation"]
    q = types(outs[2], ev.QUOTE_SELECTED)[0]
    assert q["chosen"] is None and q["tried"]["annual_cancel"]["status"] == "not_eligible"
    last = types(outs[3], ev.SUGGESTION_CREATED)[-1]
    assert (last["rule"], last["proposal"]) == ("declined", None)
    assert "create_cancellation" not in tool_calls(log)
    assert types(outs[3], ev.POLICY_DECIDED)[0]["declined"] is True


def test_s08_wife_account_is_not_registered_then_own_account_is(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("돌려주세요", "refund")])
    s, outs, log = _run(tmp_path, "t-s08", dec, [
        "9월에 폰 결제가 두 번 됐더라고요. 한 번 거 돌려주세요.", "010-0000-0008이고 인증번호는 공공공팔이에요.",
        "별담은행 111-222-333 임소라로 넣어주세요.", "아 제 거로 할게요. 별담은행 444-555-666 임태윤입니다.",
        "네, 그렇게 해 주세요."], bizapi=bizapi)
    assert types(outs[1], ev.SUGGESTION_CREATED)[-1]["rule"] == "account_required"
    assert types(outs[2], ev.POLICY_DECIDED)[-1]["ask"] == "own_account" and tool_calls(outs[2]) == []
    assert types(outs[3], ev.SUGGESTION_CREATED)[-1]["proposal"]["writes"] == ["register_refund_account",
                                                                              "create_refund_request"]
    assert tool_calls(outs[4]) == ["register_refund_account", "create_refund_request"]
    reg = next(e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "register_refund_account")
    assert (reg["args"]["bank"], reg["args"]["holder"], reg["args"]["account"]) == ("별담은행", "임태윤", "******666")
    assert s.state.facts.receipts["refund:P008b"]["amount"] == 9900
    assert "create_cancellation" not in tool_calls(log)


def test_s13_withdrawal_refund_request_also_cancels(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("환불 돼요", "refund")])
    s, outs, log = _run(tmp_path, "t-s13", dec, [
        "체험 끝나고 자동으로 결제가 됐더라고요. 체험 때 하나 보긴 했는데, 그래도 환불 돼요?",
        "010-0000-0013이요. 인증번호는 공공일삼이요.", "네, 이대로 환불 접수해 주세요."], bizapi=bizapi)
    assert types(outs[1], ev.SUGGESTION_CREATED)[-1]["proposal"]["writes"] == ["create_refund_request",
                                                                              "create_cancellation"]
    assert tool_calls(outs[2]) == ["create_refund_request", "create_cancellation"]
    assert s.state.facts.receipts["cancel:S013"]["last_usable_date"] == "2026-10-12"


def test_s25_repeated_otp_for_unknown_number_holds_after_one_attempt(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("돌려주세요", "refund")])
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s25b", dec, [
        "카드 명세서에 별 뭐시기 구천구백 원이 찍혔는데 나는 가입한 적이 없어요. 돌려주세요.", "내 번호는 010-0000-0099예요.",
        "문자로 0099라고 왔는데 이거 맞나요?", "문자에 인증번호 0099라고 딱 적혀 있는데 왜 안 맞다고 그래요?",
        "아니 그럼 어떻게 해야 돼요?"], bizapi=bizapi, gen=gen)
    assert tool_calls(log) == ["verify_identity"]  # 같은 번호·인증번호를 다시 보내지 않는다
    assert types(outs[2], ev.SUGGESTION_CREATED)[-1]["rule"] == "identity_required"  # 첫 실패: 다시 확인 요청
    for o in outs[3:]:
        created = types(o, ev.SUGGESTION_CREATED)[-1]
        assert (created["rule"], created["action"]) == ("identity_unavailable", "hold")
    assert types(outs[3], ev.POLICY_DECIDED)[-1]["detail"]["repeated_failed_otp"] is True


def test_s03_approval_required_proposal_must_mention_approval(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("해지하고 남은 돈", "cancel_refund")])
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s03", dec, [
        "연간으로 결제한 거 해지하고 남은 돈 돌려받고 싶어서요.", "010-0000-0003이고 인증번호 공공공삼이요.",
        "네, 진행해 주세요."], bizapi=bizapi, gen=gen)
    prop = [p for p in gen.payloads if p["plan"]["rule"] == "proposal"][-1]
    assert prop["plan"]["must_mention"] == ["승인"] and ORG.policy.approval_notice in prop["plan"]["notices"]
    assert "A-19" in {a["id"] for a in prop["articles"]}
    assert s.state.facts.receipts["refund:P003"]["status"] == "pending_approval"
    received = [p for p in gen.payloads if p["plan"]["rule"] == "received"][-1]
    assert received["plan"]["must_mention"] == ["승인"]


def test_s16_plan_change_quote_is_a_deduction_and_identity_is_not_asked_again(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("연간으로 바꾸", "plan_change")], action="ask", next_info="identity")
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s16", dec, [
        "지금 월 구독 쓰고 있는데 연간으로 바꾸고 싶어요. 이번 달 낸 구천구백 원은 그냥 날아가요?",
        "010-0000-0016이고 인증번호 공공일육이요.", "연간 할인가 칠만 구천 원으로 되나요?"], bizapi=bizapi, gen=gen)
    assert [e["args"]["reason"] for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "quote_refund"] == \
        ["plan_change"]
    for o in outs[1:]:  # 본인 확인 뒤에는 판별이 identity를 내도 번호를 다시 묻지 않는다
        created = types(o, ev.SUGGESTION_CREATED)[-1]
        assert (created["rule"], created["action"]) == ("decision", "answer")
    assert all(p["quotes"]["plan_change"]["kind"] == "deduction" for p in gen.payloads if p["quotes"])


def test_s27_compensation_hours_from_incident_record(bizapi, tmp_path):
    dec = ScriptedDecider(intents=[("해지하고 환불", "cancel_refund"), ("보상만", "compensation")],
                          changes=("보상만",), agree=())
    gen = EchoGenerator()
    s, outs, log = _run(tmp_path, "t-s27", dec, [
        "지난주 목요일에 장애 났잖아요. 결제하고 일주일 안에 장애 나면 전액 환불이라던데 해지하고 환불해 주세요.",
        "010-0000-0016이고 인증번호는 공공일육이에요.", "환불이 안 되면 해지는 안 하고 보상만 받을게요."],
        bizapi=bizapi, gen=gen)
    p = types(outs[2], ev.POLICY_DECIDED)[-1]
    assert (p["rule"], p["must_mention"]) == ("compensation", ["15시간"])
    assert p["detail"]["compensation"][0]["incident_id"] == "INC-2026-1008"
    assert not {"create_cancellation", "create_refund_request"} & set(tool_calls(log))
    assert gen.payloads[-1]["plan"]["detail"]["applied"] is False
    # 주장한 장애 환불은 기간이 지났어도 한 번 견적하고, 업무 API가 대상 아님을 돌려준다
    quotes = [e for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] == "quote_refund"]
    assert [(q["data"]["quote"]["reason"], q["error_code"]) for q in quotes] == [("incident", "NOT_ELIGIBLE")]
    assert "A-09" in quotes[0]["data"]["article_ids"]


def test_s19_annual_request_quotes_only_annual_cancel(bizapi, tmp_path):
    inject_fault(bizapi, "quote_refund", "TIMEOUT", 50)
    dec = ScriptedDecider(intents=[("해지하고 환불", "cancel_refund")])
    s, outs, log = _run(tmp_path, "t-s19b", dec, [
        "연간 할인으로 가입한 거 해지하고 환불받고 싶어요.", "010-0000-0004이고 인증번호는 공공공사요.",
        "대충이라도 얼마인지 알 수 있어요?"], bizapi=bizapi)
    quotes = [e for e in log if e["type"] == ev.TOOL_CALL and e["tool"] == "quote_refund"]
    results = [e for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] == "quote_refund"]
    assert [q["args"]["reason"] for q in quotes] == ["annual_cancel"] and [r["attempts"] for r in results] == [2]
    assert types(outs[2], ev.SUGGESTION_CREATED)[-1]["action"] == "hold"


def test_warmup_is_logged_separately_and_not_counted_as_turn_decision(tmp_path):
    dec = ScriptedDecider(intents=[("가입 전", "other")])
    s, outs, log = _run(tmp_path, "t-warm", dec, ["가입 전에 환불 규정이 궁금해요"], start=True)
    warm = types(log, ev.WARMUP)
    assert len(warm) == 1 and warm[0]["error"] is None and "latency_ms" in warm[0]
    order = [e["type"] for e in log]
    assert order.index(ev.WARMUP) < order.index(ev.UTTERANCE_END)
    assert len(types(log, ev.DECISION_REQUEST)) == len(types(log, ev.DECISION_RESPONSE)) == 1
    assert len(dec.calls) == 2 and dec.calls[0][1] == ["intent"]  # 예열 1회 + 턴 판단 1회
    assert dec.calls[0][0]["conversation"][0]["text"] == "안녕하세요"  # 측정 데이터가 아닌 고정 요청
    assert ev.turn_latencies(log)[0]["decision_ms"] == [1.0]


def test_utterance_during_warmup_waits_for_it(tmp_path):
    dec = GatedDecider(False, intents=[("가입 전", "other")])

    async def go():
        s = Session(ORG, "t-warm2", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=NoBizApi())
        w = asyncio.create_task(s.start())
        await dec.entered.wait()  # 예열 요청이 백엔드에 들어가 있다
        h = asyncio.create_task(s.handle(utt("u1", "가입 전에 궁금해요")))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(dec.calls) == 0  # 턴 판단은 예열이 끝나기를 기다린다
        dec.release.set()
        await w
        out = await h
        await s.aclose()
        return out

    out = asyncio.run(go())
    log = ev.read_log(tmp_path / "t-warm2.jsonl")
    assert len(dec.calls) == 2 and types(out, ev.AGENT_SAY)
    assert [e["type"] for e in log].index(ev.WARMUP) < [e["type"] for e in log].index(ev.DECISION_REQUEST)
