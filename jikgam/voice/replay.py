"""개발용 재생을 음성으로 돌린다: 고객 녹음 → 실제 STT → 엔진 → 실제 TTS → (재생 흉내) 출력.

    uv run python -m jikgam.voice.replay --replays s01 s20 s21 --bizapi-url http://127.0.0.1:8096

- 고객 음성은 data/voice/<재생>/uNN.wav(jikgam/voice/customer_audio.py로 만든 것)다. 정책은 C(엔진 + 조직 기본 판별
  백엔드)이고, 엔진 코드와 설정은 채팅 재생과 같다.
- 실행 순서: 모의 업무 API 초기화 → 세션 열기와 예열(start) → 파이프라인 시작 → 줄마다 녹음 넣기 → 마지막 응답 재생이
  끝나면 정리.
- 줄을 넣는 때: 보통은 앞 줄에 대한 엔진 응답의 재생이 끝나고 gap_ms(700ms) 뒤. 조건 변경 표시(marker.condition_change)가
  있는 줄은 앞 줄에 대한 응답이 재생되기 시작하고 barge_in_after_ms(1,000ms) 뒤에 넣는다. 응답이 끝나기 전에 고객이
  정정을 시작하는 끼어들기다(채팅 시뮬레이터의 빠른 정정에 해당, docs/evaluation.md §3.3 보완). "앞 줄에 대한 응답"은
  기준 버전이 그 줄을 넣기 직전의 세션 버전보다 큰 첫 음성 노출이다.
- 조건 변경 표시는 그 줄의 녹음 구간에서 처음 확정된 발화에 붙인다(EngineBridge의 marker_for). STT가 한 줄을 둘 이상으로
  확정하면 나머지에는 붙지 않으며 보고서의 finals로 드러난다.
- 결과: results/voice/<run_id>/<세션>.jsonl(엔진과 음성 계층 이벤트), <세션>.wav(스테레오 녹음, 저장소 밖),
  <세션>.metrics.json, summary.json, pipecat.log(저장소 밖).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from jikgam.engine import events as ev
from jikgam.engine.chat import session_cost
from jikgam.engine.org import ROOT, load_org

from . import bridge as vb
from .audio import duration_ms, read_wav
from .config import VoiceConfig, load_voice_config
from .metrics import SEGMENT, session_summary

OUT_DIR = ROOT / "results/voice"
MANIFEST = ROOT / "data/voice/manifest.json"
GAP_MS = 700.0  # 앞 응답 재생이 끝난 뒤 고객이 말을 시작하기까지
BARGE_IN_AFTER_MS = 1000.0  # 조건 변경 줄: 앞 응답 재생 시작부터 고객이 말을 시작하기까지
REPLY_TIMEOUT_S = 60.0
TTS_TOKENS_PER_S = 25.0  # Gemini TTS 오디오 출력 토큰(2026-10-14 측정: 3.2초 78토큰, 8.6초 212토큰)


def reset_bizapi(url: str) -> None:
    """관리 엔드포인트에는 인증이 없으므로 로컬 주소에만 보낸다."""
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"로컬 주소가 아니라 관리 요청을 보내지 않는다: {url}")
    httpx.post(f"{url}/_admin/reset", timeout=5).raise_for_status()


def manifest_files(name: str) -> list[dict[str, Any]]:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = sorted((f for f in m["files"] if f["replay"] == name), key=lambda f: f["line"])
    if not files:
        raise SystemExit(f"{name}의 녹음이 없다. 먼저 python -m jikgam.voice.customer_audio --replays {name}")
    return files


def reply_after(bridge: vb.EngineBridge, version: int) -> vb.Playback | None:
    """기준 버전이 version보다 큰 첫 재생(시작된 것). 그 버전 뒤의 고객 발화에 대한 응답이다."""
    return next((p for p in bridge.playbacks if p.version > version and p.start_ms is not None), None)


class Segments:
    """넣은 녹음 구간과 조건 변경 표시. 확정 발화에 표시를 붙이는 marker_for를 준다."""

    def __init__(self):
        self.items: list[dict[str, Any]] = []
        self._claimed: set[int] = set()

    def marker_for(self, speech_start_ms: float | None, t_end_ms: float) -> dict[str, Any] | None:
        idx = max((i for i, s in enumerate(self.items) if s["feed_start_ms"] <= t_end_ms), default=None)
        if idx is None or idx in self._claimed:
            return None
        self._claimed.add(idx)
        return self.items[idx].get("marker")


async def run_one(name: str, run_dir: Path, run_id: str, cfg: VoiceConfig, bizapi_url: str, gap_ms: float,
                  barge_in_after_ms: float) -> dict[str, Any]:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    from jikgam.engine.session import Session

    from .pipeline import IN_RATE, OUT_RATE, build_voice_pipeline
    from .transport import PacedAudioTransport, Recorder

    files = manifest_files(name)
    org = load_org("org_a")
    reset_bizapi(bizapi_url)
    sid = f"{name}-voice-{run_id}"
    session = Session(org, sid, run_dir, channel=vb.CHANNEL)  # 노출은 연결 계층이 재생 시작에 남긴다
    await session.start()
    recorder = Recorder()
    transport = PacedAudioTransport(TransportParams(audio_in_enabled=True, audio_in_sample_rate=IN_RATE,
                                                    audio_out_enabled=True, audio_out_sample_rate=OUT_RATE), recorder)
    segs = Segments()
    vp = build_voice_pipeline(session, cfg, transport, marker_for=segs.marker_for)
    bridge = vp.bridge
    started = asyncio.Event()

    @vp.worker.event_handler("on_pipeline_started")
    async def _started(_worker, _frame):
        started.set()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(vp.worker)
    run = asyncio.create_task(runner.run())
    t_open = ev.now_ms()
    notes: list[str] = []
    try:
        await asyncio.wait_for(started.wait(), 30)
        session.log.emit(vb.VOICE_SESSION, session.version, **cfg.log(), pipecat=pkg_version("pipecat-ai"),
                         google_genai=pkg_version("google-genai"), policy="C", decision_backend=session.backend,
                         gap_ms=gap_ms, barge_in_after_ms=barge_in_after_ms,
                         clock="engine monotonic ms; t_end_ms = VAD stop decision (wall→engine) - stop_secs",
                         audio=[{"file": f["path"], "sha256_16": f["sha256_16"]} for f in files])
        await asyncio.sleep(0.5)
        for i, f in enumerate(files):
            barge = bool((f.get("marker") or {}).get("condition_change")) and i > 0
            if i > 0:
                prev_v = segs.items[-1]["version_before"]
                if not await bridge.wait_until(lambda: reply_after(bridge, prev_v) is not None, REPLY_TIMEOUT_S):
                    notes.append(f"{f['utt_id']}: 앞 줄에 대한 응답 재생이 {REPLY_TIMEOUT_S:.0f}초 안에 시작되지 않았다")
                elif barge:
                    await asyncio.sleep(barge_in_after_ms / 1000)
                    if reply_after(bridge, prev_v).done:
                        notes.append(f"{f['utt_id']}: 끼어들기 계획이었지만 앞 응답이 {barge_in_after_ms:.0f}ms 안에 끝났다")
                else:
                    await bridge.wait_until(lambda: reply_after(bridge, prev_v).done, REPLY_TIMEOUT_S * 3)
                    await asyncio.sleep(gap_ms / 1000)
            pcm, rate = read_wav(ROOT / f["path"])
            if rate != IN_RATE:
                raise SystemExit(f"{f['path']}: 샘플레이트 {rate}Hz, {IN_RATE}Hz여야 한다")
            seg = {"line": f["line"], "file": f["path"], "text": f["text"], "marker": f.get("marker"),
                   "barge_in_planned": barge, "version_before": session.version}
            fed = transport.input().enqueue(pcm)
            t_start = await fed.started  # 이 구간의 자막은 이 뒤에만 온다
            seg.update(feed_start_ms=t_start, speech_start_ms=round(t_start + f["speech_start_ms"], 3),
                       speech_end_ms=round(t_start + f["speech_end_ms"], 3))
            segs.items.append(seg)
            seg["feed_end_ms"] = await fed.finished + 20
            session.log.emit(SEGMENT, session.version, t_ms=t_start, **seg)
        last_v = segs.items[-1]["version_before"]
        if await bridge.wait_until(lambda: reply_after(bridge, last_v) is not None, REPLY_TIMEOUT_S):
            await bridge.wait_until(lambda: reply_after(bridge, last_v).done, REPLY_TIMEOUT_S * 3)
        else:
            notes.append("마지막 줄에 대한 응답 재생이 시작되지 않았다")
        await asyncio.sleep(1.0)
        if not await bridge.drain(REPLY_TIMEOUT_S):
            notes.append("끝낼 때 엔진 처리나 응답 재생이 남아 있었다")
    finally:
        t_close = ev.now_ms()
        await vp.worker.end()
        try:
            await asyncio.wait_for(run, 30)
        except TimeoutError:
            notes.append("파이프라인이 30초 안에 끝나지 않았다")
            await vp.worker.cancel()
        await session.aclose()
    recorder.save(run_dir / f"{sid}.wav", t_open)
    log = ev.read_log(run_dir / f"{sid}.jsonl")
    summary = session_summary(log)
    agent_ms = sum(duration_ms(pcm, r) for _, pcm, r in recorder.agent)
    stream_min = (t_close - t_open) / 60000
    summary.update(replay=name, session=sid, notes=notes, stt_stream_min=round(stream_min, 2),
                   tts_audio_s=round(agent_ms / 1000, 1),
                   cost_usd={"engine": round(session_cost(log), 5),
                             "stt_estimate": round(stream_min * cfg.stt.price_usd_per_min, 5),
                             "tts_estimate": round(agent_ms / 1000 * TTS_TOKENS_PER_S * cfg.tts.price_audio_per_mtok / 1e6, 5)})
    write_json(run_dir / f"{sid}.metrics.json", summary)
    return summary


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def report(s: dict[str, Any]) -> str:
    lines = [f"=== {s['replay']} ({s['session']})"]
    lines.append("  줄  첫자막  확정   t_end오차  첫음성  (t_end부터)  엔진   TTS   확정수 표시  인식 결과")
    for r in s["segments"]:
        mark = "변경" if (r["marker"] or {}).get("condition_change") else "    "
        def f(x):
            return "-" if x is None else f"{x:.0f}"
        lines.append(f"  u{r['line']:<2} {f(r['first_partial_ms']):>6} {f(r['final_ms']):>6} {f(r['t_end_error_ms']):>8} "
                     f"{f(r['first_audio_ms']):>7} {f(r['first_audio_from_t_end_ms']):>9} {f(r['engine_ms']):>6} "
                     f"{f(r['tts_ms']):>5} {r['finals']:>5}  {mark}  {r['transcript']}")
    for r in s["refresh"]:
        lines.append(f"  근거 갱신 {r['event']}: {r['latency_ms']} ms(utterance.end 기준), "
                     f"{r['latency_from_speech_end_ms']} ms(기준 발화 종료 기준)")
    for b in s["barge_ins"]:
        lines.append(f"  끼어들기 u{b['line']} {b['trigger']}: 감지 {b['detect_ms']} ms, 재생 멈춤 {b['stop_ms']} ms"
                     f"(발화 시작 기준), 재생 {b['played_ms']} ms 뒤")
    st = s["stale_voice"]
    lines.append(f"  무효 노출(음성): {st['invalid']}/{st['denominator']}, 미재확인 {st['unconfirmed']}")
    lines.append(f"  재생 {s['playbacks']['total']}건(노출 {s['playbacks']['exposed']}), 끊김 {s['playbacks']['interrupted']}, "
                 f"음성 계층 보류 {s['voice_held']}, 폐기 {s['voice_discarded']}")
    if s["voice_errors"] or s["engine_errors"]:
        lines.append(f"  오류: 음성 {s['voice_errors']}, 엔진 {s['engine_errors']}")
    for n in s["notes"]:
        lines.append(f"  참고: {n}")
    c = s["cost_usd"]
    lines.append(f"  비용: 엔진 ${c['engine']:.5f}, STT 추정 ${c['stt_estimate']:.5f}({s['stt_stream_min']}분), "
                 f"TTS 추정 ${c['tts_estimate']:.5f}({s['tts_audio_s']}초)")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replays", nargs="+", default=["s01", "s20", "s21"])
    ap.add_argument("--bizapi-url", required=True, help="로컬 모의 업무 API 주소(실행마다 초기화한다)")
    ap.add_argument("--run-id", default=datetime.now(ev.KST).strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--gap-ms", type=float, default=GAP_MS)
    ap.add_argument("--barge-in-after-ms", type=float, default=BARGE_IN_AFTER_MS)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ["JIKGAM_BIZAPI_URL"] = args.bizapi_url  # 조직 설정의 bizapi_url보다 우선한다(docs/engine.md §9)
    run_dir = OUT_DIR / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(run_dir / "pipecat.log", level="DEBUG", encoding="utf-8")
    cfg = load_voice_config()
    summaries = []
    for name in args.replays:
        s = await run_one(name, run_dir, args.run_id, cfg, args.bizapi_url, args.gap_ms, args.barge_in_after_ms)
        summaries.append(s)
        print(report(s), flush=True)
    total = {k: round(sum(s["cost_usd"][k] for s in summaries), 5) for k in ("engine", "stt_estimate", "tts_estimate")}
    write_json(run_dir / "summary.json", {"run_id": args.run_id, "replays": args.replays, "cost_usd": total,
                                          "sessions": summaries})
    print(f"결과: {run_dir}\n비용 합: {total}")


if __name__ == "__main__":
    asyncio.run(main())
