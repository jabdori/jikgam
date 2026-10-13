"""음성 도구: 음성 구간·쉼 찾기, 앞뒤 무음 맞추기, 리샘플, 시험 입력 전송의 시각."""

import asyncio

import numpy as np
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

from jikgam.voice.audio import duration_ms, resample, speech_bounds, trim_and_pad
from jikgam.voice.transport import CHUNK_MS, PacedAudioInput

RATE = 16000


def tone(ms: int, rate: int = RATE, amp: float = 0.3) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return (np.sin(2 * np.pi * 220 * t) * amp * 32767).astype(np.int16).tobytes()


def silence(ms: int, rate: int = RATE) -> bytes:
    return bytes(rate * ms // 1000 * 2)


def test_speech_bounds_and_pauses():
    pcm = silence(130) + tone(500) + silence(300) + tone(400) + silence(250)
    b = speech_bounds(pcm, RATE)
    assert b.start_ms == 130 and b.end_ms == 130 + 500 + 300 + 400
    assert b.pauses_ms == (300.0,) and b.max_pause_ms == 300.0
    assert speech_bounds(silence(500), RATE) is None


def test_trim_and_pad_puts_speech_at_lead():
    pcm = silence(370) + tone(600) + silence(90)
    out = trim_and_pad(pcm, RATE, lead_ms=200, tail_ms=300)
    b = speech_bounds(out, RATE)
    assert (b.start_ms, b.end_ms) == (200, 800)
    assert duration_ms(out, RATE) == 1100


def test_resample_keeps_duration():
    pcm = tone(1000, rate=24000)
    out = resample(pcm, 24000, 16000)
    assert abs(duration_ms(out, 16000) - 1000) < 2
    assert resample(pcm, 24000, 24000) is pcm


class Collector(FrameProcessor):
    def __init__(self):
        super().__init__(enable_direct_mode=True)
        self.audio: list[bytes] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio.append(frame.audio)
        await self.push_frame(frame, direction)


def test_paced_input_feeds_audio_in_real_time_and_reports_engine_times():
    inp = PacedAudioInput(TransportParams(audio_in_enabled=True, audio_in_sample_rate=RATE))
    sink = Collector()
    pcm = tone(200)

    async def go():
        worker = PipelineWorker(Pipeline([inp, sink]), params=PipelineParams(audio_in_sample_rate=RATE),
                                idle_timeout_secs=None, cancel_on_idle_timeout=False, enable_rtvi=False)
        started = asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def _s(_w, _f):
            started.set()

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        run = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), 10)
        result = await inp.play(pcm)
        await worker.end()
        await asyncio.wait_for(run, 10)
        return result

    t_start, t_last = asyncio.run(go())
    chunks = len(pcm) // (RATE * 2 * CHUNK_MS // 1000)
    # 200ms = 20ms 조각 10개. 첫 조각과 마지막 조각 사이는 9조각(180ms). Windows asyncio 시계(약 15.6ms) 여유를 둔다
    assert 180 - 40 <= t_last - t_start <= 180 + 120
    joined = b"".join(sink.audio)
    assert pcm in joined  # 조각 경계에서 시작하므로 그대로 들어 있다
    assert len(sink.audio) >= chunks
