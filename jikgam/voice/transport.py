"""로컬 테스트용 Pipecat 전송: 녹음 파일을 실시간으로 흘려 넣고, 엔진 음성을 실시간으로 "재생"한다.

전화·WebRTC 클라이언트 없이 실제 STT를 통과하는 자동 테스트를 하려는 것이다(ADR 0008, docs/design.md §8).

- 입력(PacedAudioInput): 20ms 조각을 엔진 단조 시계(jikgam.engine.events.now_ms) 기준 기한에 맞춰 넣는다. 재생할 오디오가
  없으면 무음을 넣는다(마이크처럼 끊기지 않는 입력. VAD 끝점과 STT 연결 유지에 필요하다). `play(pcm)`은 그 오디오를 다음
  조각 경계부터 넣고, 첫 조각을 넣은 시각(엔진 시계)을 돌려준다. 파일 안 위치 x ms의 소리는 그 시각 + x에 파이프라인에
  들어간 것이다. 이 값이 테스트의 "오디오상 발화 시작·종료" 기준값이다.
- 출력(PacedAudioOutput): 조각마다 그 길이만큼 기다려 실시간 재생을 흉내 낸다(실제 장치의 쓰기가 막히는 것과 같은 효과).
  그래서 Pipecat 끼어들기(InterruptionFrame)가 오면 재생이 그 자리에서 끊긴다.
- 기한은 asyncio.sleep으로 기다린다. Python 3.12의 Windows asyncio 시계는 해상도가 약 15.6ms라 조각마다 그만큼 늦을 수
  있다. 기한을 누적해 계산하므로 평균 속도는 실시간이고, 늦은 조각은 다음 조각에서 따라잡는다.
- 두 방향의 오디오를 엔진 시계와 함께 Recorder에 남겨, 끝나면 스테레오 WAV(왼쪽 고객, 오른쪽 엔진)로 저장할 수 있다.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from jikgam.engine.events import now_ms

from .audio import resample, write_wav

CHUNK_MS = 20


async def sleep_until(t_ms: float) -> None:
    """엔진 시계로 t_ms까지 잔다. asyncio 시계가 거칠어 일찍 깨면 다시 잔다."""
    while (remain := t_ms - now_ms()) > 0:
        await asyncio.sleep(remain / 1000)


@dataclass
class Recorder:
    """두 방향 오디오를 엔진 시계와 함께 모은다."""

    customer: list[tuple[float, bytes, int]] = field(default_factory=list)
    agent: list[tuple[float, bytes, int]] = field(default_factory=list)

    def save(self, path, t0_ms: float, rate: int = 16000) -> None:
        """t0_ms부터의 스테레오 WAV(왼쪽 고객, 오른쪽 엔진)."""
        chans = []
        for parts in (self.customer, self.agent):
            buf = np.zeros(0, dtype=np.int32)
            for t, pcm, r in parts:
                x = np.frombuffer(resample(pcm, r, rate), dtype=np.int16).astype(np.int32)
                at = max(0, int((t - t0_ms) * rate / 1000))
                if len(buf) < at + len(x):
                    buf = np.concatenate([buf, np.zeros(at + len(x) - len(buf), dtype=np.int32)])
                buf[at:at + len(x)] += x
            chans.append(buf)
        n = max(len(c) for c in chans)
        chans = [np.concatenate([c, np.zeros(n - len(c), dtype=np.int32)]) for c in chans]
        stereo = np.clip(np.stack(chans, axis=1), -32768, 32767).astype(np.int16)
        write_wav(path, stereo.tobytes(), rate, channels=2)


@dataclass
class _Segment:
    pcm: bytes
    started: asyncio.Future  # 첫 조각을 넣은 엔진 시각
    finished: asyncio.Future  # 마지막 조각을 넣은 엔진 시각


class PacedAudioInput(BaseInputTransport):
    def __init__(self, params: TransportParams, recorder: Recorder | None = None, **kwargs):
        super().__init__(params, **kwargs)
        self._segments: deque[_Segment] = deque()
        self._feed_task: asyncio.Task | None = None
        self._recorder = recorder

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        if self._feed_task is None:
            self._feed_task = self.create_task(self._feed(), name="paced-input")

    async def _stop_feed(self):
        if self._feed_task is not None:
            await self.cancel_task(self._feed_task)
            self._feed_task = None

    async def stop(self, frame):
        await self._stop_feed()
        await super().stop(frame)

    async def cancel(self, frame):
        await self._stop_feed()
        await super().cancel(frame)

    def enqueue(self, pcm: bytes) -> "_Segment":
        """pcm(입력 샘플레이트, 모노 16비트)을 재생 대기열에 넣는다. started·finished는 첫·마지막 조각을 넣은 엔진 시각."""
        loop = asyncio.get_running_loop()
        seg = _Segment(pcm, loop.create_future(), loop.create_future())
        self._segments.append(seg)
        return seg

    async def play(self, pcm: bytes) -> tuple[float, float]:
        """pcm을 넣고 (첫 조각 시각, 마지막 조각 시각)을 돌려준다(엔진 시계 ms)."""
        seg = self.enqueue(pcm)
        return await seg.started, await seg.finished

    async def _feed(self):
        size = self.sample_rate * 2 * CHUNK_MS // 1000
        silence = bytes(size)
        deadline = now_ms()
        seg: _Segment | None = None
        pos = 0
        while True:
            if seg is None and self._segments:
                seg, pos = self._segments.popleft(), 0
            if seg is not None:
                chunk = seg.pcm[pos:pos + size]
                chunk += bytes(size - len(chunk))
            else:
                chunk = silence
            t = now_ms()
            await self.push_audio_frame(InputAudioRawFrame(audio=chunk, sample_rate=self.sample_rate, num_channels=1))
            if seg is not None:
                if pos == 0:
                    seg.started.set_result(t)
                if self._recorder is not None:
                    self._recorder.customer.append((t, chunk, self.sample_rate))
                pos += size
                if pos >= len(seg.pcm):
                    seg.finished.set_result(t)
                    seg = None
            deadline += CHUNK_MS
            await sleep_until(deadline)


class PacedAudioOutput(BaseOutputTransport):
    def __init__(self, params: TransportParams, recorder: Recorder | None = None, **kwargs):
        super().__init__(params, **kwargs)
        self._recorder = recorder
        self._clock: float | None = None  # 지금 재생 중인 조각이 끝나는 엔진 시각

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        t = now_ms()
        if self._clock is None or self._clock < t:  # 쉬다가 다시 재생하면 지금부터 센다
            self._clock = t
        start = self._clock
        self._clock += len(frame.audio) / 2 / frame.sample_rate * 1000
        if self._recorder is not None:
            self._recorder.agent.append((start, frame.audio, frame.sample_rate))
        await sleep_until(self._clock)
        return True


class PacedAudioTransport(BaseTransport):
    def __init__(self, params: TransportParams, recorder: Recorder | None = None):
        super().__init__()
        self._params = params
        self.recorder = recorder
        self._input: PacedAudioInput | None = None
        self._output: PacedAudioOutput | None = None

    def input(self) -> PacedAudioInput:
        if self._input is None:
            self._input = PacedAudioInput(self._params, self.recorder, name="paced-input")
        return self._input

    def output(self) -> FrameProcessor:
        if self._output is None:
            self._output = PacedAudioOutput(self._params, self.recorder, name="paced-output")
        return self._output
