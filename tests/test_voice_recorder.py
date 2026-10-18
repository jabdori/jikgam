"""관리 콘솔 음성 시험의 스테레오 녹음(jikgam/voice/recorder.py): 두 채널을 엔진 시계에 맞춘다."""

import asyncio
import struct

from jikgam.voice.recorder import StereoRecorder

RATE = 16000


def tone(ms: float, value: int) -> bytes:
    return struct.pack("<h", value) * int(RATE * ms / 1000)


def channel(stereo: bytes, side: int) -> list[int]:
    samples = struct.unpack(f"<{len(stereo) // 2}h", stereo)
    return list(samples[side::2])


def test_ai_speech_lands_where_it_was_played_and_customer_audio_stays_continuous():
    now = [0.0]
    rec = StereoRecorder(RATE, clock=lambda: now[0])
    rec.start()

    async def run():
        for i in range(100):  # 고객 마이크: 20ms씩 2초를 끊김 없이
            now[0] = (i + 1) * 20.0
            await rec.add("left", tone(20, 100), RATE)
            if 50 <= i < 75:  # AI 음성: 1초부터 0.5초 동안(보낸 뒤에 내려온다)
                await rec.add("right", tone(20, -200), RATE)
        now[0] += 3  # 같은 시각대의 작은 흔들림은 무음으로 채우지 않는다
    asyncio.run(run())
    stereo = rec.stop()
    left, right = channel(stereo, 0), channel(stereo, 1)
    assert len(left) == len(right) == RATE * 2
    assert set(left) == {100}
    first = right.index(-200)
    assert abs(first - RATE) <= RATE * 20 // 1000  # 1초 자리(한 프레임 안)
    assert right[:first] == [0] * first and right.count(-200) == RATE // 2


def test_nothing_is_recorded_before_start_or_after_stop():
    rec = StereoRecorder(RATE, clock=lambda: 0.0)
    asyncio.run(rec.add("left", tone(20, 1), RATE))
    assert rec.stop() is None and not rec.left
