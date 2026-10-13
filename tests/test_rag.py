"""정책 A(개선형 RAG) 기준선과 재생 실행기의 장애 주입. LLM은 가짜, 업무 API는 Go 모의 서버를 쓴다."""

import asyncio
import copy
import json

import pytest
from engine_fakes import EchoGenerator, ScriptedDecider, utt

from jikgam.baselines.rag import FINAL, LLMStep, RagSession
from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient, idempotency_key
from jikgam.engine.chat import inject_faults, make_session, replay, report, session_cost
from jikgam.engine.org import load_org
from jikgam.engine.session import Session

ORG = load_org("org_a")


def final(say, cited=(), amount=None, amount_final=False, action="answer"):
    return {"name": FINAL, "args": {"say": say, "cited_articles": list(cited), "amount": amount,
                                    "amount_final": amount_final, "action": action}}


def last_response(contents, tool):
    """대화 기록에서 마지막 tool 함수 결과."""
    for c in reversed(contents):
        for p in c.get("parts", []):
            fr = p.get("functionResponse")
            if fr and fr["name"] == tool:
                return fr["response"]
    raise AssertionError(f"{tool} 결과가 없다")


class ScriptedLLM:
    """단계마다 정해 둔 함수 호출을 낸다. 항목은 호출 목록이거나 대화 기록을 받아 호출 목록을 돌려주는 함수다."""

    name = model = "fake-llm"

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    async def step(self, system, contents, tools):
        self.seen.append(copy.deepcopy(contents))
        if not self.steps:
            return LLMStep(None, [], self.model, error="대본 끝")
        s = self.steps.pop(0)
        calls = s(contents) if callable(s) else s
        content = {"role": "model", "parts": [{"functionCall": c} for c in calls]}
        return LLMStep(content, calls, self.model, latency_ms=5.0, input_tokens=100, output_tokens=10, cost_usd=0.0001)

    async def aclose(self):
        pass


def types(events, *wanted):
    return [e for e in events if e["type"] in wanted]


def _run(tmp_path, sid, llm, lines, bizapi=None):
    async def go():
        s = RagSession(ORG, sid, tmp_path, llm=llm,
                       bizapi=BizApiClient(bizapi or "http://127.0.0.1:9", sid, retry_delay_s=0.0))
        await s.start()
        outs = [await s.handle(utt(f"u{i}", t)) for i, t in enumerate(lines, 1)]
        await s.aclose()
        return s, outs

    s, outs = asyncio.run(go())
    return s, outs, ev.read_log(tmp_path / f"{sid}.jsonl")


def test_rag_tool_loop_writes_engine_event_format(bizapi, tmp_path):
    llm = ScriptedLLM([
        [final("본인 확인을 위해 휴대폰 번호를 알려 주세요.", action="ask")],
        [final("인증번호를 알려 주세요.", action="ask")],
        [{"name": "verify_identity", "args": {"phone": "010-0000-0001", "otp": "0001", "extra": "x"}}],  # 선언 밖 인자는 뺀다
        [{"name": "list_payments", "args": {"customer_id": "C001"}}, {"name": "get_usage", "args": {"customer_id": "C001"}}],
        [{"name": "quote_refund", "args": {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal",
                                           "request_date": "2026-10-12"}}],
        [final("청약철회로 9,900원 전액 환불됩니다. 진행할까요?", ("A-05", "A-99"), 9900, True)],
        lambda contents: [{"name": "create_refund_request", "args": {
            "customer_id": "C001", "payment_id": "P001",
            "quote_id": last_response(contents, "quote_refund")["data"]["quote_id"]}},
            {"name": "create_cancellation", "args": {"customer_id": "C001", "subscription_id": "S001"}}],
        [final("환불과 해지를 접수했습니다.", ("A-05", "A-14"), 9900, True)],
    ])
    s, outs, log = _run(tmp_path, "r-s01", llm, [
        "지난주에 결제됐는데 안 써서 취소하고 환불받고 싶어요.", "010-0000-0001이에요.", "공공공일이요.", "네, 진행해 주세요."],
        bizapi)
    assert log[0]["type"] == ev.SESSION_START and log[0]["policy"] == "A" and log[0]["engine"] == "rag"
    # 대화 버전·무효화 구조가 없다
    assert not types(log, ev.VERSION_BUMPED, ev.SUGGESTION_INVALIDATED, ev.RESULT_DISCARDED, ev.TASK_CANCELLED)
    # 근거 갱신·제안·노출은 발화마다 하나, 기준 버전은 그 답을 만들 때 본 고객 확정 발화 수
    ups = types(log, ev.EVIDENCE_UPDATED)
    assert [e["version"] for e in ups] == [1, 2, 3, 4]
    assert [e["basis_version"] for e in types(log, ev.SUGGESTION_CREATED)] == [1, 2, 3, 4]
    assert [e["version"] for e in types(log, ev.UTTERANCE_END)] == [0, 1, 2, 3]
    assert ups[2]["articles"] == ["A-05"] and ups[2]["cited_raw"] == ["A-05", "A-99"]  # 색인에 없는 조항은 근거에서 뺀다
    say3 = types(outs[2], ev.AGENT_SAY)[0]
    assert (say3["amount"], say3["amount_final"], say3["cited_articles"]) == (9900, True, ["A-05"])
    # 도구 호출은 엔진과 같은 형식. 쓰기에는 (세션, 도구, 대상) 멱등 키
    calls = types(log, ev.TOOL_CALL)
    assert [c["tool"] for c in calls] == ["verify_identity", "list_payments", "get_usage", "quote_refund",
                                          "create_refund_request", "create_cancellation"]
    assert calls[0]["args"] == {"phone": "010-0000-0001", "otp": "****"}  # 인증번호를 가리고 선언 밖 인자는 없다
    results = types(log, ev.TOOL_RESULT)
    assert all(r["ok"] for r in results)
    refund = next(r for r in results if r["tool"] == "create_refund_request")
    assert refund["idempotency_key"] == idempotency_key("r-s01", "create_refund_request", "refund:P001")
    # 같은 지표 코드가 동작한다
    lat = ev.turn_latencies(log)
    assert all(t["evidence_ms"] is not None and t["say_ms"] is not None for t in lat)
    assert lat[2]["tool_ms"] and len(lat[2]["generation_ms"]) == 4
    text = report(log)
    assert "create_cancellation(customer_id=C001, subscription_id=S001)" in text and "생성 8회" in text
    assert session_cost(log) == pytest.approx(8 * 0.0001)  # LLM 요청 1 + 1 + 4 + 2
    # 대화 기록은 사용자와 모델이 번갈아 오고, 이전 답과 도구 결과가 다음 발화에 이어진다
    roles = [c["role"] for c in llm.seen[-1]]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert any(p.get("text") == "청약철회로 9,900원 전액 환불됩니다. 진행할까요?" for c in llm.seen[-1] for p in c["parts"])
    first_user = json.loads(llm.seen[0][0]["parts"][0]["text"])
    assert first_user["customer_utterance"].startswith("지난주") and first_user["retrieved_articles"]
    assert {"A-15"} <= {a["id"] for a in first_user["retrieved_articles"]}  # 엔진과 같은 검색(본인 확인 전 절차 조항)


def test_rag_does_not_cancel_in_flight_generation(tmp_path):
    """앞 답을 만드는 중에 새 발화가 와도 취소하지 않는다. 앞 답은 끝나는 대로 노출되고, 새 발화는 그 뒤에 답한다."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    class GatedLLM(ScriptedLLM):
        async def step(self, system, contents, tools):
            if not entered.is_set():
                entered.set()
                await gate.wait()
            return await super().step(system, contents, tools)

    llm = GatedLLM([[final("연장 보상으로 처리해 드릴까요?")], [final("해지와 환불 여부를 확인하겠습니다.")]])

    async def go():
        s = RagSession(ORG, "r-gate", tmp_path, llm=llm, bizapi=BizApiClient("http://127.0.0.1:9", "r-gate"))
        t1 = asyncio.create_task(s.handle(utt("u1", "장애 보상 있어요?")))
        await entered.wait()
        t2 = asyncio.create_task(s.handle(utt("u2", "연장 말고 해지하고 환불해 주세요",
                                               marker={"condition_change": True, "invalidates_prior": True})))
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()
        o1, o2 = await t1, await t2
        await s.aclose()
        return o1, o2

    o1, o2 = asyncio.run(go())
    log = ev.read_log(tmp_path / "r-gate.jsonl")
    order = [(e["type"], e.get("utt_id") or e.get("suggestion_id")) for e in log
             if e["type"] in (ev.UTTERANCE_END, ev.SUGGESTION_EXPOSED)]
    assert order == [(ev.UTTERANCE_END, "u1"), (ev.UTTERANCE_END, "u2"), (ev.SUGGESTION_EXPOSED, "r-gate-s1"),
                     (ev.SUGGESTION_EXPOSED, "r-gate-s2")]
    s1 = types(log, ev.SUGGESTION_CREATED)[0]
    assert s1["basis_version"] == 1  # 조건 변경 뒤에 노출된 지난 조건의 답(무효 노출로 세어진다)
    assert not types(log, ev.TASK_CANCELLED, ev.RESULT_DISCARDED, ev.SUGGESTION_INVALIDATED)
    texts = [json.loads(c["parts"][0]["text"])["customer_utterance"] for c in llm.seen[0] if c["role"] == "user"]
    assert texts == ["장애 보상 있어요?"]  # 첫 답은 새 발화를 보지 않았다
    lat = ev.turn_latencies(log)
    assert lat[1]["marker"]["condition_change"] and lat[1]["evidence_ms"] is not None


def test_rag_fallback_on_llm_error_and_step_limit(tmp_path):
    llm = ScriptedLLM([])  # 첫 요청부터 오류
    s, outs, log = _run(tmp_path, "r-err", llm, ["환불해 주세요"])
    created = types(log, ev.SUGGESTION_CREATED)[0]
    assert created["fallback"] and created["action"] == "hold" and created["amount"] is None
    assert types(log, ev.EVIDENCE_UPDATED)[0]["error"] == "대본 끝"
    loop = ScriptedLLM([[{"name": "list_incidents", "args": {}}]] * 3)

    async def go():
        s = RagSession(ORG, "r-loop", tmp_path, llm=loop, bizapi=BizApiClient("http://127.0.0.1:9", "r-loop",
                                                                              retry_delay_s=0.0, timeout_s=1.0),
                       max_steps=2)
        await s.handle(utt("u1", "장애 있었어요?"))
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "r-loop.jsonl")
    assert len(types(log, ev.GENERATION_RESPONSE)) == 2 and types(log, ev.SUGGESTION_CREATED)[0]["fallback"]
    assert types(log, ev.TOOL_RESULT)[0]["error_code"] == "UNAVAILABLE"  # 업무 API가 없으면 장애로 돌려준다


def test_rag_unknown_tool_is_reported_back_to_model(tmp_path):
    llm = ScriptedLLM([[{"name": "delete_account", "args": {"customer_id": "C001"}}], [final("확인하겠습니다.")]])
    s, outs, log = _run(tmp_path, "r-unk", llm, ["계정 지워 주세요"])
    assert types(log, ev.TOOL_RESULT)[0]["error_code"] == "UNKNOWN_TOOL"
    assert last_response(llm.seen[-1], "delete_account") == {"ok": False, "error": {
        "code": "UNKNOWN_TOOL", "message": "없는 도구: delete_account"}}


def test_policy_a_rejects_decision_backend(tmp_path):
    with pytest.raises(SystemExit):
        make_session("A", "jev", ORG, "x", tmp_path)


def test_replay_faults_field_injects_before_the_line(bizapi, tmp_path):
    """재생 파일의 faults는 그 줄을 보내기 직전에 모의 업무 API에 들어가고 세션 로그에 남는다(s19)."""
    path = tmp_path / "s19.jsonl"
    rows = [{"text": "연간 할인으로 가입한 거 해지하고 환불받고 싶어요."}, {"text": "010-0000-0004예요."},
            {"text": "공공공사요.", "faults": [{"tool": "quote_refund", "code": "TIMEOUT", "count": 50}]}]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    dec = ScriptedDecider(intents=[("해지하고 환불", "cancel_refund")])

    async def go():
        s = Session(ORG, "t-replay-fault", tmp_path, decision_client=dec, generator=EchoGenerator(),
                    bizapi=BizApiClient(bizapi, "t-replay-fault", retry_delay_s=0.0))
        await replay(s, path, bizapi)
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-replay-fault.jsonl")
    inj = types(log, ev.FAULT_INJECTED)
    assert len(inj) == 1 and inj[0]["before_utt"] == "u3" and inj[0]["faults"][0]["count"] == 50
    order = [e["type"] for e in log]
    assert order.index(ev.FAULT_INJECTED) > max(i for i, e in enumerate(log) if e.get("utt_id") == "u2")
    quotes = [e for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] == "quote_refund"]
    assert quotes and all(q["error_code"] == "TIMEOUT" for q in quotes)
    with pytest.raises(ValueError):
        inject_faults(bizapi, [{"tool": "quote_refund", "code": "TIMEOUT", "delay": 3}])
    with pytest.raises(SystemExit):
        inject_faults("http://10.0.0.1:8081", [{"tool": "quote_refund", "code": "TIMEOUT"}])
