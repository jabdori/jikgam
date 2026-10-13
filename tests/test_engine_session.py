import asyncio
import json
from datetime import datetime
from pathlib import Path

from engine_fakes import EchoGenerator, GatedDecider, NoBizApi, ScriptedDecider, utt

from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
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
    assert s3["intent"] == "compensation" and s3["rule"] == "decision"
    inv = types(outs[3], ev.SUGGESTION_INVALIDATED)
    assert [e["suggestion_id"] for e in inv] == [s3["suggestion_id"]]
    assert "condition_changed=true" in inv[0]["reasons"] and inv[0]["invalidated_version"] == outs[3][1]["to"]
    order = [e["type"] for e in outs[3]]
    assert order.index(ev.SUGGESTION_INVALIDATED) < order.index(ev.EVIDENCE_UPDATED)
    q = types(outs[3], ev.QUOTE_SELECTED)[0]
    assert q["chosen"] == "incident" and q["tried"]["withdrawal"]["status"] == "not_eligible"
    assert tool_calls(outs[4]) == ["create_refund_request", "create_cancellation"]
    refund = [e for e in outs[4] if e["type"] == ev.TOOL_CALL][0]
    assert refund["args"]["payment_id"] == "P010"


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
