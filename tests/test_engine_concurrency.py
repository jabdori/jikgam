"""빠른 정정: 앞 발화를 처리하는 도중에 새 고객 발화가 들어오는 경우(docs/evaluation.md §3.3 보완, 2026-10-14).

시나리오 실행기는 조건 변경 발화를 직전 고객 발화 1,000ms 뒤에 엔진 응답을 기다리지 않고 보낸다. 그래서 Session.handle이
앞 턴이 진행 중일 때 불린다. 여기서 확인하는 것:
- 앞 턴의 진행 작업은 취소되거나(task.cancelled) 결과가 버려진다(result.discarded).
- 새 발화의 utterance.end 뒤에 지난 버전으로 만든 제안이 노출되지 않는다(아래 exposure_invariant).
- 쓰기는 중간에 취소되지 않는다(시작한 쓰기 묶음은 끝까지 가고, 결과로 버전을 올려 최신 발화로 다시 진행한다).
"""

import asyncio

import pytest
from engine_fakes import (DelayedDecider, DelayedGenerator, EchoGenerator, GatedGenerator, NoBizApi, RandomDelay,
                          ScriptedDecider, utt)

from jikgam.engine import events as ev
from jikgam.engine.bizapi import WRITE_TOOLS, BizApiClient
from jikgam.engine.org import load_org
from jikgam.engine.session import Session

ORG = load_org("org_a")


def types(events, *wanted):
    return [e for e in events if e["type"] in wanted]


def exposure_violations(log: list[dict]) -> list[str]:
    """새 고객 발화가 들어온 뒤에 그 전 버전으로 만든 제안이 노출된 경우.

    utterance.end의 version은 그 발화가 버전을 올리기 전 값이다. 그 발화는 version+1을 만들므로, 기준 버전 b인 제안은
    version ≥ b인 utterance.end보다 뒤에 노출되면 지난 제안이다(docs/evaluation.md §3.3의 무효 노출보다 엄격한 조건:
    표시와 관계없이 모든 새 발화를 본다).
    """
    out = []
    ends = [e for e in log if e["type"] == ev.UTTERANCE_END and e.get("speaker") == "customer"]
    created = {e["suggestion_id"]: e for e in log if e["type"] == ev.SUGGESTION_CREATED}
    for x in types(log, ev.SUGGESTION_EXPOSED):
        b = created[x["suggestion_id"]]["basis_version"]
        for u in ends:
            if u["t_ms"] <= x["t_ms"] and u["version"] >= b:
                out.append(f"{x['suggestion_id']}(v{b}) exposed after {u['utt_id']}(v{u['version']}→)")
    return out


@pytest.mark.parametrize("ignore_cancel", [False, True])
def test_new_utterance_during_generation_cancels_or_discards_previous_turn(tmp_path, ignore_cancel):
    dec = ScriptedDecider(intents=[("보상", "other"), ("연장 말고", "other")])
    gen = GatedGenerator(ignore_cancel=ignore_cancel)

    async def go():
        s = Session(ORG, "t-fast-gen", tmp_path, decision_client=dec, generator=gen, bizapi=NoBizApi())
        t1 = asyncio.create_task(s.handle(utt("u1", "장애 보상 같은 거 없어요?")))
        await gen.entered.wait()  # 앞 턴이 생성 중이다
        t2 = asyncio.create_task(s.handle(utt("u2", "연장 말고 돈으로 돌려받을 수 있어요?",
                                               marker={"condition_change": True, "invalidates_prior": True})))
        while s.state.version < 2:
            await asyncio.sleep(0)
        gen.release.set()
        o1, o2 = await t1, await t2
        await s.aclose()
        return o1, o2

    o1, o2 = asyncio.run(go())
    log = ev.read_log(tmp_path / "t-fast-gen.jsonl")
    if ignore_cancel:  # 생성이 취소를 무시하고 끝나도 결과를 쓰지 않는다
        disc = types(o1, ev.RESULT_DISCARDED)
        assert [(d["stage"], d["started_version"]) for d in disc] == [("generation", 1)]
    else:
        canc = types(o1, ev.TASK_CANCELLED)
        assert [(c["stage"], c["started_version"]) for c in canc] == [("generation", 1)]
    assert not types(o1, ev.SUGGESTION_CREATED, ev.SUGGESTION_EXPOSED, ev.AGENT_SAY)
    assert [e["basis_version"] for e in types(log, ev.SUGGESTION_CREATED)] == [2]
    assert exposure_violations(log) == []


class GatedWrites(BizApiClient):
    """create_refund_request를 release 전까지 붙잡는 업무 API 클라이언트(실제 모의 서버에 보낸다)."""

    def __init__(self, url: str, session_id: str):
        super().__init__(url, session_id, retry_delay_s=0.0)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished: list[str] = []

    async def call(self, tool, args, target=None):
        if tool == "create_refund_request":
            self.entered.set()
            await self.release.wait()
        r = await super().call(tool, args, target=target)
        if tool in WRITE_TOOLS:
            self.finished.append(tool)
        return r


def test_writes_are_not_cancelled_by_a_fast_correction(bizapi, tmp_path):
    """동의 뒤 쓰기 도중 고객이 정정해도 쓰기 묶음은 끝까지 가고, 결과로 버전을 올려 최신 발화로 다시 진행한다."""
    dec = ScriptedDecider(intents=[("취소하고 환불", "cancel_refund")], changes=("잠깐만요",))

    async def go():
        biz = GatedWrites(bizapi, "t-fast-write")
        s = Session(ORG, "t-fast-write", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=biz)
        await s.handle(utt("u1", "결제했는데 안 쓸 거라서 취소하고 환불받고 싶어요."))
        await s.handle(utt("u2", "010-0000-0001이고 인증번호는 공공공일이요."))
        t3 = asyncio.create_task(s.handle(utt("u3", "네, 그렇게 진행해 주세요.")))
        await biz.entered.wait()  # 환불 접수가 진행 중이다
        t4 = asyncio.create_task(s.handle(utt("u4", "아 잠깐만요, 이거 언제 들어와요?",
                                               marker={"condition_change": True, "invalidates_prior": False})))
        for _ in range(500):  # 새 발화의 파이프라인이 정책 단계에 와서 쓰기를 기다릴 때까지(최대 약 5초)
            if any(e["type"] == ev.WRITES_WAIT for e in ev.read_log(tmp_path / "t-fast-write.jsonl")):
                break
            await asyncio.sleep(0.01)
        biz.release.set()
        o3, o4 = await t3, await t4
        await s.aclose()
        return s, biz, o3, o4

    s, biz, o3, o4 = asyncio.run(go())
    log = ev.read_log(tmp_path / "t-fast-write.jsonl")
    assert biz.finished == ["create_refund_request", "create_cancellation"]  # 묶음이 끝까지 실행됐다
    results = [e for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] in WRITE_TOOLS]
    assert [(r["tool"], r["ok"]) for r in results] == [("create_refund_request", True), ("create_cancellation", True)]
    assert s.state.facts.receipts["refund:P001"] and s.state.facts.receipts["cancel:S001"]
    # 앞 턴은 도구 단계에서 취소되어 아무것도 노출하지 않았다
    assert [c["stage"] for c in types(o3, ev.TASK_CANCELLED)] == ["tools"]
    assert not types(o3, ev.SUGGESTION_EXPOSED)
    # 쓰기 결과로 버전이 오르고(background) 새 발화로 다시 진행해 접수 결과를 안내했다
    bg = [e for e in log if e["type"] == ev.VERSION_BUMPED and e.get("background")]
    assert len(bg) == 1
    # 새 발화의 파이프라인은 쓰기가 끝나기를 기다린 뒤에 정책을 정한다(접수 중인 내용을 다시 안내하지 않는다)
    assert types(o4, ev.WRITES_WAIT)
    created = types(o4, ev.SUGGESTION_CREATED)
    assert len(created) == 1 and created[0]["rule"] == "received" and created[0]["basis_version"] >= bg[0]["to"]
    assert exposure_violations(log) == []


@pytest.mark.parametrize("seed", range(8))
def test_random_overlaps_never_expose_a_stale_suggestion(tmp_path, seed):
    """판별·생성 지연과 발화 간격을 난수로 섞어 겹치게 보내도 지난 버전의 제안이 새 발화 뒤에 노출되지 않는다."""
    delay = RandomDelay(seed, max_ms=30)
    dec = DelayedDecider(delay, intents=[("규정", "other")])
    gen = DelayedGenerator(delay)
    gaps = RandomDelay(1000 + seed, max_ms=40)
    texts = ["환불 규정이 궁금해요", "아 아니고 해지 규정이요", "그럼 위약금 규정은요?", "체험 규정도 알려 주세요",
             "마지막으로 결제일 규정이요"]

    async def go():
        s = Session(ORG, f"t-rand-{seed}", tmp_path, decision_client=dec, generator=gen, bizapi=NoBizApi())
        tasks = []
        for i, t in enumerate(texts, 1):
            tasks.append(asyncio.create_task(s.handle(utt(f"u{i}", t))))
            await gaps.sleep()
        await asyncio.gather(*tasks)
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / f"t-rand-{seed}.jsonl")
    assert exposure_violations(log) == []
    # 마지막 발화에는 반드시 답한다
    final_v = max(e["to"] for e in types(log, ev.VERSION_BUMPED))
    assert any(e["basis_version"] == final_v for e in types(log, ev.SUGGESTION_CREATED))
    # 진행 중이던 앞 턴은 취소되거나 버려졌다: 답하지 않은 발화마다 취소·폐기 기록이 있다
    answered = {e["basis_version"] for e in types(log, ev.SUGGESTION_CREATED)}
    unanswered = [e["to"] for e in types(log, ev.VERSION_BUMPED) if e["to"] not in answered]
    stopped = {e["started_version"] for e in types(log, ev.TASK_CANCELLED, ev.RESULT_DISCARDED)}
    started = {e["version"] for e in types(log, ev.RETRIEVAL_DONE)}
    assert all(v in stopped or v not in started for v in unanswered)


def test_two_utterances_during_slow_warmup_run_one_pipeline(tmp_path):
    """예열이 끝나기 전에 발화 두 개가 오면 둘 다 예열을 기다린다. 예열 뒤에는 최신 발화만 진행하고 앞 발화는 양보한다.
    (시뮬레이터 작업자가 가짜만으로 재현: 고치기 전에는 두 handle이 같은 버전으로 파이프라인을 하나씩 돌려 같은 기준
    버전의 응답 두 개를 노출했다.) 버전은 예열과 관계없이 발화가 도착할 때 오른다."""
    dec = GatedDeciderForWarmup(intents=[("궁금", "other")])

    async def go():
        s = Session(ORG, "t-warm-two", tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=NoBizApi())
        w = asyncio.create_task(s.start())
        await dec.entered.wait()  # 예열 요청이 백엔드에 걸려 있다
        h1 = asyncio.create_task(s.handle(utt("u1", "환불 규정이 궁금해요")))
        h2 = asyncio.create_task(s.handle(utt("u2", "아니 해지 규정이 궁금해요")))
        for _ in range(20):
            await asyncio.sleep(0)
        assert s.state.version == 2  # 두 발화 모두 도착 즉시 버전을 올렸다
        dec.release.set()
        await w
        o1, o2 = await h1, await h2
        await s.aclose()
        return o1, o2

    o1, o2 = asyncio.run(go())
    log = ev.read_log(tmp_path / "t-warm-two.jsonl")
    assert not types(o1, ev.RETRIEVAL_DONE, ev.DECISION_REQUEST, ev.SUGGESTION_CREATED)  # 앞 발화는 진행하지 않는다
    created = types(log, ev.SUGGESTION_CREATED)
    assert [c["basis_version"] for c in created] == [2] and len(types(log, ev.SUGGESTION_EXPOSED)) == 1
    assert len(dec.calls) == 2  # 예열 1회 + 최신 발화의 판단 1회
    assert exposure_violations(log) == []


class GatedDeciderForWarmup(ScriptedDecider):
    """첫 호출(예열)을 release 전까지 붙잡는다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._first = True

    async def decide(self, state, questions):
        if self._first:
            self._first = False
            self.entered.set()
            await self.release.wait()
        return await super().decide(state, questions)


def test_exposure_invariant_detects_a_stale_exposure():
    """검사 함수 자체가 지난 제안 노출을 잡는지 확인한다."""
    log = [
        {"type": ev.UTTERANCE_END, "t_ms": 0.0, "version": 0, "utt_id": "u1", "speaker": "customer"},
        {"type": ev.UTTERANCE_END, "t_ms": 10.0, "version": 1, "utt_id": "u2", "speaker": "customer"},
        {"type": ev.SUGGESTION_CREATED, "t_ms": 12.0, "suggestion_id": "s1", "basis_version": 1},
        {"type": ev.SUGGESTION_EXPOSED, "t_ms": 12.0, "suggestion_id": "s1"},
    ]
    assert exposure_violations(log) == ["s1(v1) exposed after u2(v1→)"]
    log[2]["basis_version"] = 2
    assert exposure_violations(log) == []
