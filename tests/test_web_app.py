"""웹 서버 경로(jikgam/web/app.py)와 실시간 세션(jikgam/web/live.py).

엔진 세션은 가짜 판별기·생성기로 만든다(외부 API를 부르지 않는다). 재생 테스트만 모의 업무 API(Go)를 띄운다.
"""

import asyncio
import dataclasses
import json
from pathlib import Path

import httpx
import pytest
from engine_fakes import EchoGenerator, ScriptedDecider
from test_engine_handoff_request import HandoffOnlyBiz

from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.org import load_org
from jikgam.engine.session import Session
from jikgam.web import live as live_mod
from jikgam.web.app import WebConfig, create_app, list_logs, resolve_log
from jikgam.web.live import LiveSession

ORG = load_org("org_a")
ROOT = Path(__file__).resolve().parents[1]


def fake_factory(biz=None):
    def make(policy, sid, log_dir):
        if policy == "A":
            raise SystemExit("테스트에서는 정책 A 세션을 만들지 않는다")
        return Session(ORG, sid, log_dir, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                       generator=EchoGenerator(), bizapi=biz() if biz else HandoffOnlyBiz())
    return make


def make_cfg(tmp_path, **kw) -> WebConfig:
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    return WebConfig(ORG, results_root=results, console_dir=tmp_path / "console", session_factory=fake_factory(), **kw)


async def client_for(cfg):
    app = create_app(cfg)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def sse_updates(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0] == "event: update":
            out.append(json.loads(lines[1].removeprefix("data: ")))
    return out


def test_pages_and_static_files_are_served(tmp_path):
    async def go():
        async with await client_for(make_cfg(tmp_path)) as c:
            r = await c.get("/")
            assert r.status_code in (302, 307) and r.headers["location"] == "/console"
            for path, title in (("/console", "상담 콘솔"), ("/timeline", "턴 상세 타임라인"),
                                ("/timeline/results/x/y.jsonl", "턴 상세 타임라인"), ("/dashboard", "평가 대시보드")):
                r = await c.get(path)
                assert r.status_code == 200 and title in r.text and 'lang="ko"' in r.text
            for f in ("common.css", "common.js", "console.js", "timeline.js", "dashboard.js"):
                assert (await c.get(f"/static/{f}")).status_code == 200
            cfg = (await c.get("/api/config")).json()
            assert cfg["org"] == "org_a" and {p["id"] for p in cfg["policies"]} == {"A", "B", "C"}
            assert "s20.jsonl" in cfg["replays"] and cfg["applies_threshold"] == ORG.applies_threshold
    asyncio.run(go())


def test_console_session_lifecycle_message_handoff_and_stream(tmp_path):
    cfg = make_cfg(tmp_path)

    async def go():
        async with await client_for(cfg) as c:
            r = await c.post("/api/sessions", json={"policy": "C"})
            assert r.status_code == 201
            sid = r.json()["session_id"]
            assert r.json()["timeline"].startswith("/timeline/console/") and r.json()["can_handoff"]
            assert (await c.post(f"/api/sessions/{sid}/messages", json={"text": "  "})).status_code == 400
            assert (await c.post(f"/api/sessions/{sid}/messages", json={"text": "가입 전에 환불 규정이 궁금해요"})).status_code == 202
            await cfg.sessions[sid].wait_idle()
            assert (await c.post(f"/api/sessions/{sid}/handoff")).status_code == 202
            await cfg.sessions[sid].wait_idle()
            closed = (await c.post(f"/api/sessions/{sid}/close")).json()
            assert closed["closed"] is True
            assert (await c.post(f"/api/sessions/{sid}/messages", json={"text": "또"})).status_code == 409
            # 닫힌 세션의 스트림은 지금까지의 상태를 보내고 closed로 끝난다
            body = (await c.get(f"/api/sessions/{sid}/stream")).text
            assert body.rstrip().endswith("event: closed\ndata: {}")
            updates = sse_updates(body)
            view = updates[-1]["view"]
            assert view["handoff"]["receipt"]["id"] == "HO-1" and view["current"]["rule"] == "handoff"
            kinds = [m["kind"] for m in updates[0]["messages"]]
            assert kinds[0] == "session" and "warmup" in kinds and "invalidated" not in kinds
            # 화면 메시지는 로그 줄마다 하나다(엔진 로그는 그대로 남는다)
            log = ev.read_log(cfg.console_dir / f"{sid}.jsonl")
            engine_msgs = [m for m in updates[0]["messages"] if m["type"] != "console"]
            assert [m["type"] for m in engine_msgs] == [e["type"] for e in log]
            # 콘솔 로그는 타임라인으로 열 수 있다
            tl = (await c.get("/api/timeline", params={"path": f"console/{sid}.jsonl"})).json()
            assert tl["turns"][0]["utt_id"] == "u1" and tl["meta"]["session"] == sid
            logs = (await c.get("/api/logs")).json()
            assert any(f["path"] == f"console/{sid}.jsonl" for g in logs for f in g["files"])
            assert (await c.post("/api/sessions/nope/messages", json={"text": "a"})).status_code == 404
            assert (await c.post("/api/sessions", json={"policy": "Z"})).status_code == 400
            assert (await c.post("/api/sessions", json={"policy": "C", "replay": "../x.jsonl"})).status_code == 400
            r = await c.post("/api/sessions", json={"policy": "A"})
            assert r.status_code == 400 and "정책 A" in r.json()["error"]
    asyncio.run(go())


def test_timeline_paths_are_restricted(tmp_path):
    cfg = make_cfg(tmp_path)
    run = cfg.results_root / "engine" / "scenarios" / "dev9-r1-C"
    run.mkdir(parents=True)
    log = [{"t_ms": 0.0, "type": "session.start", "version": 0, "session": "s"},
           {"t_ms": 1.0, "type": "utterance.end", "version": 0, "utt_id": "u1", "speaker": "customer", "text": "a"}]
    (run / "s.jsonl").write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")
    (run / "s.sim.jsonl").write_text('{"type": "sim.end"}\n', encoding="utf-8")
    hold = cfg.results_root / "engine" / "scenarios" / "hold-r1-C"
    hold.mkdir(parents=True)
    (hold / "summary.json").write_text(json.dumps({"scenario_file": "data/scenarios/org_a_holdout.jsonl"}),
                                       encoding="utf-8")
    (hold / "h.jsonl").write_text(json.dumps(log[0]) + "\n", encoding="utf-8")
    (tmp_path / "outside.jsonl").write_text(json.dumps(log[0]) + "\n", encoding="utf-8")

    async def go():
        async with await client_for(cfg) as c:
            ok = await c.get("/api/timeline", params={"path": "results/engine/scenarios/dev9-r1-C/s.jsonl"})
            assert ok.status_code == 200 and ok.json()["turns"][0]["utt_id"] == "u1"
            for bad in ("results/../outside.jsonl", "results/engine/scenarios/dev9-r1-C/s.sim.jsonl",
                        "results/engine/scenarios/hold-r1-C/h.jsonl", "nope/s.jsonl"):
                assert (await c.get("/api/timeline", params={"path": bad})).status_code == 403, bad
            assert (await c.get("/api/timeline", params={"path": "results/missing.jsonl"})).status_code == 404
            groups = (await c.get("/api/logs")).json()
            by_dir = {g["dir"]: g for g in groups}
            assert by_dir["engine/scenarios/hold-r1-C"]["locked"] and by_dir["engine/scenarios/hold-r1-C"]["files"] == []
            assert [f["name"] for f in by_dir["engine/scenarios/dev9-r1-C"]["files"]] == ["s.jsonl"]
            dash = await c.get("/api/dashboard")
            assert dash.status_code == 200 and "운영 성과가 아니다" in dash.json()["note"]
    asyncio.run(go())
    # 실행기가 summary.json을 다시 쓰는 중이라 읽을 수 없으면 보류용인지 모르므로 잠근다
    (run / "summary.json").write_text("", encoding="utf-8")
    with pytest.raises(PermissionError):
        resolve_log(cfg, "results/engine/scenarios/dev9-r1-C/s.jsonl")
    assert {g["dir"]: g for g in list_logs(cfg)}["engine/scenarios/dev9-r1-C"]["locked"]
    cfg.allow_holdout = True
    assert resolve_log(cfg, "results/engine/scenarios/hold-r1-C/h.jsonl").name == "h.jsonl"


# --- 실시간 세션 ---------------------------------------------------------------------------------


class GatedSession:
    """재생 간격을 보려는 가짜 세션. handle은 release가 켜질 때까지 끝나지 않는다."""

    def __init__(self, log_dir: Path):
        self.log = ev.EventLog(log_dir / "g.jsonl", "g")
        self.log.emit(ev.SESSION_START, 0)
        self.version = 0
        self.release = asyncio.Event()

    async def start(self):
        pass

    async def handle(self, event):
        self.version += 1
        self.log.emit(ev.UTTERANCE_END, self.version - 1, utt_id=event["utt_id"], speaker="customer",
                      text=event["text"], marker=event.get("marker"))
        await self.release.wait()
        return []

    async def aclose(self):
        self.log.close()


def test_replay_sends_condition_change_without_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mod, "FAST_DELAY_MS", 50.0)
    rows = [{"text": "첫 메시지"}, {"text": "정정", "marker": {"condition_change": True, "event": "x#0"}},
            {"text": "보통 메시지"}]

    async def go():
        s = GatedSession(tmp_path)
        ls = LiveSession("g", "C", s)
        ls.start_replay(rows, speed=0)
        await asyncio.sleep(0.3)  # 첫 메시지 처리는 끝나지 않았다
        sent = [t["text"] for t in ls.view.transcript]
        assert sent == ["첫 메시지", "정정"]  # 조건 변경 줄은 응답을 기다리지 않는다
        s.release.set()
        await ls.wait_idle()
        assert [t["text"] for t in ls.view.transcript] == ["첫 메시지", "정정", "보통 메시지"]
        kinds = [m["kind"] for m in ls.messages if m["type"] == "console"]
        assert kinds == ["replay", "replay"]
        ends = [e for e in ev.read_log(s.log.path) if e["type"] == ev.UTTERANCE_END]
        assert 40 <= ends[1]["t_ms"] - ends[0]["t_ms"] < 250
        await ls.close()
        with pytest.raises(RuntimeError):
            ls.say("닫힌 뒤")

    asyncio.run(go())


def test_stream_does_not_miss_events_emitted_while_sending(tmp_path):
    """SSE 본문을 보내는 동안(전송이 밀려 생성기가 yield에 멈춘 동안) 생긴 이벤트도 다음 묶음으로 나간다."""

    async def go():
        s = GatedSession(tmp_path)
        ls = LiveSession("g", "C", s)
        gen = ls.stream()
        first = await gen.__anext__()  # 생성기는 첫 묶음을 내고 멈춰 있다
        assert first.startswith("event: update")
        s.log.emit(ev.UTTERANCE_END, 0, utt_id="u1", speaker="customer", text="보내는 동안 온 발화")
        nxt = await asyncio.wait_for(gen.__anext__(), 2.0)
        assert "보내는 동안 온 발화" in nxt
        await gen.aclose()
        await ls.close()

    asyncio.run(go())


def test_observed_log_errors_do_not_reach_engine(tmp_path):
    async def go():
        s = Session(ORG, "t-obs", tmp_path, decision_client=ScriptedDecider(), generator=EchoGenerator(),
                    bizapi=HandoffOnlyBiz())
        ls = LiveSession("t-obs", "C", s)
        ls.view.apply = lambda e: (_ for _ in ()).throw(RuntimeError("화면 오류"))  # 화면 쪽이 깨져도
        ls.say("가입 전에 환불 규정이 궁금해요")
        await ls.wait_idle()
        await ls.close()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-obs.jsonl")
    assert any(e["type"] == ev.AGENT_SAY for e in log)  # 엔진은 끝까지 진행했다


def test_replay_through_app_against_mock_bizapi(bizapi, tmp_path, monkeypatch):
    """재생 세션: 모의 업무 API를 비우고 s20을 끝까지 보낸다. 판별·생성은 가짜다."""
    monkeypatch.setattr(live_mod, "FAST_DELAY_MS", 20.0)
    org = dataclasses.replace(ORG, bizapi_url=bizapi)

    def make(policy, sid, log_dir):
        return Session(org, sid, log_dir, decision_client=ScriptedDecider(intents=[("해지", "cancel")]),
                       generator=EchoGenerator(), bizapi=BizApiClient(bizapi, sid))

    cfg = WebConfig(org, results_root=tmp_path / "results", console_dir=tmp_path / "console", session_factory=make)
    rows = [json.loads(x) for x in (ROOT / "data/replays/s20.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    async def go():
        async with await client_for(cfg) as c:
            r = await c.post("/api/sessions", json={"policy": "C", "replay": "s20.jsonl", "speed": 0})
            assert r.status_code == 201
            ls = cfg.sessions[r.json()["session_id"]]
            await ls.wait_idle()
            snap = ls.view.snapshot()
            customer = [t["text"] for t in snap["transcript"] if t["speaker"] == "customer"]
            assert customer == [row["text"] for row in rows]
            assert snap["facts"]["identity"]["verified"]  # 실제 모의 업무 API로 본인 확인
            await ls.close()
    asyncio.run(go())
