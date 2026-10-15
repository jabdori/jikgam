"""실시간 통화 화면의 재생 피드(ADR 0021). 기록된 합성 통화를 원래 시각대로 다시 흘린다.

엔진과 외부 API를 부르지 않는다. 피드는 지금 시각까지의 이벤트만 calls.build_call에 넘겨, 끝난 통화와 같은 함수로
화면 자료를 만든다. 통화가 끝나면 잠시 쉬었다가 처음부터 다시 흐른다. 상담사가 넘겨받으면 그 시각에서 멈춘다.
시계는 바꿔 넣을 수 있다(스크린샷은 고정 시계로 찍는다, ADR 0022).
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jikgam.engine import events as ev

from .. import settings
from .calls import Call, build_call
from .tenants import Tenant, recording_of


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

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.full = build_call(self.events, self.id, self.recording)
        self.t_start, self.t_end = self.full.start_ms, self.full.end_ms

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
