"""개발용 재생의 고객 발화를 Gemini TTS로 음성 파일로 만든다(음성 테스트 입력).

    uv run python -m jikgam.voice.customer_audio --replays s01 s20 s21

- 입력: data/replays/<재생>.jsonl의 줄마다 text(와 marker). 고객 목소리는 config/voice.toml [customer_tts].
- 출력: data/voice/<재생>/uNN.wav(16kHz 모노 16비트)와 data/voice/manifest.json. 앞뒤 무음을 잘라 내고 앞 200ms·뒤
  300ms 무음을 붙이므로 파일 안의 음성 시작은 200ms다. 음성 끝(speech_end_ms)과 음성 사이 가장 긴 쉼(max_pause_ms)을
  매니페스트에 적는다. 쉼은 VAD 끝점(config/voice.toml [vad].stop_secs)을 정할 때 본다.
- 같은 문장이라도 다시 만들면 다른 음성이 나온다. 그래서 있는 파일은 --force 없이는 다시 만들지 않고, 매니페스트에
  sha256을 남겨 측정에 쓴 파일을 확인할 수 있게 한다.
- 파일이 300KB 이상이면 저장소에 넣지 않는다(data/voice/.gitignore에 그 파일을 적는다).

재시도: HTTP 429·5xx와 연결 오류는 2, 4, 8초 뒤 최대 3번 다시 보낸다(docs/evaluation.md §2.4와 같은 규칙).
"""

import argparse
import asyncio
import hashlib
import io
import json
import sys
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from jikgam.decision.registry import secrets
from jikgam.engine.events import KST
from jikgam.engine.org import ROOT

from .audio import duration_ms, resample, speech_bounds, trim_and_pad, write_wav
from .config import TtsConfig, load_voice_config

OUT_DIR = ROOT / "data/voice"
SAMPLE_RATE = 16000  # STT 입력(Gemini Live는 16kHz 모노 PCM을 권장한다)
LEAD_MS = 200
TAIL_MS = 300
MAX_BYTES = 300 * 1024
RETRY_DELAYS_S = (2, 4, 8)


def _pcm_from_part(data: bytes, mime: str) -> tuple[bytes, int]:
    """TTS 응답 조각을 (PCM, 샘플레이트)로. 한 번에 받으면 audio/wav, 스트리밍이면 audio/l16; rate=24000이다."""
    if mime.startswith("audio/wav") or data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate()
    rate = 24000
    for p in mime.split(";"):
        if p.strip().startswith("rate="):
            rate = int(p.strip()[5:])
    return data, rate


async def synthesize(client, cfg: TtsConfig, text: str) -> tuple[bytes, int, dict[str, Any]]:
    from google.genai import errors, types

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(language_code=cfg.language, voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice))))
    for attempt, delay in enumerate((*RETRY_DELAYS_S, None)):
        try:
            r = await client.aio.models.generate_content(model=cfg.model, contents=text, config=config)
            break
        except (errors.ServerError, errors.ClientError) as e:
            code = getattr(e, "code", 0) or 0
            if delay is None or not (code == 429 or code >= 500):
                raise
            await asyncio.sleep(delay)
    part = r.candidates[0].content.parts[0]
    pcm, rate = _pcm_from_part(part.inline_data.data, part.inline_data.mime_type or "")
    u = r.usage_metadata
    usage = {"model_version": r.model_version, "input_tokens": u.prompt_token_count or 0,
             "audio_tokens": u.candidates_token_count or 0, "attempts": attempt + 1}
    usage["cost_usd"] = round(usage["input_tokens"] * cfg.price_input_per_mtok / 1e6
                              + usage["audio_tokens"] * cfg.price_audio_per_mtok / 1e6, 6)
    return pcm, rate, usage


async def build(replays: list[str], force: bool) -> dict[str, Any]:
    from google import genai

    vc = load_voice_config()
    cfg = TtsConfig(**{**vars(vc.customer_tts), "price_input_per_mtok": vc.tts.price_input_per_mtok,
                       "price_audio_per_mtok": vc.tts.price_audio_per_mtok})
    client = genai.Client(api_key=secrets()[cfg.api_key_env])
    manifest_path = OUT_DIR / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": []}
    entries = {(f["replay"], f["utt_id"]): f for f in old["files"]}
    for name in replays:
        rows = [json.loads(x) for x in (ROOT / f"data/replays/{name}.jsonl").read_text(encoding="utf-8").splitlines()
                if x.strip()]
        for i, row in enumerate(rows, 1):
            utt_id = f"u{i}"
            path = OUT_DIR / name / f"{utt_id}.wav"
            prev = entries.get((name, utt_id))
            if path.exists() and prev and prev["text"] == row["text"] and not force:
                continue
            pcm, rate, usage = await synthesize(client, cfg, row["text"])
            pcm = trim_and_pad(resample(pcm, rate, SAMPLE_RATE), SAMPLE_RATE, LEAD_MS, TAIL_MS)
            write_wav(path, pcm, SAMPLE_RATE)
            b = speech_bounds(pcm, SAMPLE_RATE)
            data = path.read_bytes()
            entries[(name, utt_id)] = {
                "replay": name, "utt_id": utt_id, "line": i, "path": path.relative_to(ROOT).as_posix(),
                "text": row["text"], "marker": row.get("marker"), "bytes": len(data),
                "duration_ms": round(duration_ms(pcm, SAMPLE_RATE), 1), "speech_start_ms": b.start_ms,
                "speech_end_ms": b.end_ms, "max_pause_ms": b.max_pause_ms,
                "sha256_16": hashlib.sha256(data).hexdigest()[:16], "voice": cfg.voice, "model": cfg.model, **usage,
                "generated": datetime.now(KST).isoformat(timespec="seconds")}
            print(f"{name} {utt_id} {len(data) / 1024:.0f}KB {b.end_ms - b.start_ms:.0f}ms 최대 쉼 {b.max_pause_ms:.0f}ms "
                  f"${usage['cost_usd']:.5f}  {row['text']}")
    files = sorted(entries.values(), key=lambda f: (f["replay"], f["line"]))
    manifest = {"sample_rate": SAMPLE_RATE, "lead_ms": LEAD_MS, "tail_ms": TAIL_MS,
                "silence_dbfs": -45.0, "files": files}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    large = sorted(f["path"] for f in files if f["bytes"] >= MAX_BYTES)
    ignore = OUT_DIR / ".gitignore"
    if large:
        ignore.write_text("# 300KB 이상인 파일은 저장소에 넣지 않는다(jikgam/voice/customer_audio.py로 다시 만든다)\n"
                          + "".join(f"{Path(p).relative_to('data/voice').as_posix()}\n" for p in large),
                          encoding="utf-8", newline="\n")
    elif ignore.exists():
        ignore.unlink()
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replays", nargs="+", default=["s01", "s20", "s21"])
    ap.add_argument("--force", action="store_true", help="있는 파일도 다시 만든다(다른 음성이 나온다)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    m = asyncio.run(build(args.replays, args.force))
    cost = sum(f.get("cost_usd", 0) for f in m["files"])
    print(f"파일 {len(m['files'])}개, 최대 {max(f['bytes'] for f in m['files']) / 1024:.0f}KB, "
          f"최대 쉼 {max(f['max_pause_ms'] for f in m['files']):.0f}ms, TTS 비용 합(추정) ${cost:.5f}")


if __name__ == "__main__":
    main()
