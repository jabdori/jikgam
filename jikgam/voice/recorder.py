"""관리 콘솔 음성 시험의 스테레오 녹음: 왼쪽 고객 마이크, 오른쪽 AI 음성. 엔진 시계에 맞춰 둔다.

STT는 입력 음성을 다음 단계로 넘기지 않는다(audio_passthrough=False). 그래서 출력 뒤 한 곳에서는 고객 음성을 받을 수 없다.
입력 전송 바로 뒤(고객)와 출력 전송 바로 뒤(AI, 보낸 뒤에 내려온다)에 탭을 하나씩 둔다. 탭은 프레임을 바꾸지 않고 먼저 넘긴
뒤에 사본을 적는다. 프레임이 들어온 엔진 시각을 녹음 위치로 삼고, 앞 소리와 GAP_MS보다 크게 벌어지면 무음으로 채운다.
작은 흔들림은 채우지 않고 이어 붙인다(말 중간에 무음이 끼지 않게).
"""

from collections.abc import Callable

from pipecat.audio.utils import create_stream_resampler, interleave_stereo_audio
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jikgam.engine import events as ev

GAP_MS = 120.0
SAMPLE_BYTES = 2  # 16비트 PCM


class StereoRecorder:
    def __init__(self, rate: int, clock: Callable[[], float] = ev.now_ms):
        self.rate = rate
        self.clock = clock
        self.t0_ms: float | None = None
        self.left = bytearray()
        self.right = bytearray()
        self._resample = {"left": create_stream_resampler(), "right": create_stream_resampler()}

    def start(self) -> float:
        self.t0_ms = self.clock()
        return self.t0_ms

    def stop(self) -> bytes | None:
        """두 채널을 같은 길이로 맞춰 L, R 순서로 섞은 16비트 PCM. 시작하지 않았으면 None."""
        if self.t0_ms is None:
            return None
        self.t0_ms = None
        n = max(len(self.left), len(self.right))
        left = bytes(self.left) + b"\x00" * (n - len(self.left))
        right = bytes(self.right) + b"\x00" * (n - len(self.right))
        return interleave_stereo_audio(left, right)

    async def add(self, side: str, audio: bytes, sample_rate: int) -> None:
        if self.t0_ms is None:
            return
        pcm = await self._resample[side].resample(audio, sample_rate, self.rate)
        buf = self.left if side == "left" else self.right
        start_ms = self.clock() - self.t0_ms - len(pcm) / SAMPLE_BYTES / self.rate * 1000  # 이 소리가 시작된 시각
        gap = int(start_ms * self.rate / 1000) * SAMPLE_BYTES - len(buf)
        if gap > GAP_MS * self.rate / 1000 * SAMPLE_BYTES:
            buf.extend(b"\x00" * gap)
        buf.extend(pcm)

    def tap(self, side: str) -> FrameProcessor:
        return _Tap(self, side)


class _Tap(FrameProcessor):
    def __init__(self, recorder: StereoRecorder, side: str):
        super().__init__(name=f"recorder-{side}")
        self.recorder = recorder
        self.side = side
        self.kind = InputAudioRawFrame if side == "left" else OutputAudioRawFrame

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, self.kind):
            await self.recorder.add(self.side, frame.audio, frame.sample_rate)
