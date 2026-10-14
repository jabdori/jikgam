"""입력 이벤트 형식, 단조 시계, 세션별 이벤트 로그(JSONL).

모든 이벤트는 세션별 JSONL 로그에 한 줄씩 남는다(docs/engine.md §2). 공통 필드는 t_ms(엔진 프로세스의 단조 시계),
wall(KST ISO 시각), session, type, version(그 시점의 대화 버전)이다. 유형은 docs/evaluation.md §3.1을 따르고,
단계별 지연과 판단 근거를 보기 위한 유형(retrieval.done, policy.decided 등)을 더한다.

wall은 t_ms에서 계산한다. 프로세스 시작 시점의 벽시계에 단조 시계 경과를 더하므로, 같은 로그 안에서 두 값은 어긋나지 않는다.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))
_ORIGIN_NS = time.perf_counter_ns()
_ORIGIN_WALL = datetime.now(KST)

# docs/evaluation.md §3.1의 유형
UTTERANCE_END = "utterance.end"
STT_PARTIAL = "stt.partial"
STT_FINAL = "stt.final"
VERSION_BUMPED = "version.bumped"
DECISION_REQUEST = "decision.request"
DECISION_RESPONSE = "decision.response"
DECISION_PREJUDGE = "decision.prejudge"  # 정책 D 선판단(미확정 자막으로 미리 한 첫 판별). 상태를 바꾸지 않는다
EVIDENCE_UPDATED = "evidence.updated"
SUGGESTION_CREATED = "suggestion.created"
SUGGESTION_INVALIDATED = "suggestion.invalidated"
SUGGESTION_EXPOSED = "suggestion.exposed"
RESULT_DISCARDED = "result.discarded"
TOOL_CALL = "tool.call"
TOOL_RESULT = "tool.result"
# 엔진이 더하는 유형: 단계별 지연과 판단 근거
SESSION_START = "session.start"
CLAIMS_EXTRACTED = "claims.extracted"
RETRIEVAL_DONE = "retrieval.done"
POLICY_DECIDED = "policy.decided"
QUOTE_SELECTED = "quote.selected"
GENERATION_REQUEST = "generation.request"
GENERATION_RESPONSE = "generation.response"
TASK_CANCELLED = "task.cancelled"
ENGINE_ERROR = "engine.error"
AGENT_SAY = "agent.say"
WARMUP = "warmup"  # 세션 시작 예열 요청. 턴 판단(decision.*)으로 세지 않는다
FAULT_INJECTED = "fault.injected"  # 재생 실행기가 모의 업무 API에 장애를 주입함(재생 파일의 faults)
WRITES_WAIT = "writes.wait"  # 앞 턴이 시작한 쓰기가 끝나기를 기다린 뒤 정책을 정함(빠른 정정)

SPEAKERS = ("customer", "agent")


def now_ms() -> float:
    """엔진 프로세스의 단조 시계(ms). 프로세스 안의 모든 세션이 같은 원점을 쓴다."""
    return round((time.perf_counter_ns() - _ORIGIN_NS) / 1e6, 3)


def wall_at(t_ms: float) -> str:
    """단조 시계 값에 해당하는 KST 시각."""
    return (_ORIGIN_WALL + timedelta(milliseconds=t_ms)).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Utterance:
    """입력 이벤트(docs/engine.md §2). 채팅은 항상 final이고 t_end_ms는 엔진이 받은 시각이다."""

    utt_id: str
    speaker: str
    text: str
    final: bool
    t_end_ms: float
    marker: dict[str, Any] | None = None


def parse_utterance(event: dict[str, Any], received_ms: float) -> Utterance:
    """입력 이벤트를 검사해 Utterance로 바꾼다. 형식이 틀리면 ValueError."""
    if event.get("type") != "utterance":
        raise ValueError(f"입력 이벤트 type은 utterance여야 한다: {event.get('type')!r}")
    utt_id, text, speaker = event.get("utt_id"), event.get("text"), event.get("speaker", "customer")
    if not isinstance(utt_id, str) or not utt_id:
        raise ValueError("utt_id가 필요하다")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text가 비어 있다")
    if speaker not in SPEAKERS:
        raise ValueError(f"speaker는 {SPEAKERS} 중 하나여야 한다: {speaker!r}")
    final = event.get("final", True)
    if not isinstance(final, bool):
        raise ValueError("final은 true/false여야 한다")
    t_end = event.get("t_end_ms")
    if t_end is not None and not isinstance(t_end, (int, float)):
        raise ValueError("t_end_ms는 숫자여야 한다")
    marker = event.get("marker")
    if marker is not None and not isinstance(marker, dict):
        raise ValueError("marker는 객체여야 한다")
    return Utterance(utt_id, speaker, text.strip(), final, float(t_end) if t_end is not None else received_ms, marker)


class EventLog:
    """세션 하나의 JSONL 이벤트 로그. 줄마다 바로 flush해 중간에 멈춰도 그때까지의 기록이 남는다."""

    def __init__(self, path: Path, session: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.session = session
        self._f = path.open("a", encoding="utf-8", newline="\n")

    def emit(self, type_: str, version: int, *, t_ms: float | None = None, **fields: Any) -> dict[str, Any]:
        t = now_ms() if t_ms is None else t_ms
        event = {"t_ms": t, "wall": wall_at(t), "session": self.session, "type": type_, "version": version, **fields}
        self._f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self._f.flush()
        return event

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def turn_latencies(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """고객 확정 발화마다 단계별 지연(ms)을 로그에서 계산한다.

    한 턴은 그 발화의 utterance.end부터 다음 고객 utterance.end 전까지의 로그 줄이다. 기준 시각 t0는 utterance.end.
    - evidence_ms: 그 발화가 만든 버전 이상으로 계산한 evidence.updated가 처음 기록된 시각 − t0
      (docs/evaluation.md §3.2의 근거 갱신 시간. 턴 안에 없으면 None = 미갱신)
    - say_ms: 그 버전 이상으로 만든 agent.say가 처음 기록된 시각 − t0
    - 그 밖: 검색 시간 합, 판별 호출 지연 목록, 도구 호출 지연 목록, 생성 지연 목록, 폐기·취소 수
    """
    starts = [i for i, e in enumerate(events) if e["type"] == UTTERANCE_END and e.get("speaker") == "customer"]
    turns = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(events)
        u = events[i]
        span = events[i + 1:end]
        bump = next((e for e in span if e["type"] == VERSION_BUMPED and e.get("utt_id") == u["utt_id"]), None)
        v = bump["to"] if bump else u["version"] + 1
        t0 = u["t_ms"]
        ev = next((e for e in span if e["type"] == EVIDENCE_UPDATED and e["version"] >= v), None)
        say = next((e for e in span if e["type"] == AGENT_SAY and e["version"] >= v), None)
        turns.append({
            "utt_id": u["utt_id"],
            "text": u.get("text"),
            "marker": u.get("marker"),
            "version": v,
            "evidence_ms": round(ev["t_ms"] - t0, 1) if ev else None,
            "say_ms": round(say["t_ms"] - t0, 1) if say else None,
            "retrieval_ms": round(sum(e["duration_ms"] for e in span if e["type"] == RETRIEVAL_DONE), 1),
            "decision_ms": [round(e["latency_ms"], 1) for e in span if e["type"] == DECISION_RESPONSE],
            "tool_ms": [(e["tool"], round(e["latency_ms"], 1)) for e in span if e["type"] == TOOL_RESULT],
            "generation_ms": [round(e["latency_ms"], 1) for e in span if e["type"] == GENERATION_RESPONSE],
            "evidence_updates": sum(e["type"] == EVIDENCE_UPDATED for e in span),
            "discarded": sum(e["type"] == RESULT_DISCARDED for e in span),
            "cancelled": sum(e["type"] == TASK_CANCELLED for e in span),
            "say": say.get("text") if say else None,
        })
    return turns
