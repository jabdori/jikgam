"""관리 콘솔 음성 시험: 브라우저 마이크 → 음성 파이프라인 → 브라우저 스피커(Pipecat SmallWebRTC).

엔진, VAD, STT, TTS, 응답 재생 규칙, 로그 형식은 음성 재생 실행기와 같다. 세션은 jikgam.voice.replay.make_voice_session과
voice_options로, 파이프라인은 jikgam.voice.pipeline.build_voice_pipeline으로 만든다. 바뀌는 것은 전송 하나다. 브라우저는
에코 제거를 켠 마이크를 보낸다(static/console.js의 data-voice-test).

- 한 번에 한 통화만 연다. settings.VOICE_TEST_MAX_S가 지나거나 브라우저 연결이 끊기면 끝낸다.
- 기록: results/voice/live/<시험 id>/<세션>.jsonl(기존 음성 로그와 같은 형식)과 <세션>.wav(스테레오: 왼쪽 고객, 오른쪽 AI).
  녹음 시작 시각은 voice.recording에 남긴다. 평가 결과 폴더와 섞지 않고 평가 수치에 쓰지 않는다.
- 끝난 통화는 관리 콘솔 통화 리플레이(results/voice 아래를 찾는다)에서 녹음과 함께 열린다.
- API 키는 .env에서만 읽는다(jikgam.decision.registry.secrets, 음성 파이프라인과 같다).
"""

import asyncio
import hashlib
import json
import os
import socket
import wave
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jikgam.engine import events as ev
from jikgam.engine.org import load_org

from .. import settings

LIVE_DIR = "results/voice/live"
CUSTOMERS = "domain/org_a/customers.json"
RUNNING = ("연결 중", "통화 중", "끝내는 중")


class VoiceTestError(Exception):
    """시작할 수 없는 상태(모의 업무 API 꺼짐, WebRTC 모듈 없음 등). 글은 화면에 그대로 보인다."""


# --- 시작 전 확인 ------------------------------------------------------------------------------------


def bizapi_url() -> str:
    """엔진이 부를 모의 업무 API 주소. 환경 변수가 조직 설정보다 우선한다(docs/engine.md §9)."""
    return os.environ.get("JIKGAM_BIZAPI_URL") or load_org("org_a").bizapi_url


def bizapi_up(url: str) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=0.3):
            return True
    except OSError:
        return False


def webrtc_available() -> bool:
    try:
        import aiortc  # noqa: F401
    except ImportError:
        return False
    return True


def cost_per_min(root: Path) -> dict[str, Any] | None:
    """기존 음성 기록(정책 C)의 분당 비용: 세션별 엔진 비용과 STT·TTS 추정의 합 ÷ STT 연결 시간(분). 청구 금액이 아니다."""
    path = root / settings.VOICE_TEST_COST_RUN / "summary.json"
    try:
        sessions = json.loads(path.read_text(encoding="utf-8")).get("sessions") or []
    except (OSError, ValueError):
        return None
    usd = sum(sum(v for v in (s.get("cost_usd") or {}).values() if isinstance(v, (int, float))) for s in sessions)
    minutes = sum(s.get("stt_stream_min") or 0 for s in sessions)
    if not minutes:
        return None
    return {"usd_per_min": usd / minutes, "minutes": minutes, "sessions": len(sessions), "source": settings.VOICE_TEST_COST_RUN}


def customers(root: Path) -> list[dict[str, Any]]:
    """시험자가 연기할 가상 고객(모의 업무 API 자료). 인증번호는 모의 업무 API의 규칙대로 휴대폰 번호 끝 4자리다."""
    try:
        data = json.loads((root / CUSTOMERS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for c in data.get("customers", []):
        digits = "".join(ch for ch in c.get("phone", "") if ch.isdigit())
        subs = c.get("subscriptions") or []
        pays = sorted(c.get("payments") or [], key=lambda p: p.get("date", ""))
        out.append({"id": c["id"], "name": c.get("name"), "phone": c.get("phone"), "otp": digits[-4:] if digits else None,
                    "status": c.get("status"), "subscription": subs[0] if subs else None, "payment": pays[-1] if pays else None})
    return out


def seconds(ms: float | None) -> float | None:
    """엔진 시계 밀리초 간격 → 초(화면 표시용)."""
    return None if ms is None else ms / 1000


# --- 통화 -------------------------------------------------------------------------------------------


@dataclass
class TestCall:
    id: str
    session_id: str
    run_dir: Path
    customer: str | None = None
    status: str = "연결 중"
    started_ms: float | None = None   # 엔진 시계. 녹음 시작과 같다
    ended_ms: float | None = None
    reason: str | None = None          # 끝난 이유
    session: Any = field(default=None, repr=False)
    worker: Any = field(default=None, repr=False)
    task: Any = field(default=None, repr=False)
    timer: Any = field(default=None, repr=False)
    connection: Any = field(default=None, repr=False)
    recorder: Any = field(default=None, repr=False)

    @property
    def log_path(self) -> Path:
        return self.run_dir / f"{self.session_id}.jsonl"

    @property
    def running(self) -> bool:
        return self.status in RUNNING


def read_events(path: Path) -> list[dict[str, Any]]:
    """쓰는 중인 세션 로그도 읽는다(마지막 줄이 덜 쓰였으면 뺀다)."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class VoiceTests:
    def __init__(self, root: Path):
        self.root = root
        self.calls: dict[str, TestCall] = {}
        self.lock = asyncio.Lock()

    # 끝난 통화는 서버를 다시 띄운 뒤에도 기록에서 찾는다
    def find(self, call_id: str | None) -> TestCall | None:
        if not call_id:
            return None
        if call_id in self.calls:
            return self.calls[call_id]
        run_dir = self.root / LIVE_DIR / call_id
        if call_id != run_dir.name or not run_dir.is_dir():
            return None
        log = next(iter(sorted(run_dir.glob("*.jsonl"))), None)
        if log is None:
            return None
        return TestCall(call_id, log.stem, run_dir, status="끝남")

    def current(self) -> TestCall | None:
        return next((c for c in self.calls.values() if c.running), None)

    async def start(self, sdp: str, sdp_type: str, customer: str | None) -> dict[str, Any]:
        """브라우저의 WebRTC 제안을 받아 통화를 열고 응답(SDP)을 돌려준다. 파이프라인은 응답을 돌려준 뒤 연결되면 시작한다."""
        if not webrtc_available():
            raise VoiceTestError("브라우저 음성 연결 모듈(aiortc)이 없습니다. uv sync로 기본 의존성을 설치하세요.")
        url = bizapi_url()
        if not bizapi_up(url):
            raise VoiceTestError(f"모의 업무 API({url})가 꺼져 있습니다.")
        async with self.lock:
            running = self.current()
            if running is not None:
                await self.stop(running.id, "새 시험을 시작해 끝냈다")
            return await self._open(sdp, sdp_type, customer, url)

    async def _open(self, sdp: str, sdp_type: str, customer: str | None, url: str) -> dict[str, Any]:
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
        from pipecat.workers.runner import WorkerRunner

        from jikgam.voice import bridge as vb
        from jikgam.voice.config import load_voice_config
        from jikgam.voice.pipeline import IN_RATE, OUT_RATE, build_voice_pipeline
        from jikgam.voice.recorder import StereoRecorder
        from jikgam.voice.replay import make_voice_session, reset_bizapi, voice_options

        reset_bizapi(url)
        os.environ["JIKGAM_BIZAPI_URL"] = url
        cfg = load_voice_config()
        call_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = self.root / LIVE_DIR / call_id
        run_dir.mkdir(parents=True, exist_ok=True)
        call = TestCall(call_id, f"voice-test-{call_id}", run_dir, customer)
        self.calls[call_id] = call
        policy = settings.VOICE_TEST_POLICY
        call.session = make_voice_session(policy, load_org("org_a"), call.session_id, run_dir)
        await call.session.start()
        call.connection = SmallWebRTCConnection()
        await call.connection.initialize(sdp=sdp, type=sdp_type)
        transport = SmallWebRTCTransport(webrtc_connection=call.connection, params=TransportParams(
            audio_in_enabled=True, audio_in_sample_rate=IN_RATE, audio_out_enabled=True, audio_out_sample_rate=OUT_RATE))
        call.recorder = StereoRecorder(OUT_RATE)  # 왼쪽 고객(입력 바로 뒤), 오른쪽 AI(출력 바로 뒤)
        vp = build_voice_pipeline(call.session, cfg, transport, after_input=[call.recorder.tap("left")],
                                  after_output=[call.recorder.tap("right")], **voice_options(policy))
        call.worker = vp.worker
        started = asyncio.Event()

        @vp.worker.event_handler("on_pipeline_started")
        async def _started(_worker, _frame):
            started.set()

        @transport.event_handler("on_client_disconnected")
        async def _gone(_transport, _client):
            asyncio.create_task(self.stop(call.id, "브라우저 연결이 끊겼다"))

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(vp.worker)
        call.task = asyncio.create_task(runner.run())
        answer = call.connection.get_answer()

        async def _after_start():
            try:
                await asyncio.wait_for(started.wait(), 30)
            except TimeoutError:
                await self.stop(call.id, "음성 연결이 30초 안에 열리지 않았다")
                return
            call.session.log.emit(vb.VOICE_SESSION, call.session.version, **cfg.log(), pipecat=pkg_version("pipecat-ai"),
                                  google_genai=pkg_version("google-genai"), policy=policy, stale_guard=vp.bridge.stale_guard,
                                  provisional=vp.bridge.provisional, decision_backend=getattr(call.session, "backend", None),
                                  input="browser-webrtc", echo_cancellation=True, max_s=settings.VOICE_TEST_MAX_S,
                                  customer=customer, purpose="관리 콘솔 음성 시험(평가 아님)",
                                  clock="engine monotonic ms; t_end_ms = VAD stop decision (wall→engine) - stop_secs")
            call.started_ms = call.recorder.start()
            call.status = "통화 중"
            call.timer = asyncio.create_task(self._limit(call.id))

        asyncio.create_task(_after_start())
        return {**answer, "call": call_id}

    async def _limit(self, call_id: str) -> None:
        await asyncio.sleep(settings.VOICE_TEST_MAX_S)
        await self.stop(call_id, "최대 통화 시간이 지났다")

    async def stop(self, call_id: str, reason: str) -> TestCall | None:
        call = self.calls.get(call_id)
        if call is None or not call.running or call.status == "끝내는 중":
            return call
        call.status = "끝내는 중"
        call.reason = reason
        if call.timer is not None and call.timer is not asyncio.current_task():
            call.timer.cancel()
        try:
            audio = call.recorder.stop() if call.recorder is not None else None
            if call.worker is not None:
                await call.worker.end()
                try:
                    await asyncio.wait_for(call.task, 30)
                except TimeoutError:
                    await call.worker.cancel()
            if audio and call.started_ms is not None:
                self._save_recording(call, audio, call.recorder.rate)
        finally:
            call.ended_ms = ev.now_ms()
            if call.session is not None:
                await call.session.aclose()
            if call.connection is not None:
                await call.connection.disconnect()
            call.status = "끝남"
        return call

    def _save_recording(self, call: TestCall, audio: bytes, rate: int) -> None:
        from jikgam.voice.metrics import RECORDING

        channels = 2
        path = call.run_dir / f"{call.session_id}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(audio)
        frames = len(audio) // (2 * channels)
        call.session.log.emit(RECORDING, call.session.version, file=path.relative_to(self.root).as_posix(),
                              t0_ms=call.started_ms, sha256_16=hashlib.sha256(path.read_bytes()).hexdigest()[:16],
                              duration_ms=round(frames / rate * 1000, 1), sample_rate=rate,
                              channels={"left": "customer", "right": "agent"})
