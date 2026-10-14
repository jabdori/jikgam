"""상담 콘솔의 실시간 세션: 엔진 세션 하나와 그 이벤트를 화면으로 보내는 통로.

- 엔진 세션은 jikgam.engine.chat.make_session으로 만든다(정책 A = RAG 기준선, B = 엔진 + gemini-flash 판단,
  C = 엔진 + 조직 설정 판별 백엔드). 재생 실행기와 같은 세션이다.
- 이벤트 구독: 세션의 log 속성(EventLog)을 ObservedLog로 감싸, 엔진이 로그 줄을 쓸 때마다 화면 상태(ConsoleView)를
  갱신하고 구독자(SSE 연결)를 깨운다. 엔진 코드는 바꾸지 않고, 로그 파일에는 그대로 같은 줄이 남는다. 콜백 오류는 엔진에
  올리지 않는다.
- 고객 메시지는 받는 즉시 Session.handle 작업으로 시작한다. 앞 턴이 끝나기 전에 보내면 엔진이 빠른 정정으로 처리한다.
- 재생: data/replays의 파일을 줄마다 보낸다. 보통 줄은 앞 응답이 끝난 뒤 읽고 쓰는 시간(think_ms, 화면 시연용 값이며
  측정 조건이 아니다)을 두고, 조건 변경 표시 줄은 시나리오 실행기와 같이 직전 고객 발화 1,000ms 뒤에 엔진 응답을 기다리지
  않고 보낸다(docs/evaluation.md §3.3 보완). faults가 있는 줄은 보내기 직전에 모의 업무 API에 장애를 넣고 fault.injected를
  남긴다(chat.replay와 같다).
- 인계 버튼: 엔진 세션이면 Session.request_handoff(정책 규칙 1이 create_handoff를 부른다). 정책 A(RAG)에는 상담사 인계
  진입점이 없어 쓸 수 없다.
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from jikgam.engine import events as ev
from jikgam.engine.chat import inject_faults
from jikgam.sim.runner import FAST_DELAY_MS

from .view import ConsoleView, ui_message

THINK_BASE_MS = 1200.0  # 재생: 앞 응답을 읽고 다음 메시지를 쓰기 시작할 때까지(화면 시연용)
THINK_PER_CHAR_MS = 120.0
THINK_MAX_MS = 6000.0
COALESCE_S = 0.03  # 한 번에 몰려 오는 이벤트를 묶어 보낸다
PING_S = 15.0


def think_ms(text: str) -> float:
    return min(THINK_BASE_MS + THINK_PER_CHAR_MS * len(text), THINK_MAX_MS)


def load_replay(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for i, r in enumerate(rows, 1):
        if not isinstance(r.get("text"), str) or not r["text"].strip():
            raise ValueError(f"{path.name} {i}번째 줄에 text가 없다")
    return rows


class ObservedLog:
    """엔진 EventLog를 감싸 줄을 쓸 때마다 콜백을 부른다. 나머지 속성은 원래 로그로 넘긴다."""

    def __init__(self, inner: Any, on_event: Callable[[dict[str, Any]], None]):
        self._inner = inner
        self._on_event = on_event

    def emit(self, type_: str, version: int, *, t_ms: float | None = None, **fields: Any) -> dict[str, Any]:
        event = self._inner.emit(type_, version, t_ms=t_ms, **fields)
        try:
            self._on_event(event)
        except Exception:  # 화면 쪽 오류가 엔진 처리를 멈추지 않게 한다
            traceback.print_exc(file=sys.stderr)
        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LiveSession:
    def __init__(self, sid: str, policy: str, session: Any, titles: dict[str, str] | None = None,
                 bizapi_url: str | None = None, replay_name: str | None = None, deduction_reasons: frozenset[str] = frozenset()):
        self.sid = sid
        self.policy = policy
        self.session = session
        self.bizapi_url = bizapi_url
        self.replay_name = replay_name
        self.view = ConsoleView(titles, deduction_reasons)
        self.messages: list[dict[str, Any]] = []
        self.closed = False
        self._subs: set[asyncio.Event] = set()
        self._tasks: set[asyncio.Task] = set()
        self._replay: asyncio.Task | None = None
        self._n_utt = 0
        self.log_path: Path = session.log.path
        for e in ev.read_log(self.log_path):  # 세션을 만들 때 이미 쓴 줄(session.start)
            self._on_event(e)
        session.log = ObservedLog(session.log, self._on_event)

    @property
    def can_handoff(self) -> bool:
        return hasattr(self.session, "request_handoff")

    # --- 이벤트와 구독 --------------------------------------------------------------------------

    def _on_event(self, e: dict[str, Any]) -> None:
        self.view.apply(e)
        self.messages.append(ui_message(e))
        self._wake()

    def _console_message(self, kind: str, text: str) -> None:
        """엔진 이벤트가 아닌 콘솔 자체의 알림(입력 오류, 재생 시작·끝). 엔진 로그에는 남기지 않는다."""
        self.messages.append({"t_ms": ev.now_ms(), "type": "console", "kind": kind, "version": self.view.version,
                              "text": text})
        self._wake()

    def _wake(self) -> None:
        for s in self._subs:
            s.set()

    def state(self) -> dict[str, Any]:
        return {"session_id": self.sid, "policy": self.policy, "closed": self.closed, "can_handoff": self.can_handoff,
                "replay": self.replay_name, "replaying": self._replay is not None and not self._replay.done(),
                "log_path": str(self.log_path)}

    async def stream(self):
        """SSE 본문. 처음에 지금까지의 메시지와 상태를 보내고, 이후 새 메시지가 생길 때마다 묶어 보낸다."""
        wake = asyncio.Event()
        self._subs.add(wake)
        cursor = 0
        try:
            while True:
                batch = self.messages[cursor:]
                cursor += len(batch)
                payload = {"messages": batch, "view": self.view.snapshot(), "session": self.state()}
                yield f"event: update\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                if self.closed and cursor == len(self.messages):
                    yield "event: closed\ndata: {}\n\n"
                    return
                wake.clear()
                while not wake.is_set():
                    try:
                        await asyncio.wait_for(wake.wait(), PING_S)
                    except TimeoutError:
                        yield ": ping\n\n"
                await asyncio.sleep(COALESCE_S)
        finally:
            self._subs.discard(wake)

    # --- 입력 ------------------------------------------------------------------------------------

    def _spawn(self, coro, what: str) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self._console_message("error", f"{what} 오류: {type(t.exception()).__name__}: {t.exception()}")

        task.add_done_callback(done)
        return task

    def start(self) -> asyncio.Task:
        """세션 예열(Session.start)을 시작한다. 예열 중에 온 고객 발화는 엔진이 예열을 기다린다."""
        return self._spawn(self.session.start(), "예열")

    def say(self, text: str, marker: dict[str, Any] | None = None) -> asyncio.Task:
        """고객 메시지 하나를 엔진에 보낸다. 처리를 기다리지 않는다."""
        if self.closed:
            raise RuntimeError("닫힌 세션이다")
        text = text.strip()
        if not text:
            raise ValueError("메시지가 비어 있다")
        self._n_utt += 1
        event = {"type": "utterance", "utt_id": f"u{self._n_utt}", "speaker": "customer", "text": text, "final": True}
        if marker:
            event["marker"] = marker
        return self._spawn(self.session.handle(event), f"u{self._n_utt} 처리")

    def handoff(self) -> asyncio.Task:
        if not self.can_handoff:
            raise RuntimeError("이 정책(RAG 기준선)에는 상담사 인계 진입점이 없다")
        if self.closed:
            raise RuntimeError("닫힌 세션이다")
        return self._spawn(self.session.request_handoff(), "인계 요청")

    def start_replay(self, rows: list[dict[str, Any]], speed: float) -> None:
        if self._replay is not None and not self._replay.done():
            raise RuntimeError("이미 재생 중이다")
        self._replay = self._spawn(self._run_replay(rows, speed), "재생")

    async def _run_replay(self, rows: list[dict[str, Any]], speed: float) -> None:
        self._console_message("replay", f"재생 시작: {self.replay_name} {len(rows)}줄, "
                                        + ("대기 없음" if speed <= 0 else f"속도 {speed:g}배"))
        prev: asyncio.Task | None = None
        prev_sent = 0.0
        for row in rows:
            marker = row.get("marker") or None
            if prev is not None:
                if marker and marker.get("condition_change"):
                    # 빠른 정정: 직전 고객 발화 1,000ms 뒤, 엔진 응답을 기다리지 않는다(시나리오 실행기와 같은 방식)
                    while (remain := prev_sent + FAST_DELAY_MS - ev.now_ms()) > 0:
                        await asyncio.sleep(remain / 1000)
                else:
                    await asyncio.wait({prev})
                    if speed > 0:
                        await asyncio.sleep(think_ms(row["text"]) / speed / 1000)
            if self.closed:
                return
            if row.get("faults"):
                if self.bizapi_url is None:
                    raise ValueError("faults를 넣을 업무 API 주소가 없다")
                await asyncio.to_thread(inject_faults, self.bizapi_url, row["faults"])
                self.session.log.emit(ev.FAULT_INJECTED, self.session.version, before_utt=f"u{self._n_utt + 1}",
                                      faults=row["faults"])
            prev_sent = ev.now_ms()
            prev = self.say(row["text"], marker)
        if prev is not None:
            await asyncio.wait({prev})
        self._console_message("replay", "재생 끝")

    async def wait_idle(self) -> None:
        """진행 중인 입력 처리와 재생이 모두 끝날 때까지 기다린다(테스트와 종료에 쓴다)."""
        while self._tasks:
            await asyncio.wait(set(self._tasks))

    async def close(self) -> None:
        if self.closed:
            return
        if self._replay is not None and not self._replay.done():
            self._replay.cancel()
        await self.wait_idle()
        try:
            await self.session.aclose()
        finally:
            self.closed = True
            self._wake()
