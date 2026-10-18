"""관리 콘솔 음성 시험(/admin/voice-test): 접근 제한, 시작 전 안내, 시험용 고객, 통화 중·끝난 통화의 화면.

브라우저 WebRTC와 유료 음성 호출은 부르지 않는다. 통화 중 화면은 세션 로그를 직접 써서 그린다.
"""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jikgam.engine import events as ev
from jikgam.engine.org import ROOT
from jikgam.web import auth
from jikgam.web.admin import voicetest as screen
from jikgam.web.admin.common import Ctx
from jikgam.web.app import WebConfig, create_app
from jikgam.web.data import voicetest as vt

A = auth.Account("a", "pw-a", "admin")
T = auth.Account("t", "pw-t", "tenant", "org_a")


def hdr(acct):
    return {"Authorization": auth.basic_header(acct.name, acct.password)}


def client(host="127.0.0.1"):
    return TestClient(create_app(WebConfig(root=ROOT, accounts={"a": A, "t": T}, clock=lambda: 0.0)), client=(host, 50000))


def test_only_admins_on_this_pc_can_open_it(monkeypatch):
    monkeypatch.setattr(vt, "bizapi_up", lambda url: False)
    assert client().get("/admin/voice-test", headers=hdr(A)).status_code == 200
    assert client("10.0.0.7").get("/admin/voice-test", headers=hdr(A)).status_code == 403
    assert client().get("/admin/voice-test", headers=hdr(T)).status_code == 403
    r = client().post("/admin/voice-test/offer", headers={**hdr(A), "origin": "http://evil.example"}, json={"sdp": "x", "type": "offer"})
    assert r.status_code == 403


def test_start_is_refused_with_instructions_when_the_business_api_is_down(monkeypatch):
    monkeypatch.setattr(vt, "bizapi_up", lambda url: False)
    c = client()
    page = c.get("/admin/voice-test", headers=hdr(A)).text
    assert "data-voice-test" in page and "cd bizapi &amp;&amp; go run ./cmd/bizapi" in page
    assert '<button class="btn btn-primary" type="button" data-voice-start disabled>' in page
    assert "분당 예상 비용" in page and "echoCancellation" not in page  # 마이크 설정은 스크립트에만
    r = c.post("/admin/voice-test/offer", headers=hdr(A), json={"sdp": "v=0", "type": "offer"})
    assert r.status_code == 409 and "모의 업무 API" in r.json()["error"]
    assert c.post("/admin/voice-test/offer", headers=hdr(A), json={}).status_code == 400


def test_customer_card_reads_the_mock_business_data(monkeypatch):
    monkeypatch.setattr(vt, "bizapi_up", lambda url: True)
    page = client().get("/admin/voice-test?customer=C002", headers=hdr(A)).text
    c2 = next(c for c in vt.customers(ROOT) if c["id"] == "C002")
    assert c2["name"] in page and c2["phone"] in page and c2["otp"] == c2["phone"][-4:] and c2["otp"] in page
    assert "이렇게 말해 보세요" in page and 'data-voice-start disabled' not in page


def test_cost_per_minute_comes_from_a_recorded_voice_run():
    cost = vt.cost_per_min(ROOT)
    assert cost and cost["usd_per_min"] > 0 and cost["source"].startswith("results/voice/")


def write_call(root: Path, call_id: str, events: list[dict]) -> vt.TestCall:
    run_dir = root / vt.LIVE_DIR / call_id
    run_dir.mkdir(parents=True)
    sid = f"voice-test-{call_id}"
    (run_dir / f"{sid}.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    return vt.TestCall(call_id, sid, run_dir)


def session_events(t0: float) -> list[dict]:
    return [
        {"type": "session.start", "t_ms": t0, "session": "s", "org": "org_a"},
        {"type": "voice.session", "t_ms": t0},
        {"type": "utterance.end", "t_ms": t0 + 1000, "speaker": "customer", "text": "다음 결제가 언제예요", "utt_id": "u1"},
        {"type": "stt.final", "t_ms": t0 + 1100, "utt_id": "u1", "text": "다음 결제가 언제예요"},
        {"type": "version.bumped", "t_ms": t0 + 1101, "version": 1, "from": 0, "to": 1, "cause": "utterance"},
        {"type": "decision.response", "t_ms": t0 + 1300, "version": 1, "model": "jev-1.13.0", "latency_ms": 212.0,
         "answers": {"intent": {"label": "billing_inquiry", "p": 0.97}, "condition_changed": {"p_true": 0.08}}},
        {"type": "evidence.updated", "t_ms": t0 + 1310, "version": 1, "applies": {"A-04": 0.62, "A-02": 0.37, "A-15": 0.18},
         "articles": ["A-04"], "sufficient": False, "action": "ask"},
        {"type": "tool.result", "t_ms": t0 + 1400, "version": 1, "tool": "get_customer", "ok": True, "latency_ms": 12.0},
        {"type": "suggestion.created", "t_ms": t0 + 1500, "suggestion_id": "x-s1", "text": "본인 확인이 필요합니다",
         "basis_version": 1, "version": 1},
        {"type": "suggestion.exposed", "t_ms": t0 + 1600, "suggestion_id": "x-s1", "channel": "voice", "start_ms": t0 + 1600,
         "version": 1},
        {"type": "voice.barge_in", "t_ms": t0 + 2400, "suggestion_id": "x-s1", "started": True, "played_ms": 800.0,
         "trigger": "vad", "version": 1},
        {"type": "tts.playback_end", "t_ms": t0 + 2400, "suggestion_id": "x-s1", "start_ms": t0 + 1600, "end_ms": t0 + 2400,
         "played_ms": 800.0, "interrupted": "barge_in", "version": 1},
        {"type": "result.discarded", "t_ms": t0 + 2500, "version": 1, "stage": "voice", "reason": "version"},
        {"type": "stt.partial", "t_ms": t0 + 2600, "utt_id": "u2", "text": "아니요 그냥 해지"},
    ]


def ctx(query):
    return Ctx(ROOT, {}, False, query, "a")


def test_running_call_shows_interim_caption_and_decision_log(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "bizapi_up", lambda url: True)
    t0 = ev.now_ms()
    tests = vt.VoiceTests(tmp_path)
    call = write_call(tmp_path, "20991231-000000", session_events(t0))
    call.status, call.started_ms = "통화 중", t0
    tests.calls[call.id] = call
    html = str(screen.page(ctx({"call": call.id}), tests))
    assert 'data-live="/admin/voice-test?call=20991231-000000' in html  # 통화 중에는 스스로 갱신한다
    assert "고객 · 말하는 중" in html and "아니요 그냥 해지" in html  # 아직 확정되지 않은 말
    for kind in ("대화 버전", "판별", "근거", "업무 API", "끼어들기", "폐기"):
        assert f"{kind}</span></td>" in html, kind
    assert "결제 문의" in html and "A-04" in html and "get_customer" in html
    assert "data-voice-start disabled" in html and "data-voice-stop>" in html  # 통화 중이라 종료만 누를 수 있다


def test_ended_call_links_to_replay_and_stops_refreshing(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "bizapi_up", lambda url: True)
    events = session_events(1000.0)[:-1]
    call = write_call(tmp_path, "20991231-000001", events)
    tests = vt.VoiceTests(tmp_path)  # 서버를 다시 띄운 뒤에도 기록에서 찾는다
    found = tests.find(call.id)
    assert found is not None and found.status == "끝남" and found.session_id == call.session_id
    html = str(screen.page(ctx({"call": call.id}), tests))
    assert "data-live" not in html and "리플레이에서 열기" in html
    assert f"/admin/replay?s=live/{call.id}/{call.session_id}" in html
    assert tests.find("../escape") is None
