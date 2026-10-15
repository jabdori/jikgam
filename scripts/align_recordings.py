"""voice.recording이 없는 이전 음성 실행(20261014-voice-r1, -r2)의 녹음 시작 시각을 추정해 recordings.json으로 남긴다.

    uv run python scripts/align_recordings.py results/voice/20261014-voice-r2

새 실행은 세션 로그의 voice.recording에 녹음 시작 시각(t0_ms, 엔진 시계)을 남긴다(jikgam/voice/replay.py save_recording).
이전 실행은 그 값이 없어 로그 시각과 녹음 위치를 맞출 수 없다. 녹음의 왼쪽(고객) 채널은 고객 녹음을 넣은 시각에만 소리가
있으므로, 첫 고객 음성이 시작되는 녹음 위치(10ms 프레임 RMS가 -45 dBFS를 넘는 첫 프레임, data/voice/manifest.json과 같은
기준)와 로그의 첫 voice.segment의 기준 발화 시작(speech_start_ms)을 맞춘다.

    t0_ms = speech_start_ms(첫 구간) - 녹음 안 첫 고객 음성 위치(ms)

같은 방식으로 녹음의 모든 구간 시작을 다시 계산해 로그 값과의 차(최대 오차)를 함께 적는다. 녹음 파일은 저장소에 넣지 않는다.
"""

import hashlib
import json
import sys
import wave
from pathlib import Path

import numpy as np

from jikgam.engine.events import read_log
from jikgam.engine.org import ROOT

FRAME_MS = 10
SILENCE_DBFS = -45.0


def onsets(left: np.ndarray, rate: int) -> list[float]:
    """무음 300ms 뒤에 소리가 시작되는 위치(ms) 목록."""
    n = rate * FRAME_MS // 1000
    frames = left[: len(left) // n * n].reshape(-1, n).astype(np.float64)
    rms = np.sqrt((frames ** 2).mean(axis=1)) / 32768.0
    voiced = 20 * np.log10(np.maximum(rms, 1e-9)) > SILENCE_DBFS
    out, quiet = [], 30
    for i, v in enumerate(voiced):
        if v and quiet >= 30:
            out.append(i * FRAME_MS)
        quiet = 0 if v else quiet + 1
    return out


def align(run_dir: Path) -> dict:
    rows = []
    for wav in sorted(run_dir.glob("*.wav")):
        log = read_log(run_dir / f"{wav.stem}.jsonl")
        segs = [e for e in log if e["type"] == "voice.segment"]
        with wave.open(str(wav), "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, ch)
        found = onsets(pcm[:, 0], rate)
        t0 = segs[0]["speech_start_ms"] - found[0]
        errs = [round(min(abs((t0 + o) - s["speech_start_ms"]) for o in found), 1) for s in segs]
        rows.append({"session": wav.stem, "file": wav.relative_to(ROOT).as_posix(),
                     "sha256_16": hashlib.sha256(wav.read_bytes()).hexdigest()[:16],
                     "duration_ms": round(len(pcm) / rate * 1000, 1), "sample_rate": rate,
                     "channels": {"left": "customer", "right": "agent"}, "t0_ms": round(t0, 1),
                     "t0_source": "estimated: first customer speech onset in the left channel aligned to the first "
                                  "voice.segment speech_start_ms",
                     "segment_onset_error_ms_max": max(errs)})
    return {"run_dir": run_dir.relative_to(ROOT).as_posix(), "recordings": rows}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    for d in sys.argv[1:]:
        run_dir = (ROOT / d).resolve()
        out = align(run_dir)
        (run_dir / "recordings.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                                 newline="\n")
        for r in out["recordings"]:
            print(f"{r['session']}: t0 {r['t0_ms']} ms, 구간 시작 최대 오차 {r['segment_onset_error_ms_max']} ms")


if __name__ == "__main__":
    main()
