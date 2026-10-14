"""음성 연결(EngineBridge): 시각 대응, 미확정·확정 자막 처리, 음성 노출 기록, 끼어들기, 고객 차례 동안 응답 보류.

실제 Pipecat 파이프라인 [EngineBridge → FakeSpeaker]에 프레임을 넣고, 엔진은 실제 Session에 가짜 판별기·생성기를 붙여
쓴다. FakeSpeaker는 TTS와 출력 전송을 흉내 낸다: TTSSpeakFrame을 받으면 start_delay 뒤 BotStartedSpeakingFrame,
duration 뒤 BotStoppedSpeakingFrame을 위로 올리고, InterruptionFrame을 받으면 재생을 멈춘다.
"""

import asyncio
import time

import pytest
from engine_fakes import EchoGenerator, GatedGenerator, NoBizApi, ScriptedDecider
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterimTranscriptionFrame,
                                   InterruptionFrame, TranscriptionFrame, TTSSpeakFrame, VADUserStartedSpeakingFrame,
                                   VADUserStoppedSpeakingFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from jikgam.engine import events as ev
from jikgam.engine.org import load_org
from jikgam.engine.session import Session
from jikgam.sim import metrics as sm
from jikgam.voice import bridge as vb
from jikgam.voice.bridge import EngineBridge, wall_to_engine_ms

ORG = load_org("org_a")


class FakeSpeaker(FrameProcessor):
    def __init__(self, start_delay: float = 0.05, duration: float = 0.3):
        super().__init__(enable_direct_mode=True)
        self.start_delay = start_delay
        self.duration = duration
        self.spoken: list[str] = []
        self.interruptions = 0
        self._task: asyncio.Task | None = None
        self._speaking = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            self.spoken.append(frame.text)
            self._task = asyncio.create_task(self._speak())
            return
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self.interruptions += 1
            if self._task is not None and not self._task.done():
                self._task.cancel()
                if self._speaking:
                    self._speaking = False
                    await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)

    async def _speak(self):
        await asyncio.sleep(self.start_delay)
        self._speaking = True
        await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(self.duration)
        self._speaking = False
        await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)


def make_session(tmp_path, sid: str, generator=None) -> Session:
    return Session(ORG, sid, tmp_path, decision_client=ScriptedDecider(intents=[("", "other")]),
                   generator=generator or EchoGenerator(), bizapi=NoBizApi(), channel=vb.CHANNEL)


def run(session: Session, bridge: EngineBridge, speaker: FakeSpeaker, script) -> None:
    """파이프라인을 돌리고 같은 이벤트 루프에서 세션을 닫는다."""
    async def go():
        try:
            await run_pipeline(bridge, speaker, script)
        finally:
            await session.aclose()

    asyncio.run(go())


async def run_pipeline(bridge: EngineBridge, speaker: FakeSpeaker, script) -> None:
    worker = PipelineWorker(Pipeline([bridge, speaker]), params=PipelineParams(), idle_timeout_secs=None,
                            cancel_on_idle_timeout=False, enable_rtvi=False)
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def _started(_w, _f):
        started.set()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run = asyncio.create_task(runner.run())
    await asyncio.wait_for(started.wait(), 10)
    try:
        await script(worker)
    finally:
        await worker.end()
        await asyncio.wait_for(run, 10)


def vad_start(ago_s: float = 0.0) -> VADUserStartedSpeakingFrame:
    return VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time() - ago_s)


def vad_stop(ago_s: float = 0.0) -> VADUserStoppedSpeakingFrame:
    return VADUserStoppedSpeakingFrame(stop_secs=0.5, timestamp=time.time() - ago_s)


def partial(text: str) -> InterimTranscriptionFrame:
    return InterimTranscriptionFrame(text, "customer", "")


def final(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text, "customer", "", finalized=True)


def types(log, *wanted):
    return [e for e in log if e["type"] in wanted]


def test_wall_to_engine_ms():
    assert wall_to_engine_ms(100.0, now_wall_s=100.5, now_engine_ms=2000.0) == 1500.0
    assert wall_to_engine_ms(101.0, now_wall_s=100.5, now_engine_ms=2000.0) == 2500.0


def test_partials_do_not_bump_version_and_final_carries_vad_speech_end(tmp_path):
    s = make_session(tmp_path, "v-final")
    b = EngineBridge(s)
    sent: dict[str, float] = {}

    async def script(w):
        await w.queue_frame(vad_start(ago_s=4.0))  # 4초 전에 말을 시작했다
        await w.queue_frame(partial("아 그냥"))
        await w.queue_frame(partial("아 그냥 해지"))
        sent["stop"] = ev.now_ms()
        await w.queue_frame(vad_stop(ago_s=2.0))  # VAD가 2초 전에 끝을 판정했다
        await w.queue_frame(final("아 그냥 해지할게요."))
        assert await b.wait_until(lambda: any(p.done for p in b.playbacks), 10)

    run(s, b, FakeSpeaker(), script)
    log = ev.read_log(tmp_path / "v-final.jsonl")
    parts = types(log, ev.STT_PARTIAL)
    assert [(p["utt_id"], p["version"]) for p in parts] == [("u1", 0), ("u1", 0)]
    bumps = types(log, ev.VERSION_BUMPED)
    assert [x["utt_id"] for x in bumps if x["cause"] == "utterance"] == ["u1"]
    end = types(log, ev.UTTERANCE_END)[0]
    fin = types(log, ev.STT_FINAL)[0]
    expected = sent["stop"] - 2000 - 500  # 끝 판정 시각(엔진 시계) − stop_secs
    assert abs(end["t_ms"] - expected) < 100
    assert fin["t_end_source"] == "vad" and fin["t_end_ms"] == end["t_ms"]
    assert fin["t_ms"] > end["t_ms"] + 2000  # 확정 자막은 발화 종료보다 늦게 받는다
    assert end["final"] is True and end["text"] == "아 그냥 해지할게요."


def test_voice_exposure_logged_at_playback_start(tmp_path):
    s = make_session(tmp_path, "v-expose")
    b = EngineBridge(s)
    sp = FakeSpeaker(start_delay=0.2, duration=0.2)

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("환불 규정이 궁금해요."))
        assert await b.wait_until(lambda: any(p.done for p in b.playbacks), 10)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-expose.jsonl")
    say = types(log, ev.AGENT_SAY)[0]
    voice = [x for x in types(log, ev.SUGGESTION_EXPOSED) if x["channel"] == "voice"]
    assert len(voice) == 1 and voice[0]["suggestion_id"] == say["suggestion_id"]
    assert voice[0]["start_ms"] - say["t_ms"] >= 150  # TTS 요청 뒤 재생이 시작된 시각이다(start_delay 200ms)
    assert voice[0]["basis_version"] == say["version"]
    end = types(log, vb.PLAYBACK_END)[0]
    assert end["exposed"] and end["interrupted"] is None and end["end_ms"] > voice[0]["start_ms"]
    assert sp.spoken == [say["text"]]
    # 평가 지표 코드가 음성 채널 노출로 센다. 엔진은 음성 세션에서 노출 줄을 남기지 않는다(channel=voice)
    assert [x["channel"] for x in sm.exposures(log)] == ["voice"]
    assert types(log, ev.SESSION_START)[0]["channel"] == "voice"


def test_bridge_rejects_a_chat_channel_session(tmp_path):
    """채팅 채널 세션을 음성에 붙이면 엔진(chat)과 음성 계층(voice)이 같은 응답의 노출을 두 번 남긴다."""
    s = Session(ORG, "v-chat", tmp_path, decision_client=ScriptedDecider(), generator=EchoGenerator(), bizapi=NoBizApi())
    with pytest.raises(ValueError):
        EngineBridge(s)
    asyncio.run(s.aclose())


def test_barge_in_by_vad_stops_playback(tmp_path):
    s = make_session(tmp_path, "v-barge")
    b = EngineBridge(s)
    sp = FakeSpeaker(start_delay=0.05, duration=5.0)

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("환불 규정이 궁금해요."))
        assert await b.wait_until(lambda: any(p.start_ms is not None for p in b.playbacks), 10)
        await asyncio.sleep(0.3)
        await w.queue_frame(vad_start())  # 재생 중에 고객이 말을 시작한다
        assert await b.wait_until(lambda: b.playbacks[0].done, 5)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-barge.jsonl")
    barge = types(log, vb.BARGE_IN)
    assert len(barge) == 1 and barge[0]["trigger"] == "vad" and barge[0]["started"] is True
    end = types(log, vb.PLAYBACK_END)[0]
    assert end["interrupted"] == "barge_in" and end["played_ms"] < 2000
    assert sp.interruptions >= 1


def test_barge_in_by_partial_transcript(tmp_path):
    s = make_session(tmp_path, "v-barge-t")
    b = EngineBridge(s)
    sp = FakeSpeaker(start_delay=0.05, duration=5.0)

    async def script(w):
        await w.queue_frame(final("환불 규정이 궁금해요."))
        assert await b.wait_until(lambda: any(p.start_ms is not None for p in b.playbacks), 10)
        await w.queue_frame(partial("네"))  # VAD보다 자막이 먼저 왔다
        assert await b.wait_until(lambda: b.playbacks[0].done, 5)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-barge-t.jsonl")
    assert [x["trigger"] for x in types(log, vb.BARGE_IN)] == ["transcript"]
    assert types(log, vb.PLAYBACK_END)[0]["interrupted"] == "barge_in"


def test_reply_held_during_customer_turn_then_discarded_by_new_final(tmp_path):
    gen = GatedGenerator()
    s = make_session(tmp_path, "v-hold", generator=gen)
    b = EngineBridge(s)
    sp = FakeSpeaker()

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("다음 결제일이 궁금해요."))
        await asyncio.wait_for(gen.entered.wait(), 5)  # u1 응답을 만드는 중
        await w.queue_frame(vad_start())  # 고객이 다시 말한다
        gen.release.set()
        assert await b.wait_until(lambda: not b._turns, 5)  # u1 응답이 나왔다(붙잡힌다)
        await asyncio.sleep(0.2)
        assert b.playbacks == []
        await w.queue_frame(vad_stop())
        await asyncio.sleep(0.2)
        assert b.playbacks == []  # 말이 끝났지만 확정 자막을 기다린다
        await w.queue_frame(final("아 그냥 해지할게요."))
        assert await b.wait_until(lambda: any(p.done for p in b.playbacks), 10)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-hold.jsonl")
    says = types(log, ev.AGENT_SAY)
    assert len(says) == 2
    held = types(log, vb.REPLY_HELD)
    assert [h["suggestion_id"] for h in held] == [says[0]["suggestion_id"]]
    disc = [d for d in types(log, ev.RESULT_DISCARDED) if d["stage"] == "voice"]
    assert [(d["suggestion_id"], d["reason"]) for d in disc] == [(says[0]["suggestion_id"], "version")]
    voice = [x["suggestion_id"] for x in types(log, ev.SUGGESTION_EXPOSED) if x["channel"] == "voice"]
    assert voice == [says[1]["suggestion_id"]]
    assert sp.spoken == [says[1]["text"]]


def test_reply_released_when_final_never_comes(tmp_path):
    s = make_session(tmp_path, "v-noise")
    b = EngineBridge(s, final_wait_ms=300)
    sp = FakeSpeaker()

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("환불 규정이 궁금해요."))
        await w.queue_frame(vad_start())  # 잡음: VAD는 말소리로 봤지만 자막이 나오지 않는다
        await w.queue_frame(vad_stop())
        assert await b.wait_until(lambda: any(p.done for p in b.playbacks), 10)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-noise.jsonl")
    held = types(log, vb.REPLY_HELD)
    exposed = [x for x in types(log, ev.SUGGESTION_EXPOSED) if x["channel"] == "voice"]
    assert len(exposed) == 1
    if held:  # 응답이 잡음보다 늦게 나왔으면 final_wait_ms 동안만 붙잡는다
        assert exposed[0]["t_ms"] - held[0]["t_ms"] < 1000


def test_version_change_before_audio_interrupts_playback(tmp_path):
    s = make_session(tmp_path, "v-stale")
    b = EngineBridge(s)
    sp = FakeSpeaker(start_delay=0.5, duration=3.0)

    async def script(w):
        await w.queue_frame(final("환불 규정이 궁금해요."))
        assert await b.wait_until(lambda: bool(b.playbacks), 10)  # TTS 요청, 소리는 아직
        await w.queue_frame(final("아 그냥 해지할게요."))  # VAD 없이 확정 자막만 온다(버전이 오른다)
        assert await b.wait_until(lambda: len(b.playbacks) == 2 and b.playbacks[1].done, 10)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-stale.jsonl")
    ends = types(log, vb.PLAYBACK_END)
    assert ends[0]["interrupted"] == "version" and ends[0]["exposed"] is True
    assert ends[1]["interrupted"] is None


def test_split_final_takes_vad_stop_before_restart(tmp_path):
    s = make_session(tmp_path, "v-split")
    b = EngineBridge(s)
    t: dict[str, float] = {}

    async def script(w):
        await w.queue_frame(vad_start(ago_s=3.0))
        await w.queue_frame(partial("목요일에 서비스"))
        t["stop1"] = ev.now_ms()
        await w.queue_frame(vad_stop(ago_s=0.1))
        await w.queue_frame(vad_start(ago_s=2.0))  # 문장 사이 쉼 뒤 다음 문장
        await w.queue_frame(final("목요일에 서비스 몇 시간 안 됐잖아요."))  # 앞 문장 확정(VAD는 말하는 중)
        await w.queue_frame(final("그거 보상 같은 거 없어요?"))  # 끝 판정 없이 확정
        await w.queue_frame(partial("연장"))
        t["stop2"] = ev.now_ms()
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("연장 말고요."))
        assert await b.wait_until(lambda: not b._turns, 10)

    run(s, b, FakeSpeaker(), script)
    fins = types(ev.read_log(tmp_path / "v-split.jsonl"), ev.STT_FINAL)
    assert [f["t_end_source"] for f in fins] == ["vad", "final_received", "vad"]
    assert abs(fins[0]["t_end_ms"] - (t["stop1"] - 100 - 500)) < 100
    assert abs(fins[2]["t_end_ms"] - (t["stop2"] - 500)) < 100
    assert [f["utt_id"] for f in fins] == ["u1", "u2", "u3"]


def test_marker_for_is_attached_to_final_utterance(tmp_path):
    s = make_session(tmp_path, "v-marker")
    calls = []

    def marker_for(start, end):
        calls.append((start, end))
        return {"condition_change": True, "invalidates_prior": True, "event": "s20#0"}

    b = EngineBridge(s, marker_for=marker_for)

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(final("아 그냥 해지할게요."))
        assert await b.wait_until(lambda: not b._turns, 10)

    run(s, b, FakeSpeaker(), script)
    log = ev.read_log(tmp_path / "v-marker.jsonl")
    end = types(log, ev.UTTERANCE_END)[0]
    assert end["marker"]["event"] == "s20#0"
    assert len(calls) == 1 and calls[0][1] == end["t_ms"]
    assert [m.event for m in sm.marked_utterances(log)] == ["s20#0"]
