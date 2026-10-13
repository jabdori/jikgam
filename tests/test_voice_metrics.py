"""음성 재생 측정(jikgam.voice.metrics)을 손으로 만든 로그로 확인한다."""

from jikgam.voice import metrics as vm

MARK = {"condition_change": True, "invalidates_prior": True, "event": "s20#0"}


def e(t, type_, version=0, **kw):
    return {"t_ms": t, "type": type_, "version": version, "session": "x", **kw}


def log():
    return [
        e(1000, "voice.segment", line=1, file="a.wav", text="해지 말고요", marker=None, feed_start_ms=1000,
          speech_start_ms=1200, speech_end_ms=3000, feed_end_ms=3320),
        e(2000, "stt.partial", utt_id="u1", text="해지"),
        e(3800, "stt.final", utt_id="u1", text="해지 말고요", t_end_ms=3020, t_end_source="vad"),
        e(3020, "utterance.end", utt_id="u1", speaker="customer", text="해지 말고요", final=True, marker=None),
        e(3801, "version.bumped", 1, cause="utterance", utt_id="u1", **{"from": 0, "to": 1}),
        e(4000, "evidence.updated", 1),
        e(6000, "suggestion.created", 1, suggestion_id="s1", basis_version=1),
        e(6000, "agent.say", 1, suggestion_id="s1", text="안내 1"),
        e(6000, "suggestion.exposed", 1, suggestion_id="s1", channel="chat", start_ms=6000, end_ms=6000),
        e(6001, "tts.request", 1, suggestion_id="s1", basis_version=1, chars=4),
        e(6800, "suggestion.exposed", 1, suggestion_id="s1", channel="voice", start_ms=6800, end_ms=None),
        e(7800, "voice.segment", 1, line=2, file="b.wav", text="그냥 해지할게요", marker=MARK, feed_start_ms=7800,
          speech_start_ms=8000, speech_end_ms=9500, feed_end_ms=9820, barge_in_planned=True),
        e(8400, "voice.barge_in", 1, suggestion_id="s1", trigger="vad", started=True, played_ms=1600),
        e(8402, "tts.playback_end", 1, suggestion_id="s1", exposed=True, start_ms=6800, end_ms=8402,
          interrupted="barge_in", played_ms=1602),
        e(8700, "stt.partial", 1, utt_id="u2", text="그냥"),
        # 확정 자막을 기다리는 사이 지난 버전 응답이 재생되기 시작했다(무효 노출 1건)
        e(9600, "suggestion.created", 1, suggestion_id="s1b", basis_version=1),
        e(9700, "suggestion.exposed", 1, suggestion_id="s1b", channel="voice", start_ms=9700, end_ms=None),
        e(10300, "stt.final", 1, utt_id="u2", text="그냥 해지할게요", t_end_ms=9520, t_end_source="vad"),
        e(9520, "utterance.end", 1, utt_id="u2", speaker="customer", text="그냥 해지할게요", final=True, marker=MARK),
        e(10301, "version.bumped", 2, cause="utterance", utt_id="u2", **{"from": 1, "to": 2}),
        e(10500, "evidence.updated", 2),
        e(12500, "suggestion.created", 2, suggestion_id="s2", basis_version=2),
        e(12500, "agent.say", 2, suggestion_id="s2", text="안내 2"),
        e(12501, "tts.request", 2, suggestion_id="s2", basis_version=2, chars=4),
        e(13300, "suggestion.exposed", 2, suggestion_id="s2", channel="voice", start_ms=13300, end_ms=None),
        e(16000, "tts.playback_end", 2, suggestion_id="s2", exposed=True, start_ms=13300, end_ms=16000,
          interrupted=None, played_ms=2700),
    ]


def test_segment_rows():
    rows = vm.segment_rows(log())
    r1, r2 = rows
    assert (r1["first_partial_ms"], r1["final_ms"], r1["t_end_error_ms"]) == (800, 800, 20)
    assert (r1["first_audio_ms"], r1["first_audio_from_t_end_ms"], r1["engine_ms"], r1["tts_ms"]) == (3800, 3780, 2200, 799)
    assert r1["reply_suggestion"] == "s1" and r1["finals"] == 1
    assert (r2["first_partial_ms"], r2["final_ms"], r2["t_end_error_ms"]) == (700, 800, 20)
    # 지난 버전 응답(s1b)은 이 발화에 대한 응답이 아니다
    assert r2["reply_suggestion"] == "s2" and r2["first_audio_ms"] == 3800
    assert r2["marker"]["event"] == "s20#0" and r2["barge_in_planned"]


def test_refresh_barge_in_and_stale_voice():
    s = vm.session_summary(log())
    (r,) = s["refresh"]
    assert r["event"] == "s20#0" and r["latency_ms"] == 980 and r["latency_from_speech_end_ms"] == 1000
    (b,) = s["barge_ins"]
    assert (b["line"], b["trigger"], b["detect_ms"], b["stop_ms"]) == (2, "vad", 400, 402)
    st = s["stale_voice"]
    assert (st["invalid"], st["denominator"]) == (1, 2)
    assert st["cases"][0]["suggestion_id"] == "s1b"
    assert s["playbacks"]["interrupted"]["barge_in"] == 1
