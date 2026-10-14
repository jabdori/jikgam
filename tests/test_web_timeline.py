"""턴 상세 타임라인(jikgam/web/timeline.py): 턴 배정, 막대 짝짓기, 취소, 근거 갱신 구간."""

import asyncio

from engine_fakes import EchoGenerator, GatedGenerator, NoBizApi, ScriptedDecider, utt
from sim_logs import LogBuilder, mark

from jikgam.engine import events as ev
from jikgam.engine.org import load_org
from jikgam.engine.session import Session
from jikgam.web.timeline import build_timeline, version_owners

ORG = load_org("org_a")


def rows_of(turn, stage):
    return [r for r in turn["rows"] if r["stage"] == stage]


def test_turn_rows_pair_requests_and_responses_on_engine_clock():
    b = LogBuilder(session="x", step=10)
    b._emit(ev.SESSION_START, t=0.0)
    b._emit(ev.WARMUP, t=5.0, backend="jev", latency_ms=4.0, wall_ms=4.5)
    b.utt("해지할게요", t=100.0)                               # 100: utterance.end, 110: version.bumped
    b._emit(ev.RETRIEVAL_DONE, t=111.0, started_version=1, duration_ms=0.5, candidates=["A-07"], sources={})
    b._emit(ev.DECISION_REQUEST, t=112.0, backend="jev", questions=["intent"], state_chars=900)
    b._emit(ev.DECISION_RESPONSE, t=300.0, request_version=1, backend="jev", latency_ms=187.5, attempts=1)
    b._emit(ev.EVIDENCE_UPDATED, t=301.0, articles=["A-07"], sufficient=True, action="answer")
    b._emit(ev.TOOL_CALL, t=302.0, request_version=1, call_id="c1", tool="list_payments", args={"customer_id": "C1"})
    b._emit(ev.TOOL_RESULT, t=320.0, request_version=1, call_id="c1", tool="list_payments", ok=True, latency_ms=18.0,
            attempts=1, data={"payments": []})
    b.bump_tool()                                               # 330: v1 → v2 (같은 턴)
    b._emit(ev.GENERATION_REQUEST, t=331.0, model="gem", rule="decision")
    b._emit(ev.GENERATION_RESPONSE, t=900.0, request_version=2, model="gem", latency_ms=569.0)
    b.suggest("x-s1", t=901.0)
    tl = build_timeline(b.events)
    assert [r["stage"] for r in tl["pre"]] == ["warmup"]
    assert len(tl["turns"]) == 1
    t = tl["turns"][0]
    assert t["utt_id"] == "u1" and t["evidence_ms"] == 201.0
    (ret,) = rows_of(t, "retrieval")
    assert (ret["start"], ret["end"]) == (10.5, 11.0)
    (dec,) = rows_of(t, "decision")
    assert (dec["start"], dec["end"], dec["status"]) == (12.0, 200.0, "ok")
    (tool,) = rows_of(t, "tool")
    assert (tool["start"], tool["end"]) == (202.0, 220.0) and "0건" in tool["detail"]
    (gen,) = rows_of(t, "generation")
    assert (gen["start"], gen["end"]) == (231.0, 800.0)  # v2(도구 결과로 오른 버전)도 같은 턴
    assert [r["label"] for r in rows_of(t, ev.VERSION_BUMPED)] == ["버전 v0→v1", "버전 v1→v2"]
    assert t["rows"] == sorted(t["rows"], key=lambda r: r["start"])
    assert t["span"][0] == 0.0 and t["span"][1] >= 801.0


def test_cancelled_work_stays_in_its_turn_and_next_utterance_line():
    """빠른 정정: 앞 턴의 생성이 다음 발화 뒤에 취소되면 막대는 앞 턴에 남고 취소 시각에서 닫힌다."""
    b = LogBuilder(session="x", step=1)
    b.utt("보상 없어요?", t=0.0)
    b._emit(ev.GENERATION_REQUEST, t=50.0, model="gem", rule="decision")
    b.utt("연장 말고 돈으로요", marker=mark("s21#0"), t=1000.0)  # v1 → v2
    b._emit(ev.TASK_CANCELLED, t=1001.0, stage="generation", started_version=1)
    b._emit(ev.EVIDENCE_UPDATED, t=1200.0)  # v2
    b._emit(ev.SUGGESTION_INVALIDATED, t=1200.5, suggestion_id="x-s0", basis_version=1, invalidated_version=2,
            reasons=["condition_changed=true"])
    tl = build_timeline(b.events)
    t1, t2 = tl["turns"]
    (gen,) = rows_of(t1, "generation")
    assert gen["status"] == "cancelled" and gen["end"] == 1001.0 and "취소" in gen["detail"]
    assert [r["status"] for r in rows_of(t1, ev.TASK_CANCELLED)] == ["cancelled"]
    assert t1["next_offset"] == 1000.0 and t1["span"][1] >= 1001.0
    assert t2["refresh"] == {"latency_ms": 200.0, "invalidates_prior": True, "event": "s21#0"}
    assert rows_of(t2, ev.SUGGESTION_INVALIDATED)  # 무효화는 무효화한 버전의 턴
    assert tl["refresh_events"] == 1 and tl["refresh_misses"] == 0


def test_rag_log_without_version_bumps_and_missing_refresh():
    """정책 A 로그: version.bumped가 없고 답이 다음 발화 뒤에 나올 수 있다(미갱신)."""
    events = [
        {"t_ms": 0.0, "type": ev.SESSION_START, "version": 0, "policy": "A", "session": "r"},
        {"t_ms": 10.0, "type": ev.UTTERANCE_END, "version": 0, "utt_id": "u1", "speaker": "customer", "text": "a"},
        {"t_ms": 11.0, "type": ev.RETRIEVAL_DONE, "version": 1, "started_version": 1, "duration_ms": 0.2, "candidates": []},
        {"t_ms": 12.0, "type": ev.GENERATION_REQUEST, "version": 1, "request_version": 1, "model": "g", "step": 1},
        {"t_ms": 20.0, "type": ev.UTTERANCE_END, "version": 1, "utt_id": "u2", "speaker": "customer", "text": "b",
         "marker": mark("t#0")},
        {"t_ms": 900.0, "type": ev.GENERATION_RESPONSE, "version": 2, "request_version": 1, "model": "g", "step": 1,
         "latency_ms": 888.0},
        {"t_ms": 901.0, "type": ev.EVIDENCE_UPDATED, "version": 1, "articles": []},
        {"t_ms": 950.0, "type": ev.GENERATION_REQUEST, "version": 2, "request_version": 2, "model": "g", "step": 1},
        {"t_ms": 1500.0, "type": ev.GENERATION_RESPONSE, "version": 2, "request_version": 2, "model": "g", "step": 1,
         "latency_ms": 550.0},
        {"t_ms": 1501.0, "type": ev.EVIDENCE_UPDATED, "version": 2, "articles": []},
    ]
    assert version_owners(events) == {1: "u1", 2: "u2"}
    tl = build_timeline(events)
    u1, u2 = tl["turns"]
    (g1,) = rows_of(u1, "generation")
    assert (g1["start"], g1["end"]) == (2.0, 890.0)  # 다음 발화(10 ms 뒤) 이후에 끝났지만 u1 턴에 남는다
    assert u1["evidence_ms"] is None  # 다음 고객 발화 전에 근거 갱신이 없다
    assert u2["refresh"]["latency_ms"] == 1481.0
    assert rows_of(u2, "generation")[0]["start"] == 930.0


def test_timeline_of_real_engine_session_with_cancellation(tmp_path):
    gen = GatedGenerator()

    async def go():
        s = Session(ORG, "t-tl", tmp_path, decision_client=ScriptedDecider(intents=[("보상", "other")]),
                    generator=gen, bizapi=NoBizApi())
        await s.start()
        t1 = asyncio.create_task(s.handle(utt("u1", "장애 보상 같은 거 없어요?")))
        await gen.entered.wait()
        t2 = asyncio.create_task(s.handle(utt("u2", "연장 말고 돈으로 돌려받을 수 있어요?",
                                               marker={"condition_change": True, "invalidates_prior": True,
                                                       "event": "s21#0"})))
        while s.version < 2:
            await asyncio.sleep(0)
        gen.release.set()
        await t1, await t2
        await s.aclose()

    asyncio.run(go())
    tl = build_timeline(ev.read_log(tmp_path / "t-tl.jsonl"))
    u1, u2 = tl["turns"]
    assert [r["status"] for r in rows_of(u1, "generation")] == ["cancelled"]
    assert rows_of(u2, "generation")[0]["status"] == "ok"
    assert u2["refresh"]["latency_ms"] is not None and u2["refresh"]["latency_ms"] >= 0
    assert [r["stage"] for r in tl["pre"]] == ["warmup"]
    assert tl["channels"] == ["chat"]
    # EchoGenerator만 쓰는 두 번째 세션: 모든 막대가 닫힌다
    async def go2():
        s = Session(ORG, "t-tl2", tmp_path, decision_client=ScriptedDecider(), generator=EchoGenerator(),
                    bizapi=NoBizApi())
        await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        await s.aclose()

    asyncio.run(go2())
    t = build_timeline(ev.read_log(tmp_path / "t-tl2.jsonl"))["turns"][0]
    assert all(r["end"] is not None for r in t["rows"] if r["kind"] == "bar")
    assert {"retrieval", "decision", "generation"} <= {r["stage"] for r in t["rows"]}
