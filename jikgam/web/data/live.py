"""실시간 통화 화면의 재생 피드(ADR 0021). 기록된 합성 통화를 원래 시각대로 다시 흘린다.

엔진과 외부 API를 부르지 않는다. 피드는 지금 시각까지의 이벤트만 calls.build_call에 넘겨, 끝난 통화와 같은 함수로
화면 자료를 만든다. 통화가 끝나면 잠시 쉬었다가 처음부터 다시 흐른다. 상담사가 넘겨받으면 그 시각에서 멈춘다.
시계는 바꿔 넣을 수 있다(스크린샷은 고정 시계로 찍는다, ADR 0022).
"""

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jikgam.engine import events as ev

from .. import settings
from .calls import Call, build_call
from .tenants import Tenant, recording_of


@dataclass
class Speech:
    """AI 답 하나가 음성으로 나간 기록. 재생 시작·끝(엔진 시계, 밀리초)과 그동안 드러낼 글자 수."""
    start_ms: float
    end_ms: float
    chars: int    # 끝까지 드러낼 글자 수. 끊긴 답은 끊긴 지점까지다
    cut: bool

    def shown(self, now_ms: float) -> int:
        """now_ms까지 말한 글자 수. 재생 길이에 고르게 나눈다."""
        if now_ms <= self.start_ms:
            return 0
        if now_ms >= self.end_ms or self.end_ms <= self.start_ms:
            return self.chars
        return round(self.chars * (now_ms - self.start_ms) / (self.end_ms - self.start_ms))


def speech_plan(events: list[dict[str, Any]]) -> dict[str, Speech]:
    """제안 id → 음성으로 나간 기록. 끊긴 답이 들린 글자 수는 같은 통화에서 끝까지 재생된 답의 속도(글자/밀리초)로 어림한다.
    화면에 보이는 글자의 양만 정하며, 기록이나 수치는 바꾸지 않는다."""
    texts = {e["suggestion_id"]: e.get("text", "") for e in events if e["type"] == ev.SUGGESTION_CREATED}
    starts = {e["suggestion_id"]: float(e.get("start_ms") or e["t_ms"]) for e in events
              if e["type"] == ev.SUGGESTION_EXPOSED and e.get("channel") == "voice"}
    ends = {e["suggestion_id"]: e for e in events if e["type"] == "tts.playback_end"}
    rates = [len(texts[sid]) / float(pb["played_ms"]) for sid, pb in ends.items()
             if sid in texts and texts[sid] and not pb.get("interrupted") and pb.get("played_ms")]
    rate = statistics.median(rates) if rates else settings.LIVE_SPEECH_CHARS_PER_S / 1000
    plan = {}
    for sid, st in starts.items():
        text = texts.get(sid, "")
        pb = ends.get(sid)
        if pb and pb.get("end_ms"):
            en = float(pb["end_ms"])
            cut = bool(pb.get("interrupted"))
            heard = float(pb.get("played_ms") or (en - st))
            chars = min(len(text), round(heard * rate)) if cut else len(text)
        else:
            en, cut, chars = st + len(text) / rate, False, len(text)
        plan[sid] = Speech(st, en, chars, cut)
    return plan


@dataclass
class Feed:
    id: str
    org: str
    path: Path
    events: list[dict[str, Any]]
    recording: dict[str, Any] | None
    offset_s: float
    clock: Callable[[], float]
    started: float = 0.0
    taken_over_ms: float | None = None
    t_start: float = 0.0
    t_end: float = 0.0
    full: Call | None = field(default=None, repr=False)
    speech: dict[str, Speech] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.full = build_call(self.events, self.id, self.recording)
        self.t_start, self.t_end = self.full.start_ms, self.full.end_ms
        self.speech = speech_plan(self.events)

    def now_ms(self) -> float:
        if self.taken_over_ms is not None:
            return self.taken_over_ms
        span = (self.t_end - self.t_start) + settings.LIVE_LOOP_PAUSE_S * 1000
        pos = ((self.offset_s + self.clock() - self.started) * 1000) % max(span, 1.0)
        return self.t_start + pos

    def call(self) -> Call:
        now = self.now_ms()
        seen = [e for e in self.events if float(e["t_ms"]) <= now or e["type"] == ev.SESSION_START]
        c = build_call(seen, self.id, self.recording, now_ms=now)
        c.start_ms = self.t_start
        c.end_ms = max(now, self.t_start)
        return c

    @property
    def elapsed_s(self) -> float:
        return max(0.0, (self.now_ms() - self.t_start) / 1000)

    @property
    def audio_s(self) -> float | None:
        """녹음에서 지금 위치(초). 녹음 파일이 없으면 None."""
        if not self.recording or not self.recording.get("available"):
            return None
        return max(0.0, (self.now_ms() - self.recording["t0_ms"]) / 1000)

    def take_over(self) -> None:
        if self.taken_over_ms is None:
            self.taken_over_ms = self.now_ms()

    @property
    def taken_over(self) -> bool:
        return self.taken_over_ms is not None


class Feeds:
    """고객사별 재생 피드 목록."""

    def __init__(self, root: Path, tenants: dict[str, Tenant], clock: Callable[[], float] = time.monotonic):
        self.by_org: dict[str, list[Feed]] = {}
        for org, t in tenants.items():
            feeds = []
            for i, rel in enumerate(t.live):
                p = root / rel
                if not p.is_file():
                    continue
                events = ev.read_log(p)
                offsets = settings.LIVE_START_OFFSETS
                feeds.append(Feed(f"{org}-live-{i + 1}", org, p, events, recording_of(root, events, p.parent),
                                  offsets[i % len(offsets)], clock))
            self.by_org[org] = feeds

    def of(self, org: str) -> list[Feed]:
        return self.by_org.get(org, [])

    def get(self, org: str, fid: str) -> Feed | None:
        return next((f for f in self.of(org) if f.id == fid), None)
