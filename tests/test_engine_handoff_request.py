"""상담사 인계 요청(Session.request_handoff). 웹 상담 콘솔의 인계 버튼이 부르는 진입점이다.

인계는 엔진 정책 규칙 1이 진행한다: 인계 사유를 시스템 사실에 두고 버전을 올리면, 마지막 고객 발화로 다시 진행한
파이프라인이 create_handoff를 부르고 인계 안내를 낸다. 확인하는 것:
- 쉬는 세션에서 부르면 create_handoff가 한 번 불리고 인계 제안이 하나 나간다.
- 앞 턴이 진행 중일 때 부르면 진행 중인 작업을 취소하고, 같은 발화의 _drive가 새 버전으로 한 번만 다시 진행한다
  (같은 버전의 제안이 두 번 나가지 않는다).
- 고객 발화가 없으면 아무것도 하지 않는다. 이미 있는 인계 사유는 바꾸지 않는다.
- 제3자 통화자에게는 정책 규칙 0(거절)이 앞선다(엔진 규칙을 우회하지 않는다).
"""

import asyncio

from engine_fakes import EchoGenerator, GatedDecider, ScriptedDecider, utt

from jikgam.engine import events as ev
from jikgam.engine.bizapi import ToolResult
from jikgam.engine.org import load_org
from jikgam.engine.session import Session

ORG = load_org("org_a")


class HandoffOnlyBiz:
    """create_handoff만 받는 가짜 업무 API. 다른 도구가 불리면 실패한다."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool, args, target=None):
        assert tool == "create_handoff", f"인계 외 도구를 부르면 안 된다: {tool}"
        self.calls.append((tool, dict(args)))
        return ToolResult(tool, args, True, data={"handoff_id": "HO-1", "reason": args["reason"],
                                                  "expected_wait_minutes": 3}, status=200)

    async def aclose(self) -> None:
        pass


def types(events, *wanted):
    return [e for e in events if e["type"] in wanted]


def test_handoff_request_on_idle_session_calls_create_handoff_once(tmp_path):
    biz = HandoffOnlyBiz()

    async def go():
        s = Session(ORG, "t-ho-idle", tmp_path, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                    generator=EchoGenerator(), bizapi=biz)
        await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        out = await s.request_handoff()
        await s.aclose()
        return s, out

    s, out = asyncio.run(go())
    bump = out[0]
    assert bump["type"] == ev.VERSION_BUMPED and bump["cause"] == "agent_handoff" and bump["reason"] == "agent_request"
    assert [c[1]["reason"] for c in biz.calls] == ["agent_request"]
    says = types(out, ev.AGENT_SAY)
    assert [e["text"] for e in says] == ["[handoff:handoff:None]"]
    assert s.state.facts.receipts["handoff"]["handoff_id"] == "HO-1"
    log = ev.read_log(tmp_path / "t-ho-idle.jsonl")
    assert len(types(log, ev.SUGGESTION_EXPOSED)) == 2  # 첫 턴 하나, 인계 하나


def test_handoff_request_during_running_turn_restarts_once(tmp_path):
    biz = HandoffOnlyBiz()
    dec = GatedDecider(ignore_cancel=False, intents=[("궁금", "other")])

    async def go():
        s = Session(ORG, "t-ho-busy", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=biz)
        t1 = asyncio.create_task(s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요")))
        await dec.entered.wait()  # 첫 판별이 진행 중이다
        out = await s.request_handoff()
        dec.release.set()
        o1 = await t1
        await s.aclose()
        return out, o1

    out, o1 = asyncio.run(go())
    assert [e["type"] for e in out] == [ev.VERSION_BUMPED]  # 진행 중이면 버전만 올리고 _drive가 다시 진행한다
    cancelled = types(o1, ev.TASK_CANCELLED)
    assert len(cancelled) == 1 and cancelled[0]["started_version"] == 1
    log = ev.read_log(tmp_path / "t-ho-busy.jsonl")
    created = types(log, ev.SUGGESTION_CREATED)
    assert [e["rule"] for e in created] == ["handoff"]
    assert len({e["basis_version"] for e in created}) == len(created)
    assert len(biz.calls) == 1


def test_repeated_handoff_requests_during_a_turn_do_not_duplicate_pipelines(tmp_path):
    """검토에서 재현된 경우: 진행 중인 턴에 인계를 요청하고 한 번 양보한 뒤 다시 요청한다. 앞 요청이 취소한 작업이
    끝났지만 그 발화의 _drive가 아직 깨어나기 전이면, 예전 구현은 두 번째 요청이 파이프라인을 따로 만들어 같은 버전의
    제안이 두 번 나갔다."""
    biz = HandoffOnlyBiz()
    dec = GatedDecider(ignore_cancel=False, intents=[("궁금", "other")])

    async def go():
        s = Session(ORG, "t-ho-twice", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=biz)
        s.state.facts.receipts["handoff"] = {"handoff_id": "HO-0"}  # 쓰기가 없어 버전이 다시 오르지 않는 경우
        t1 = asyncio.create_task(s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요")))
        await dec.entered.wait()
        await s.request_handoff()
        await asyncio.sleep(0)
        await s.request_handoff()
        dec.release.set()
        await t1
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-ho-twice.jsonl")
    bases = [e["basis_version"] for e in types(log, ev.SUGGESTION_CREATED)]
    assert len(bases) == len(set(bases)) == 1
    assert [e["rule"] for e in types(log, ev.SUGGESTION_CREATED)] == ["handoff"]


def test_handoff_request_right_after_turn_finishes_still_hands_off(tmp_path):
    """앞 턴의 파이프라인이 막 끝났고 그 발화의 _drive가 아직 깨어나지 않은 순간에 요청해도 인계가 진행된다."""
    biz = HandoffOnlyBiz()

    async def go():
        s = Session(ORG, "t-ho-edge", tmp_path, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                    generator=EchoGenerator(), bizapi=biz)
        t1 = asyncio.create_task(s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요")))
        while s._task is None or not s._task.done():  # 파이프라인이 끝나는 순간을 잡는다
            await asyncio.sleep(0)
        out = await s.request_handoff()
        await t1
        await s.aclose()
        return out

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-ho-edge.jsonl")
    created = types(log, ev.SUGGESTION_CREATED)
    assert [e["rule"] for e in created] == ["decision", "handoff"]
    assert len({e["basis_version"] for e in created}) == 2 and len(biz.calls) == 1


def test_handoff_request_without_customer_utterance_does_nothing(tmp_path):
    biz = HandoffOnlyBiz()

    async def go():
        s = Session(ORG, "t-ho-empty", tmp_path, decision_client=ScriptedDecider(), generator=EchoGenerator(), bizapi=biz)
        out = await s.request_handoff()
        version = s.version
        await s.aclose()
        return out, version

    out, version = asyncio.run(go())
    assert out == [] and version == 0 and biz.calls == []
    assert [e["type"] for e in ev.read_log(tmp_path / "t-ho-empty.jsonl")] == [ev.SESSION_START]


def test_existing_handoff_reason_is_kept(tmp_path):
    biz = HandoffOnlyBiz()

    async def go():
        s = Session(ORG, "t-ho-keep", tmp_path, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                    generator=EchoGenerator(), bizapi=biz)
        await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        s.state.facts.handoff_reason = "identity_failed"  # 업무 API가 앞서 인계 사유를 준 경우
        s.state.facts.receipts["handoff"] = {"handoff_id": "HO-0"}  # 이미 인계 접수됨
        out = await s.request_handoff()
        await s.aclose()
        return s, out

    s, out = asyncio.run(go())
    assert s.state.facts.handoff_reason == "identity_failed"
    assert biz.calls == []  # 이미 접수된 인계는 다시 부르지 않는다(정책 규칙 1)
    assert [e["text"] for e in types(out, ev.AGENT_SAY)] == ["[handoff:handoff:None]"]


def test_third_party_caller_is_refused_not_handed_off(tmp_path):
    biz = HandoffOnlyBiz()
    dec = ScriptedDecider(intents=[("해지", "cancel")], callers=[("딸인데", "third_party")])

    async def go():
        s = Session(ORG, "t-ho-3p", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=biz)
        await s.handle(utt("u1", "저 딸인데요, 엄마 구독 해지하려고요"))
        out = await s.request_handoff()
        await s.aclose()
        return out

    out = asyncio.run(go())
    assert biz.calls == []
    assert [e["rule"] for e in types(out, ev.SUGGESTION_CREATED)] == ["third_party"]
