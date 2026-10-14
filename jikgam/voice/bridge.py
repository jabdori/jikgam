"""엔진 연결 FrameProcessor: STT 자막을 엔진 입력 이벤트로 바꾸고, 엔진 응답을 TTS로 보낸다.

파이프라인: 입력 → VAD → STT(VERBATIM) → EngineBridge → TTS → 출력. 엔진(jikgam.engine.session.Session)은 고치지 않고
`handle(event)`, `version`, `log`만 쓴다. 음성 계층의 기록은 같은 세션 로그에 남긴다(docs/voice.md §3). 엔진 세션은
`channel="voice"`로 만든다. 그러면 엔진은 응답을 낼 때 노출 줄을 남기지 않고, 노출은 이 계층이 재생 시작에 남긴다(아래).
채널이 다른 세션을 넘기면 노출이 두 채널로 남으므로 받지 않는다.

입력 방향
- 미확정 자막(InterimTranscriptionFrame) → `{"type": "utterance", "final": false}`. 엔진은 stt.partial만 남기고 버전을
  올리지 않는다(docs/engine.md §3.1). 발화 ID는 이 계층이 매긴다(u1, u2, …). 확정 자막이 오기 전의 미확정 자막은 모두
  다음 확정 자막과 같은 ID다.
- 확정 자막(TranscriptionFrame) → `{"final": true, "t_end_ms": 발화 종료}`. 처리는 별도 asyncio 작업으로 돌린다. 앞
  발화를 처리하는 중에도 새 발화를 받아야 엔진이 버전을 올리고 앞 작업을 취소할 수 있기 때문이다(빠른 정정).
- t_end_ms(오디오상 발화 종료, 엔진 단조 시계): VAD가 발화 끝을 판정한 시각 − stop_secs. VAD 프레임의 timestamp는
  time.time()(벽시계, 초)이므로 받은 순간의 두 시계 차로 엔진 시계로 옮긴다(wall_to_engine_ms). 입력 오디오가 실시간으로
  들어오고 VAD가 받은 즉시 판정하므로, 판정 시각은 그 판정을 낸 오디오 조각이 들어온 시각과 같다고 본다. VAD 끝 판정
  하나는 확정 발화 하나에만 쓴다. 쓸 끝 판정이 없으면(STT가 VAD 끝점보다 먼저 확정) 확정 자막을 받은 시각을 쓰고
  t_end_source="final_received"로 표시한다. 그 경우 발화 종료가 실제보다 늦게 잡혀 지연이 짧게 계산된다.
- 발화 시작 추정값은 VAD 시작 판정 시각 − start_secs다.

출력 방향
- 엔진 handle()이 돌려준 agent.say를 한 번에 하나씩 TTSSpeakFrame으로 보낸다. 재생이 시작되면(출력 전송이 올려 보낸
  BotStartedSpeakingFrame) suggestion.exposed(channel=voice, start_ms)를 남긴다(docs/evaluation.md §3.3의 "TTS 재생
  시작"). 재생이 끝나면 tts.playback_end(end_ms, interrupted)를 남긴다.
- 대화 버전 규칙(CLAUDE.md "버전이 바뀐 결과는 폐기")을 음성 출력에도 적용한다. TTS를 요청하기 직전에 세션 버전이 응답의
  기준 버전과 다르면 말하지 않고 result.discarded(stage=voice)를 남긴다. 재생 시작 시점에 버전이 바뀌었으면 노출은
  남기고 곧바로 끊는다(interrupted=version).
- 고객 차례에는 새 응답을 시작하지 않는다: VAD가 말하는 중이거나, 말이 끝났는데 그 말의 확정 자막이 아직 없으면(최대
  final_wait_ms) 응답을 붙잡아 둔다(voice.reply_held). 확정 자막이 오면 엔진이 버전을 올리므로 붙잡은 응답은 위 규칙으로
  버려진다. 고객이 말을 끝낸 뒤 확정 자막이 오기 전에 지난 응답이 재생되는 것(무효 노출)을 막는다.
- 끼어들기: 응답을 요청했거나 재생 중일 때 VAD가 발화 시작을 판정하거나 미확정 자막이 오면(먼저 오는 쪽) Pipecat
  끼어들기(broadcast_interruption)로 TTS 합성과 출력 버퍼를 비우고 voice.barge_in(trigger=vad|transcript)을 남긴다.
  "네"처럼 짧은 말은 VAD 시작 판정(start_secs 연속 음성)보다 자막이 먼저 온다. Pipecat의 기본 사용자 차례 시작 규칙도
  VAD와 자막 둘 다를 쓴다. 버전은 엔진이 확정 발화를 받을 때 올린다.

미확정 자막 선판단(정책 D)은 하지 않는다. 엔진에 넘기는 미확정 자막은 기록용이며 그 결과가 말해지는 경로가 없다.

provisional(기본 False, 정책 D, docs/evaluation-voice.md §9.2):
- 미확정 자막이 바뀔 때마다 세션의 prejudge(발화 ID, 자막)를 불러 첫 판별을 미리 돌려 둔다(세션 상태는 바뀌지 않는다).
- VAD가 말 끝을 판정했을 때 이 발화의 미확정 자막이 있으면 확정 자막을 기다리지 않고 그 자막을 확정 발화로 엔진에 넘긴다
  (잠정 확정, voice.provisional). 그 발화는 확정 자막을 기다리지 않으므로 응답도 붙잡지 않는다. 자막에 숫자(아라비아 숫자,
  한글 숫자 읽기 세 글자 이상)가 있으면 잠정 확정하지 않고 확정 자막을 기다린다.
- 확정 자막이 오면 먼저 넘긴 잠정 문장과 비교한다(공백·문장부호를 뺀 글자). 같으면 엔진에 다시 넘기지 않는다. 다르면 확정
  자막을 새 발화 ID로 엔진에 넘긴다(버전이 올라 잠정 문장으로 하던 작업이 취소되고 지난 답은 위 규칙으로 버려지거나 끊긴다).
- 잠정 확정 뒤 고객이 다시 말을 시작하기 전에 늦게 온 미확정 자막은 끝난 말의 것이므로 새 발화로 보지 않고, 끼어들기나
  확정 자막 기다리기에도 쓰지 않는다(기록만 한다).

stale_guard(기본 True): 위의 대화 버전 규칙 두 가지(요청 직전 폐기, 재생 시작 때 끊기)와 확정 자막 기다리기는 엔진 구조의
일부다. 정책 A(개선형 RAG, 대화 버전이 없다)를 음성으로 돌릴 때는 stale_guard=False로 만들어 이 셋을 끈다. 그래도 한 번에
하나씩 재생, VAD가 말하는 중에는 시작하지 않기, 끼어들기는 그대로다(docs/evaluation-voice.md §2.1).
"""

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, CancelFrame, EndFrame, ErrorFrame,
                                   Frame, InterimTranscriptionFrame, StartFrame, TranscriptionFrame, TTSSpeakFrame,
                                   VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from jikgam.engine import events as ev
from jikgam.engine.events import now_ms

CHANNEL = "voice"

# 음성 계층이 세션 로그에 더하는 유형(docs/voice.md §3)
VOICE_SESSION = "voice.session"
VAD_START = "vad.start"
VAD_STOP = "vad.stop"
REPLY_HELD = "voice.reply_held"
TTS_REQUEST = "tts.request"
PLAYBACK_END = "tts.playback_end"
BARGE_IN = "voice.barge_in"
VOICE_ERROR = "voice.error"
PROVISIONAL = "voice.provisional"  # 정책 D: 미확정 자막으로 잠정 확정해 엔진에 넘겼다
PROVISIONAL_DROPPED = "voice.provisional_unconfirmed"  # 잠정 확정한 발화의 확정 자막이 끝내 오지 않았다
_DIGITS = re.compile(r"\d|[공영일이삼사오육칠팔구]{3,}")
_NORM = re.compile(r"[\s.,!?~·…\"'“”‘’]")


def has_digits(text: str) -> bool:
    """번호처럼 읽히는 숫자가 있는가(잠정 확정하지 않는다)."""
    return bool(_DIGITS.search(text))


def same_text(a: str, b: str) -> bool:
    """공백과 문장부호를 빼면 같은 문장인가."""
    return _NORM.sub("", a) == _NORM.sub("", b)
PLAYBACK_END_TIMEOUT_MS = 180_000  # 재생이 끝났다는 신호가 끝내 오지 않을 때의 안전장치


class SessionLike(Protocol):
    log: Any

    @property
    def version(self) -> int: ...

    async def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]: ...


def wall_to_engine_ms(wall_s: float, *, now_wall_s: float | None = None, now_engine_ms: float | None = None) -> float:
    """time.time() 값(초)을 엔진 단조 시계(ms)로 옮긴다. 같은 순간에 읽은 두 시계의 차를 쓴다."""
    nw = time.time() if now_wall_s is None else now_wall_s
    ne = now_ms() if now_engine_ms is None else now_engine_ms
    return round(ne - (nw - wall_s) * 1000.0, 3)


def stt_offsets(result: Any) -> dict[str, Any] | None:
    """Gemini Live 메시지에 오디오 기준 시작·끝 위치가 있으면 기록용으로 꺼낸다."""
    try:
        t = result.server_content.input_transcription
    except AttributeError:
        return None
    if t is None or (t.start_offset is None and t.end_offset is None):
        return None
    return {"start_offset": t.start_offset, "end_offset": t.end_offset}


@dataclass
class _Utterance:
    utt_id: str
    speech_start_ms: float | None  # VAD 추정 발화 시작
    first_partial_ms: float | None = None
    partials: int = 0
    text: str = ""  # 마지막 미확정 자막(누적 문장)
    prejudged: str = ""  # 마지막으로 선판단을 부른 자막


@dataclass(frozen=True)
class Reply:
    suggestion_id: str
    version: int  # 응답을 만든 대화 버전(agent.say의 version = 제안의 기준 버전)
    text: str


@dataclass
class Playback:
    suggestion_id: str
    version: int
    text: str
    requested_ms: float
    start_ms: float | None = None
    end_ms: float | None = None
    interrupted: str | None = None  # barge_in | version | timeout | error
    done: bool = False


class EngineBridge(FrameProcessor):
    def __init__(self, session: SessionLike, *, final_wait_ms: float = 3000.0, play_start_timeout_ms: float = 15000.0,
                 marker_for: Callable[[float | None, float], dict[str, Any] | None] | None = None,
                 stale_guard: bool = True, provisional: bool = False, **kwargs):
        """marker_for(발화 시작 추정, 발화 종료)는 시험 실행기가 조건 변경 표시를 붙일 때만 준다(엔진은 읽지 않는다).
        stale_guard=False는 정책 A용, provisional=True는 정책 D용이다(모듈 설명)."""
        super().__init__(enable_direct_mode=True, **kwargs)
        channel = getattr(session, "channel", CHANNEL)
        if channel != CHANNEL:
            raise ValueError(f"음성 연결에는 channel={CHANNEL!r}로 만든 엔진 세션을 넘긴다(받은 채널 {channel!r})")
        self.session = session
        self.final_wait_ms = final_wait_ms
        self.play_start_timeout_ms = play_start_timeout_ms
        self._marker_for = marker_for
        self.stale_guard = stale_guard
        self.provisional = provisional
        self._pending_prov: deque[dict[str, Any]] = deque()  # 잠정 확정해 넘기고 확정 자막을 기다리는 발화
        self._prov_open = False  # 잠정 확정 뒤 고객이 아직 다시 말하지 않았다(늦게 온 미확정 자막은 끝난 말의 것)
        self.playbacks: list[Playback] = []
        self.turn_outputs: list[dict[str, Any]] = []  # 세션 handle()이 돌려준 이벤트(실행기가 상담사 연결 등을 본다)
        self.finals: list[dict[str, Any]] = []
        self._n_utt = 0
        self._utt: _Utterance | None = None  # 미확정 자막만 온 발화
        self._vad_speaking = False
        self._seg_start_ms: float | None = None
        self._seg_end_ms: float | None = None
        self._stop_fresh = False  # 마지막 VAD 끝 판정을 아직 어느 확정 발화에도 쓰지 않았다
        self._final_in_seg = False
        self._awaiting_since: float | None = None  # 말이 끝났는데 확정 자막을 기다리는 중
        self._replies: deque[Reply] = deque()
        self._current: Playback | None = None
        self._turns: set[asyncio.Task] = set()
        self._speaker_task: asyncio.Task | None = None
        self._cond = asyncio.Condition()

    # --- 상태 대기 ---------------------------------------------------------------------------

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def wait_until(self, pred: Callable[[], bool], timeout_s: float | None = None) -> bool:
        """pred가 참이 될 때까지 기다린다. 시간이 다 되면 False."""
        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait_for(pred), timeout_s)
            return True
        except TimeoutError:
            return False

    def customer_turn_open(self, t: float | None = None) -> bool:
        """고객 차례인가: VAD가 말하는 중이거나, 말이 끝났는데 확정 자막을 final_wait_ms 안에서 기다리는 중.
        stale_guard가 꺼져 있으면 VAD가 말하는 중일 때만이다."""
        if self._vad_speaking:
            return True
        if not self.stale_guard:
            return False
        t = now_ms() if t is None else t
        return self._awaiting_since is not None and t - self._awaiting_since < self.final_wait_ms

    def idle(self) -> bool:
        """엔진 처리, 붙잡은 응답, 재생이 모두 없는가(실행기가 끝낼 때 본다)."""
        return not self._turns and not self._replies and self._current is None

    async def drain(self, timeout_s: float = 60.0) -> bool:
        """진행 중인 엔진 처리와 남은 응답 재생이 끝나기를 기다린다."""
        if self._turns:
            await asyncio.wait(set(self._turns), timeout=timeout_s)
        return await self.wait_until(self.idle, timeout_s)

    # --- 프레임 ------------------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if self._speaker_task is None:
                self._speaker_task = self.create_task(self._speaker(), name="voice-speaker")
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            if self._speaker_task is not None:
                await self.cancel_task(self._speaker_task)
                self._speaker_task = None
            await self.push_frame(frame, direction)
            return
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, InterimTranscriptionFrame):
                await self._on_partial(frame.text, frame.result)
                return  # 자막은 엔진이 소비한다(TTS로 내려보내지 않는다)
            if isinstance(frame, TranscriptionFrame):
                await self._on_final(frame.text, frame.result)
                return
            if isinstance(frame, VADUserStartedSpeakingFrame):
                await self._on_vad_start(wall_to_engine_ms(frame.timestamp), frame.start_secs)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                await self._on_vad_stop(wall_to_engine_ms(frame.timestamp), frame.stop_secs)
        else:
            if isinstance(frame, BotStartedSpeakingFrame):
                await self._on_bot_started()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                await self._on_bot_stopped()
            elif isinstance(frame, ErrorFrame):
                await self._on_error(frame.error)
        await self.push_frame(frame, direction)

    # --- 입력: VAD와 자막 -----------------------------------------------------------------------

    def _emit(self, type_: str, **fields: Any) -> dict[str, Any]:
        return self.session.log.emit(type_, self.session.version, **fields)

    async def _on_vad_start(self, vad_ms: float, start_secs: float) -> None:
        start = round(vad_ms - start_secs * 1000, 3)
        self._vad_speaking = True
        self._prov_open = False
        self._seg_start_ms = start
        self._final_in_seg = False
        self._awaiting_since = None
        if self._utt is not None and self._utt.speech_start_ms is None:  # 미확정 자막이 VAD보다 먼저 왔다
            self._utt.speech_start_ms = start
        self._emit(VAD_START, vad_ms=vad_ms, speech_start_ms=start)
        pb = self._current
        if pb is not None and not pb.done:
            await self._interrupt(pb, "barge_in", trigger="vad", speech_start_ms=start, vad_ms=vad_ms)
        await self._notify()

    async def _on_vad_stop(self, vad_ms: float, stop_secs: float) -> None:
        end = round(vad_ms - stop_secs * 1000, 3)
        self._vad_speaking = False
        self._seg_end_ms = end
        self._stop_fresh = True
        if self._utt is not None or not self._final_in_seg:
            self._awaiting_since = now_ms()
        self._emit(VAD_STOP, vad_ms=vad_ms, speech_end_ms=end, awaiting_final=self._awaiting_since is not None)
        if self.provisional and self._utt is not None and self._utt.text and not has_digits(self._utt.text):
            await self._send_provisional(end)
        await self._notify()

    async def _send_provisional(self, end: float) -> None:
        """정책 D: 지금 발화의 마지막 미확정 자막을 확정 발화로 엔진에 넘긴다(확정 자막을 기다리지 않는다)."""
        utt = self._utt
        self._utt = None
        self._stop_fresh = False  # 이 VAD 끝 판정은 이 발화에 썼다
        self._final_in_seg = True
        self._awaiting_since = None
        self._prov_open = True
        marker = self._marker_for(utt.speech_start_ms, end) if self._marker_for else None
        info = {"utt_id": utt.utt_id, "text": utt.text, "t_end_ms": end, "t_end_source": "vad",
                "speech_start_ms": utt.speech_start_ms, "first_partial_ms": utt.first_partial_ms,
                "partials": utt.partials}
        t = now_ms()
        self._pending_prov.append({**info, "sent_ms": t})
        self.finals.append({**info, "received_ms": t, "marker": marker, "provisional": True})
        self.session.log.emit(PROVISIONAL, self.session.version, t_ms=t, **info)
        event: dict[str, Any] = {"type": "utterance", "utt_id": utt.utt_id, "speaker": "customer", "text": utt.text,
                                 "final": True, "t_end_ms": end}
        if marker:
            event["marker"] = marker
        self._start_turn(event)

    def _start_turn(self, event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._run_turn(event))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    def _new_utterance(self) -> _Utterance:
        """새 발화. 지금 VAD 구간에서 아직 확정된 발화가 없을 때만 그 구간의 시작을 발화 시작으로 본다."""
        self._n_utt += 1
        known = self._vad_speaking or not self._final_in_seg
        return _Utterance(f"u{self._n_utt}", self._seg_start_ms if known else None)

    async def _on_partial(self, text: str, result: Any = None) -> None:
        t = now_ms()
        if self._prov_open and self._utt is None and not self._vad_speaking:
            # 잠정 확정한 말에 늦게 온 미확정 자막이다. 새 발화나 끼어들기로 보지 않고 기록만 한다
            prov = self._pending_prov[-1] if self._pending_prov else None
            await self.session.handle({"type": "utterance", "utt_id": prov["utt_id"] if prov else "late",
                                       "speaker": "customer", "text": text, "final": False})
            return
        if self._utt is None:
            self._utt = self._new_utterance()
            self._utt.first_partial_ms = t
        self._utt.partials += 1
        if not self._vad_speaking and self._awaiting_since is None:  # VAD가 아직 못 잡았거나 끝 판정 뒤에 온 자막
            self._awaiting_since = t
        pb = self._current
        if pb is not None and not pb.done:  # 짧은 말("네")은 VAD 시작 판정보다 자막이 먼저 온다
            await self._interrupt(pb, "barge_in", trigger="transcript", text=text)
        utt = self._utt
        utt.text = text
        await self.session.handle({"type": "utterance", "utt_id": utt.utt_id, "speaker": "customer",
                                   "text": text, "final": False})
        prejudge = getattr(self.session, "prejudge", None)
        if self.provisional and prejudge is not None and not same_text(text, utt.prejudged):
            utt.prejudged = text
            prejudge(utt.utt_id, text)

    async def _on_final(self, text: str, result: Any = None) -> None:
        t = now_ms()
        if self._pending_prov:
            prov = self._pending_prov.popleft()
            later = self._stop_fresh and self._seg_end_ms is not None and self._seg_end_ms > prov["t_end_ms"]
            if same_text(text, prov["text"]) or not later:
                await self._on_provisional_final(prov, text, t, result)
                return
            # 잠정 확정한 말의 확정 자막이 오지 않고 다음 말의 확정 자막이 왔다
            self._emit(PROVISIONAL_DROPPED, utt_id=prov["utt_id"], text=prov["text"])
        utt = self._utt or self._new_utterance()
        self._utt = None
        self._final_in_seg = True
        # VAD 끝 판정 하나는 확정 발화 하나에만 쓴다. 문장 사이 쉼에서 VAD가 끝을 판정해 STT가 앞 문장을 확정했고 그 사이
        # 고객이 다음 문장을 시작했으면(VAD가 다시 말하는 중) 앞 문장의 끝은 그 끝 판정이다.
        if self._stop_fresh and self._seg_end_ms is not None and \
                (utt.speech_start_ms is None or self._seg_end_ms >= utt.speech_start_ms):
            t_end, source = self._seg_end_ms, "vad"
            self._stop_fresh = False
        else:  # STT가 VAD 끝점보다 먼저 확정했다
            t_end, source = t, "final_received"
        if not self._vad_speaking:
            self._awaiting_since = None
        marker = self._marker_for(utt.speech_start_ms, t_end) if self._marker_for else None
        info = {"utt_id": utt.utt_id, "text": text, "t_end_ms": t_end, "t_end_source": source,
                "speech_start_ms": utt.speech_start_ms, "first_partial_ms": utt.first_partial_ms,
                "partials": utt.partials}
        if (offsets := stt_offsets(result)) is not None:
            info["stt_offsets"] = offsets
        self.finals.append({**info, "received_ms": t, "marker": marker})
        self.session.log.emit(ev.STT_FINAL, self.session.version, t_ms=t, **info)
        event: dict[str, Any] = {"type": "utterance", "utt_id": utt.utt_id, "speaker": "customer", "text": text,
                                 "final": True, "t_end_ms": t_end}
        if marker:
            event["marker"] = marker
        self._start_turn(event)
        await self._notify()

    async def _on_provisional_final(self, prov: dict[str, Any], text: str, t: float, result: Any) -> None:
        """잠정 확정한 발화의 확정 자막. 같으면 기록만 하고, 다르면 새 발화 ID로 엔진에 넘긴다."""
        same = same_text(text, prov["text"])
        utt_id = prov["utt_id"]
        if not same:
            self._n_utt += 1
            utt_id = f"u{self._n_utt}"
        info = {"utt_id": utt_id, "text": text, "t_end_ms": prov["t_end_ms"], "t_end_source": "vad",
                "speech_start_ms": prov["speech_start_ms"], "first_partial_ms": prov["first_partial_ms"],
                "partials": prov["partials"], "provisional_utt_id": prov["utt_id"], "provisional_text": prov["text"],
                "confirms_provisional": same}
        if (offsets := stt_offsets(result)) is not None:
            info["stt_offsets"] = offsets
        self.finals.append({**info, "received_ms": t, "marker": None})
        self.session.log.emit(ev.STT_FINAL, self.session.version, t_ms=t, **info)
        if not self._vad_speaking and self._utt is None:
            self._awaiting_since = None
        if not same:
            self._start_turn({"type": "utterance", "utt_id": utt_id, "speaker": "customer", "text": text,
                              "final": True, "t_end_ms": prov["t_end_ms"]})
        await self._notify()

    async def _run_turn(self, event: dict[str, Any]) -> None:
        try:
            out = await self.session.handle(event)
        except Exception as e:  # 엔진은 자기 오류를 기록하고 보류로 답한다. 여기 오는 것은 입력 형식 오류뿐이다
            self._emit(VOICE_ERROR, stage="engine", utt_id=event["utt_id"], error=f"{type(e).__name__}: {e}")
            out = []
        self.turn_outputs += out
        for e in out:
            if e["type"] == ev.AGENT_SAY:
                self._replies.append(Reply(e["suggestion_id"], int(e["version"]), e["text"]))
        self._turns.discard(asyncio.current_task())  # 기다리는 쪽(idle)이 이 알림에서 끝난 것을 보게 한다
        await self._notify()

    # --- 출력: 응답 재생 ------------------------------------------------------------------------

    async def _speaker(self) -> None:
        while True:
            await self.wait_until(lambda: bool(self._replies))
            reply = self._replies.popleft()
            if self.customer_turn_open():
                self._emit(REPLY_HELD, suggestion_id=reply.suggestion_id, basis_version=reply.version,
                           vad_speaking=self._vad_speaking)
                while self.customer_turn_open():  # 확정 자막 대기에는 시간 한도가 있어 짧게 나눠 다시 본다
                    await self.wait_until(lambda: not self.customer_turn_open(), 0.05)
            if self.stale_guard and self.session.version != reply.version:
                self._emit(ev.RESULT_DISCARDED, stage="voice", started_version=reply.version,
                           suggestion_id=reply.suggestion_id, reason="version")
                continue
            await self._play(reply)

    async def _play(self, reply: Reply) -> None:
        pb = Playback(reply.suggestion_id, reply.version, reply.text, now_ms())
        self.playbacks.append(pb)
        self._current = pb
        self._emit(TTS_REQUEST, t_ms=pb.requested_ms, suggestion_id=pb.suggestion_id, basis_version=pb.version,
                   chars=len(pb.text))
        await self.push_frame(TTSSpeakFrame(reply.text))
        started = await self.wait_until(lambda: pb.start_ms is not None or pb.done, self.play_start_timeout_ms / 1000)
        if not started:
            pb.interrupted = "timeout"
            self._emit(VOICE_ERROR, stage="tts", suggestion_id=pb.suggestion_id, error="재생이 시작되지 않았다")
            await self.broadcast_interruption()
            await self._finish(pb, now_ms())
            return
        if not await self.wait_until(lambda: pb.done, PLAYBACK_END_TIMEOUT_MS / 1000):
            pb.interrupted = "timeout"
            await self._finish(pb, now_ms())

    async def _on_bot_started(self) -> None:
        pb = self._current
        if pb is None or pb.done or pb.start_ms is not None:
            return
        pb.start_ms = t = now_ms()
        self.session.log.emit(ev.SUGGESTION_EXPOSED, self.session.version, t_ms=t, suggestion_id=pb.suggestion_id,
                              channel=CHANNEL, start_ms=t, end_ms=None, basis_version=pb.version,
                              requested_ms=pb.requested_ms)
        if self.stale_guard and self.session.version != pb.version:  # TTS를 기다리는 사이 버전이 바뀌었다
            await self._interrupt(pb, "version")
        await self._notify()

    async def _on_bot_stopped(self) -> None:
        pb = self._current
        if pb is not None and not pb.done and pb.start_ms is not None:
            await self._finish(pb, now_ms())

    async def _on_error(self, error: str) -> None:
        pb = self._current
        self._emit(VOICE_ERROR, stage="pipeline", error=str(error)[:500],
                   suggestion_id=pb.suggestion_id if pb else None)
        if pb is not None and not pb.done and pb.start_ms is None:
            pb.interrupted = "error"
            await self._finish(pb, now_ms())

    async def _interrupt(self, pb: Playback, reason: str, **info: Any) -> None:
        t = now_ms()
        pb.interrupted = reason
        if reason == "barge_in":
            self._emit(BARGE_IN, suggestion_id=pb.suggestion_id, started=pb.start_ms is not None,
                       played_ms=round(t - pb.start_ms, 1) if pb.start_ms is not None else None, **info)
        await self.broadcast_interruption()
        if pb.start_ms is None:  # 소리가 나기 전에 끊었다. BotStoppedSpeakingFrame이 오지 않는다
            await self._finish(pb, t)

    async def _finish(self, pb: Playback, t: float) -> None:
        if pb.done:
            return
        pb.done = True
        pb.end_ms = t
        self._emit(PLAYBACK_END, suggestion_id=pb.suggestion_id, exposed=pb.start_ms is not None,
                   start_ms=pb.start_ms, end_ms=t, interrupted=pb.interrupted,
                   played_ms=round(t - pb.start_ms, 1) if pb.start_ms is not None else None)
        if self._current is pb:
            self._current = None
        await self._notify()
