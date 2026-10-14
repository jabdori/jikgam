"""16비트 PCM 오디오 도구: WAV 읽기·쓰기, 리샘플, 음성 구간과 쉼 찾기.

음성 구간은 10ms 프레임 RMS가 문턱(기본 -45 dBFS)을 넘는 첫 프레임과 마지막 프레임으로 정한다. 합성 음성처럼 잡음이 없는
오디오에서 "오디오상 발화 시작·종료"(docs/evaluation.md §3.1)의 기준값으로 쓴다. 잡음이 있는 실제 통화 음성에는 맞지 않는다.
"""

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FRAME_MS = 10
SILENCE_DBFS = -45.0


def read_wav(path: Path) -> tuple[bytes, int]:
    """모노 16비트 WAV의 (PCM, 샘플레이트)."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"모노 16비트 WAV만 읽는다: {path} ({w.getnchannels()}ch, {w.getsampwidth() * 8}bit)")
        return w.readframes(w.getnframes()), w.getframerate()


def write_wav(path: Path, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate:
        return pcm
    import soxr  # Pipecat 의존성으로 함께 설치된다

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    y = soxr.resample(x, from_rate, to_rate)
    return (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def duration_ms(pcm: bytes, sample_rate: int) -> float:
    return len(pcm) / 2 / sample_rate * 1000.0


def _voiced(pcm: bytes, sample_rate: int, silence_dbfs: float) -> np.ndarray:
    """10ms 프레임마다 문턱을 넘는가."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    n = sample_rate * FRAME_MS // 1000
    frames = len(x) // n
    if frames == 0:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt(np.mean(x[:frames * n].reshape(frames, n) ** 2, axis=1))
    return rms > 32768.0 * 10 ** (silence_dbfs / 20)


@dataclass(frozen=True)
class SpeechBounds:
    start_ms: float  # 첫 음성 프레임 시작
    end_ms: float  # 마지막 음성 프레임 끝
    pauses_ms: tuple[float, ...]  # 음성 사이 쉼의 길이(10ms 단위)

    @property
    def max_pause_ms(self) -> float:
        return max(self.pauses_ms, default=0.0)


def speech_bounds(pcm: bytes, sample_rate: int, silence_dbfs: float = SILENCE_DBFS) -> SpeechBounds | None:
    """음성 구간과 그 안의 쉼. 음성이 없으면 None."""
    v = _voiced(pcm, sample_rate, silence_dbfs)
    idx = np.flatnonzero(v)
    if idx.size == 0:
        return None
    first, last = int(idx[0]), int(idx[-1])
    pauses = []
    run = 0
    for on in v[first:last + 1]:
        if on:
            if run:
                pauses.append(run * FRAME_MS)
            run = 0
        else:
            run += 1
    return SpeechBounds(first * FRAME_MS, (last + 1) * FRAME_MS, tuple(float(p) for p in pauses))


def trim_and_pad(pcm: bytes, sample_rate: int, lead_ms: int, tail_ms: int,
                 silence_dbfs: float = SILENCE_DBFS) -> bytes:
    """앞뒤 무음을 잘라 내고 정해진 길이의 무음을 붙인다. 음성 시작이 정확히 lead_ms가 되게 하려는 것이다."""
    b = speech_bounds(pcm, sample_rate, silence_dbfs)
    if b is None:
        raise ValueError("음성이 없는 오디오")
    per_ms = sample_rate * 2 // 1000
    body = pcm[int(b.start_ms) * per_ms:int(b.end_ms) * per_ms]
    return bytes(lead_ms * per_ms) + body + bytes(tail_ms * per_ms)
