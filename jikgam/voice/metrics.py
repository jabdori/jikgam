"""음성 재생 측정: 세션 이벤트 로그만으로 계산한다(docs/voice.md §5).

기준 시각(오디오상 발화 시작·종료)은 시험 실행기가 남긴 voice.segment의 speech_start_ms, speech_end_ms다. 그 값은 녹음
파일 안의 음성 위치(data/voice/manifest.json)에 그 파일을 넣기 시작한 엔진 시각을 더한 것이다. 엔진의 utterance.end는
VAD로 추정한 값이므로 둘의 차(t_end_error_ms)를 함께 낸다.

구간 i의 자막은 그 구간을 넣기 시작한 뒤부터 다음 구간을 넣기 시작하기 전까지 받은 stt.partial·stt.final이다. 실행기는
이전 구간의 응답이 재생되기 시작한 뒤에만 다음 구간을 넣으므로 자막이 다른 구간에 섞이지 않는다.

- first_partial_ms: 첫 미확정 자막을 받은 시각 − 발화 시작
- final_ms: 확정 자막을 받은 시각 − 발화 종료(한 구간이 둘 이상으로 확정되면 마지막 것. 나뉜 수는 finals)
- t_end_error_ms: 엔진에 넘긴 t_end_ms − 발화 종료
- first_audio_ms: 그 발화 뒤 첫 음성 노출(재생 시작) − 발화 종료. 노출한 제안의 기준 버전이 그 구간의 마지막 확정 발화가
  만든 버전 이상이어야 한다. 단계: 확정 자막 → agent.say(engine_ms) → tts.request → 재생 시작(tts_ms)
- refresh: jikgam.sim.metrics.refresh_latencies(docs/evaluation.md §3.2, t0 = utterance.end). 기준 발화 종료로 잰 값도 낸다
- barge_in: voice.barge_in마다 발화 시작부터 재생이 멈춘 시각(tts.playback_end)까지
- stale: jikgam.sim.metrics.stale_exposure의 voice 채널(docs/evaluation.md §3.3)
"""

from typing import Any

from jikgam.engine import events as ev
from jikgam.sim import metrics as sm

from . import bridge as vb

SEGMENT = "voice.segment"


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def _version_of(events: list[dict[str, Any]], utt_id: str) -> int | None:
    return next((int(e["to"]) for e in events if e["type"] == ev.VERSION_BUMPED and e.get("utt_id") == utt_id), None)


def segment_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segs = [e for e in events if e["type"] == SEGMENT]
    created = {e["suggestion_id"]: e for e in events if e["type"] == ev.SUGGESTION_CREATED}
    says = {e["suggestion_id"]: e for e in events if e["type"] == ev.AGENT_SAY}
    requests = {e["suggestion_id"]: e for e in events if e["type"] == vb.TTS_REQUEST}
    exposures = [e for e in events if e["type"] == ev.SUGGESTION_EXPOSED and e.get("channel") == vb.CHANNEL]
    rows = []
    for n, s in enumerate(segs):
        lo = s["feed_start_ms"]
        hi = segs[n + 1]["feed_start_ms"] if n + 1 < len(segs) else float("inf")
        inside = [e for e in events if lo <= e["t_ms"] < hi]
        partials = [e for e in inside if e["type"] == ev.STT_PARTIAL]
        finals = [e for e in inside if e["type"] == ev.STT_FINAL]
        row: dict[str, Any] = {"line": s["line"], "utt_file": s["file"], "script": s["text"], "marker": s.get("marker"),
                               "barge_in_planned": s.get("barge_in_planned", False),
                               "transcript": " / ".join(f["text"] for f in finals), "finals": len(finals),
                               "utt_ids": [f["utt_id"] for f in finals],
                               "first_partial_ms": _r(partials[0]["t_ms"] - s["speech_start_ms"]) if partials else None,
                               "final_ms": None, "t_end_error_ms": None, "t_end_source": None, "first_audio_ms": None,
                               "first_audio_from_t_end_ms": None, "engine_ms": None, "tts_ms": None,
                               "reply_suggestion": None}
        if finals:
            last = finals[-1]
            row["final_ms"] = _r(last["t_ms"] - s["speech_end_ms"])
            row["t_end_error_ms"] = _r(last["t_end_ms"] - s["speech_end_ms"])
            row["t_end_source"] = last.get("t_end_source")
            v = _version_of(events, last["utt_id"])
            x = next((x for x in exposures if v is not None and x["t_ms"] > last["t_ms"]
                      and created[x["suggestion_id"]]["basis_version"] >= v), None)
            if x is not None:
                sid = x["suggestion_id"]
                row["reply_suggestion"] = sid
                row["first_audio_ms"] = _r(x["start_ms"] - s["speech_end_ms"])
                row["first_audio_from_t_end_ms"] = _r(x["start_ms"] - last["t_end_ms"])
                if sid in says:
                    row["engine_ms"] = _r(says[sid]["t_ms"] - last["t_ms"])
                if sid in requests:
                    row["tts_ms"] = _r(x["start_ms"] - requests[sid]["t_ms"])
        row["replies_held"] = sum(e["type"] == vb.REPLY_HELD for e in inside)
        row["replies_discarded"] = sum(e["type"] == ev.RESULT_DISCARDED and e.get("stage") == "voice" for e in inside)
        rows.append(row)
    return rows


def barge_ins(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segs = [e for e in events if e["type"] == SEGMENT]
    ends = {e["suggestion_id"]: e for e in events if e["type"] == vb.PLAYBACK_END}
    out = []
    for b in (e for e in events if e["type"] == vb.BARGE_IN):
        seg = next((s for s in reversed(segs) if s["feed_start_ms"] <= b["t_ms"]), None)
        end = ends.get(b["suggestion_id"])
        out.append({"suggestion_id": b["suggestion_id"], "trigger": b.get("trigger"), "started": b["started"],
                    "played_ms": b.get("played_ms"), "line": seg["line"] if seg else None,
                    "detect_ms": _r(b["t_ms"] - seg["speech_start_ms"]) if seg else None,
                    "stop_ms": _r(end["end_ms"] - seg["speech_start_ms"]) if seg and end else None})
    return out


def refresh(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segs = [e for e in events if e["type"] == SEGMENT]
    rows = []
    for r in sm.refresh_latencies(events):
        utt = next((e for e in events if e["type"] == ev.STT_FINAL and e["utt_id"] == r["utt_id"]), None)
        seg = next((s for s in reversed(segs) if utt and s["feed_start_ms"] <= utt["t_ms"]), None)
        rows.append({**r, "latency_from_speech_end_ms": _r(r["t1"] - seg["speech_end_ms"])
                     if seg and r["t1"] is not None else None})
    return rows


def session_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    stale = sm.stale_exposure(events)
    voice = stale["by_channel"].get(vb.CHANNEL, {"invalid": 0, "denominator": 0, "unconfirmed": 0, "cases": []})
    playbacks = [e for e in events if e["type"] == vb.PLAYBACK_END]
    return {
        "segments": segment_rows(events),
        "refresh": refresh(events),
        "barge_ins": barge_ins(events),
        "stale_voice": {**voice, "events": stale["events"]},
        "playbacks": {"total": len(playbacks), "exposed": sum(p["exposed"] for p in playbacks),
                      "interrupted": {k: sum(p.get("interrupted") == k for p in playbacks)
                                      for k in ("barge_in", "version", "timeout", "error")},
                      "played_ms": round(sum(p.get("played_ms") or 0 for p in playbacks), 1)},
        "voice_discarded": sum(e["type"] == ev.RESULT_DISCARDED and e.get("stage") == "voice" for e in events),
        "voice_held": sum(e["type"] == vb.REPLY_HELD for e in events),
        "voice_errors": [e.get("error") for e in events if e["type"] == vb.VOICE_ERROR],
        "engine_errors": [e.get("error") for e in events if e["type"] == ev.ENGINE_ERROR],
    }
