"""음성 평가(docs/evaluation-voice.md)에 더한 것: 정책 A의 음성 채널, stale_guard를 끈 음성 연결, 음성 지표 추가분,
판정기의 음성 대화록, 고객 AI 음성 판, 음성 실행기의 도움 함수."""

import asyncio

from test_rag import ScriptedLLM
from test_rag import final as rag_final
from test_voice_bridge import FakeSpeaker, make_session, run, types, vad_start, vad_stop
from test_voice_bridge import final as stt_final

from engine_fakes import GatedGenerator, utt
from jikgam.baselines.rag import RagSession
from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.org import load_org
from jikgam.sim import customer as cust
from jikgam.sim import judge as jd
from jikgam.voice import bridge as vb
from jikgam.voice import metrics as vm
from jikgam.voice.bridge import EngineBridge, Playback
from jikgam.voice.sim import heard, read_digits, tts_text

ORG = load_org("org_a")


# --- 정책 A의 음성 채널 ----------------------------------------------------------------------------


def test_rag_voice_channel_leaves_exposure_to_the_voice_layer(tmp_path):
    async def go(sid, channel):
        s = RagSession(ORG, sid, tmp_path, llm=ScriptedLLM([[rag_final("안내합니다.")]]),
                       bizapi=BizApiClient("http://127.0.0.1:9", sid), channel=channel)
        out = await s.handle(utt("u1", "환불 규정이 궁금해요."))
        await s.aclose()
        return s, out

    s, out = asyncio.run(go("r-voice", vb.CHANNEL))
    log = ev.read_log(tmp_path / "r-voice.jsonl")
    assert s.channel == vb.CHANNEL and types(log, ev.SESSION_START)[0]["channel"] == vb.CHANNEL
    assert types(log, ev.AGENT_SAY) and not types(log, ev.SUGGESTION_EXPOSED)
    _, out = asyncio.run(go("r-chat", "chat"))  # 기본(채팅)은 그대로 노출을 남긴다
    assert [x["channel"] for x in types(ev.read_log(tmp_path / "r-chat.jsonl"), ev.SUGGESTION_EXPOSED)] == ["chat"]


# --- stale_guard를 끈 음성 연결(정책 A) ----------------------------------------------------------------


def test_without_stale_guard_old_reply_plays_after_customer_stops(tmp_path):
    """stale_guard=False: 고객이 말하는 동안에는 붙잡지만, 말이 끝나면 확정 자막을 기다리지 않고 지난 답도 재생한다."""
    gen = GatedGenerator()
    s = make_session(tmp_path, "v-noguard", generator=gen)
    b = EngineBridge(s, stale_guard=False)
    sp = FakeSpeaker()

    async def script(w):
        await w.queue_frame(vad_start())
        await w.queue_frame(vad_stop())
        await w.queue_frame(stt_final("다음 결제일이 궁금해요."))
        await asyncio.wait_for(gen.entered.wait(), 5)
        await w.queue_frame(vad_start())  # 고객이 다시 말한다
        gen.release.set()
        assert await b.wait_until(lambda: not b._turns, 5)
        await asyncio.sleep(0.2)
        assert b.playbacks == []  # 말하는 중에는 시작하지 않는다(공통 기능)
        await w.queue_frame(vad_stop())
        assert await b.wait_until(lambda: bool(b.playbacks) and b.playbacks[0].start_ms is not None, 5)
        await w.queue_frame(stt_final("아 그냥 해지할게요."))
        assert await b.wait_until(lambda: len(b.playbacks) == 2 and b.playbacks[1].done, 10)

    run(s, b, sp, script)
    log = ev.read_log(tmp_path / "v-noguard.jsonl")
    says = types(log, ev.AGENT_SAY)
    assert sp.spoken == [says[0]["text"], says[1]["text"]]
    assert not [d for d in types(log, ev.RESULT_DISCARDED) if d.get("stage") == "voice"]
    assert all(p["interrupted"] != "version" for p in types(log, vb.PLAYBACK_END))


def test_without_stale_guard_version_change_does_not_cut_playback(tmp_path):
    s = make_session(tmp_path, "v-noguard2")
    b = EngineBridge(s, stale_guard=False)
    sp = FakeSpeaker(start_delay=0.5, duration=0.5)

    async def script(w):
        await w.queue_frame(stt_final("환불 규정이 궁금해요."))
        assert await b.wait_until(lambda: bool(b.playbacks), 10)
        await w.queue_frame(stt_final("아 그냥 해지할게요."))
        assert await b.wait_until(lambda: len(b.playbacks) == 2 and b.playbacks[1].done, 10)

    run(s, b, sp, script)
    ends = types(ev.read_log(tmp_path / "v-noguard2.jsonl"), vb.PLAYBACK_END)
    assert [x["interrupted"] for x in ends] == [None, None]
    assert b.turn_outputs and any(e["type"] == ev.AGENT_SAY for e in b.turn_outputs)


# --- 음성 지표 추가분 ---------------------------------------------------------------------------------

MARK = {"condition_change": True, "invalidates_prior": True, "event": "h01#0"}


def e(t, type_, version=0, **kw):
    return {"t_ms": t, "type": type_, "version": version, "session": "x", **kw}


def rag_log():
    """정책 A 모양의 로그: version.bumped가 없고, 답은 받은 순서대로 모두 재생된다."""
    return [
        e(1000, "voice.segment", line=1, text="환불해 주세요", marker=None, feed_start_ms=1000,
          speech_start_ms=1200, speech_end_ms=2500, feed_end_ms=2820),
        e(3300, "stt.final", utt_id="u1", text="환불해 주세요", t_end_ms=2510, t_end_source="vad"),
        e(2510, "utterance.end", 0, utt_id="u1", speaker="customer", text="환불해 주세요", final=True, marker=None),
        e(3500, "voice.segment", line=2, text="아니 해지만요", marker=MARK, feed_start_ms=3500,
          speech_start_ms=3700, speech_end_ms=5000, feed_end_ms=5320),
        e(5800, "stt.final", utt_id="u2", text="아니 해지만요", t_end_ms=5010, t_end_source="vad"),
        e(5010, "utterance.end", 1, utt_id="u2", speaker="customer", text="아니 해지만요", final=True, marker=MARK),
        e(9000, "evidence.updated", 1),
        e(9000, "suggestion.created", 1, suggestion_id="a1", basis_version=1, text="환불 안내"),
        e(9000, "agent.say", 1, suggestion_id="a1", text="환불 안내"),
        e(9900, "suggestion.exposed", 2, suggestion_id="a1", channel="voice", start_ms=9900, end_ms=None),
        e(13900, "tts.playback_end", 2, suggestion_id="a1", exposed=True, start_ms=9900, end_ms=13900,
          interrupted=None, played_ms=4000),
        e(16000, "evidence.updated", 2),
        e(16000, "suggestion.created", 2, suggestion_id="a2", basis_version=2, text="해지 안내"),
        e(16000, "agent.say", 2, suggestion_id="a2", text="해지 안내"),
        e(16800, "suggestion.exposed", 2, suggestion_id="a2", channel="voice", start_ms=16800, end_ms=None),
        e(19000, "tts.playback_end", 2, suggestion_id="a2", exposed=True, start_ms=16800, end_ms=19000,
          interrupted=None, played_ms=2200),
        e(19500, "voice.recording", 2, file="results/voice/x.wav", t0_ms=500.0, sha256_16="abc", duration_ms=19500),
    ]


def test_new_condition_audio_and_stale_played_time_without_version_bumped():
    s = vm.session_summary(rag_log())
    st = s["stale_voice"]
    assert (st["invalid"], st["denominator"]) == (1, 2)
    assert [(c["suggestion_id"], c["played_ms"]) for c in st["cases"]] == [("a1", 4000)]
    (row,) = s["new_condition_audio"]
    assert row["version"] == 2 and row["suggestion_id"] == "a2"
    assert row["from_speech_end_ms"] == 11800 and row["from_t0_ms"] == 11790
    assert s["refresh"][0]["latency_ms"] == 10990  # §3.2: 새 버전의 evidence.updated
    seg = vm.segment_rows(rag_log())[1]  # 버전 폴백으로 정책 A에서도 첫 음성을 찾는다
    assert seg["reply_suggestion"] == "a2" and seg["first_audio_ms"] == 11800
    assert s["recording"]["t0_ms"] == 500.0


def test_barge_in_new_audio_after_interruption():
    log = [
        e(1000, "voice.segment", line=1, text="해지할게요", marker=None, feed_start_ms=1000, speech_start_ms=1200,
          speech_end_ms=2000, feed_end_ms=2320),
        e(2800, "stt.final", utt_id="u1", text="해지할게요", t_end_ms=2010, t_end_source="vad"),
        e(2010, "utterance.end", 0, utt_id="u1", speaker="customer", text="해지할게요", final=True, marker=None),
        e(2801, "version.bumped", 1, utt_id="u1", **{"from": 0, "to": 1}),
        e(3000, "suggestion.created", 1, suggestion_id="c1", basis_version=1),
        e(4000, "suggestion.exposed", 1, suggestion_id="c1", channel="voice", start_ms=4000, end_ms=None),
        e(5000, "voice.segment", 1, line=2, text="아니 환불이요", marker=MARK, feed_start_ms=5000,
          speech_start_ms=5200, speech_end_ms=6200, feed_end_ms=6520),
        e(5400, "voice.barge_in", 1, suggestion_id="c1", trigger="vad", started=True, played_ms=1400),
        e(5410, "tts.playback_end", 1, suggestion_id="c1", exposed=True, start_ms=4000, end_ms=5410,
          interrupted="barge_in", played_ms=1410),
        e(7000, "stt.final", 1, utt_id="u2", text="아니 환불이요", t_end_ms=6210, t_end_source="vad"),
        e(6210, "utterance.end", 1, utt_id="u2", speaker="customer", text="아니 환불이요", final=True, marker=MARK),
        e(7001, "version.bumped", 2, utt_id="u2", **{"from": 1, "to": 2}),
        e(7300, "suggestion.created", 2, suggestion_id="c2", basis_version=2),
        e(8300, "suggestion.exposed", 2, suggestion_id="c2", channel="voice", start_ms=8300, end_ms=None),
    ]
    (b,) = vm.barge_ins(log)
    assert (b["detect_ms"], b["stop_ms"], b["new_audio_ms"]) == (200, 210, 2100)


# --- 판정기의 음성 대화록 ------------------------------------------------------------------------------


def test_voice_transcript_shows_only_played_replies_with_cut_marks():
    log = [
        e(0, "voice.session", policy="C"),
        e(1000, "voice.segment", line=1, text="환불해 주세요", marker=None, speech_start_ms=1200, speech_end_ms=2500,
          feed_start_ms=1000),
        e(2510, "utterance.end", 0, utt_id="u1", speaker="customer", text="환불해 주세오", final=True),
        e(3000, "suggestion.created", 1, suggestion_id="s1", basis_version=1, text="버려진 답"),
        e(3000, "agent.say", 1, suggestion_id="s1", text="버려진 답"),
        e(3100, "result.discarded", 1, stage="voice", suggestion_id="s1", reason="version"),
        e(3200, "suggestion.created", 1, suggestion_id="s2", basis_version=1, text="재생된 답"),
        e(4000, "suggestion.exposed", 1, suggestion_id="s2", channel="voice", start_ms=4000, end_ms=None),
        e(5000, "voice.segment", 1, line=2, text="아니 해지만요", marker=MARK, speech_start_ms=5200, speech_end_ms=6000,
          feed_start_ms=5000),
        e(5420, "tts.playback_end", 1, suggestion_id="s2", exposed=True, start_ms=4000, end_ms=5420,
          interrupted="barge_in", played_ms=1420),
        e(6010, "utterance.end", 1, utt_id="u2", speaker="customer", text="아니 해지만요", final=True, marker=MARK),
    ]
    assert jd.is_voice(log) and not jd.is_voice(log[1:])
    text = jd.voice_transcript(log)
    assert text.splitlines() == [
        "[고객 1] 환불해 주세요",
        "  (인식) 환불해 주세오",
        "[상담 1] 재생된 답 (끼어들기로 1.4초 재생 뒤 끊김)",
        "[고객 2] (사건 #0 실행 표시) 아니 해지만요",
        "  (인식) 아니 해지만요",
    ]
    assert "버려진 답" not in text
    assert jd.JUDGE_VOICE_SYSTEM != jd.JUDGE_SYSTEM and "전화(음성)" in jd.JUDGE_VOICE_SYSTEM


# --- 고객 AI 음성 판과 실행기 도움 함수 ------------------------------------------------------------------


def test_voice_customer_prompt_keeps_chat_rules():
    voice = cust.VOICE_SYSTEM_PROMPT
    chat = cust.SYSTEM_PROMPT
    for old, new in cust.VOICE_REPLACEMENTS:
        assert old not in voice and new in voice
        chat = chat.replace(old, new)
    assert chat == voice  # 바꾼 문장 말고는 같다
    c = cust.GeminiCustomer({"id": "x"}, type("C", (), {"model": "m"})(), frozenset(), channel="voice")
    assert c.system == cust.VOICE_SYSTEM_PROMPT and c.prompt_version == cust.VOICE_PROMPT_VERSION


def test_tts_text_reads_only_the_otp_digit_by_digit():
    assert read_digits("0016") == "공공일육"
    said = tts_text("인증번호는 0016이고, 번호는 010-0000-0016, 금액은 10016원이에요.", {"0016"})
    assert said == "인증번호는 공공일육이고, 번호는 010-0000-0016, 금액은 10016원이에요."
    assert tts_text("01000000016 이에요", {"0016"}) == "01000000016 이에요"  # 붙여 쓴 번호
    assert tts_text("번호 0016요", set()) == "번호 0016요"


def test_heard_orders_lines_and_marks_cut_replies():
    pbs = [Playback("s1", 1, "환불 안내", 10.0, start_ms=3000.0, interrupted="barge_in", done=True),
           Playback("s2", 2, "해지 안내", 20.0, start_ms=7000.0, done=True),
           Playback("s3", 2, "버려진 답", 30.0)]
    conv = heard([(1000.0, "환불해 주세요"), (4000.0, "아니 해지만요")], pbs)
    assert conv == [{"speaker": "customer", "text": "환불해 주세요"}, {"speaker": "agent", "text": "환불 안내 [끊김]"},
                    {"speaker": "customer", "text": "아니 해지만요"}, {"speaker": "agent", "text": "해지 안내"}]
