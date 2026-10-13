"""엔진 지표를 세션 이벤트 로그만으로 계산한다. 정의는 docs/evaluation.md §3.2, §3.3이며 그 문서를 바꾸지 않는다.

§3.2 조건 변경부터 근거 갱신까지의 시간
- 조건 변경 이벤트: 고객 utterance.end 중 marker.condition_change가 참인 발화. 표시는 시뮬레이터가 시나리오 파일의
  mid_call_events 값으로 붙인다(엔진의 condition_changed 판단으로 정하지 않는다).
- t0: 그 발화의 utterance.end 시각(t_ms).
- t1: 그 발화가 만든 대화 버전(version.bumped의 to, utt_id가 그 발화) 이상으로 계산한 evidence.updated(이벤트의
  version 필드) 중 첫 번째의 시각.
- 미갱신: 다음 고객 utterance.end(로그 순서) 전이나 세션 끝까지 t1이 없으면 미갱신. p50·p95는 미갱신을 +∞로 넣어
  선형 보간으로 계산하고 미갱신 수를 따로 낸다. 값은 음수여도 그대로 둔다.

§3.3 무효 제안 노출률
- 제안: suggestion.created(제안 ID와 기준 버전 basis_version). 노출: suggestion.exposed 한 줄이 1회(연속 표시 구간
  1개). 노출 시각은 start_ms(없으면 t_ms), 채널은 channel.
- 무효 노출: 노출 시각 이전에 t0가 있고, 그 발화가 만든 버전이 제안의 기준 버전보다 큰(= 기준 버전 이후의 발화)
  condition_change·invalidates_prior가 모두 참인 이벤트가 하나라도 있는 노출.
- 분모: 같은 세션에서 invalidates_prior가 참인 조건 변경 이벤트 중 하나라도 그 t0 이후에 시작된 노출.
- 이벤트 단위: invalidates_prior가 참인 이벤트 중 그 이벤트 때문에 무효가 된 노출이 하나라도 있는 이벤트의 비율.
- 미재확인 노출: condition_change는 참이고 invalidates_prior는 거짓인 이벤트에 대해, 위와 같은 조건(기준 버전 이후 발화,
  t0 이후 노출)의 노출. 무효 노출로 이미 센 노출은 다시 세지 않는다. 엔진은 지난 제안을 다시 노출하는 경로가 없어
  "다시 판단했는가"는 기준 버전으로만 본다.
- 채널별로 따로 센다.
"""

import math
from dataclasses import dataclass
from typing import Any, Iterable

UTTERANCE_END = "utterance.end"
VERSION_BUMPED = "version.bumped"
EVIDENCE_UPDATED = "evidence.updated"
SUGGESTION_CREATED = "suggestion.created"
SUGGESTION_EXPOSED = "suggestion.exposed"


@dataclass(frozen=True)
class MarkedUtterance:
    """시뮬레이터가 사건 실행을 표시한 고객 발화."""

    session: str
    event: str  # 시나리오 사건 ID, 예: s20#0
    utt_id: str
    t0: float
    version: int  # 그 발화가 만든 대화 버전
    condition_change: bool
    invalidates_prior: bool
    log_index: int  # 로그에서 그 utterance.end의 위치
    next_index: int  # 다음 고객 utterance.end의 위치(없으면 로그 길이)


def _is_customer_utt(e: dict[str, Any]) -> bool:
    return e["type"] == UTTERANCE_END and e.get("speaker", "customer") == "customer"


def utterance_version(events: list[dict[str, Any]], index: int) -> int:
    """index 위치의 고객 발화가 만든 대화 버전. version.bumped(utt_id 일치)의 to, 없으면 발화 시점 버전 + 1."""
    u = events[index]
    for e in events[index + 1:]:
        if e["type"] == VERSION_BUMPED and e.get("utt_id") == u["utt_id"]:
            return int(e["to"])
        if _is_customer_utt(e):
            break
    return int(u["version"]) + 1


def marked_utterances(events: list[dict[str, Any]]) -> list[MarkedUtterance]:
    starts = [i for i, e in enumerate(events) if _is_customer_utt(e)]
    out = []
    for n, i in enumerate(starts):
        u = events[i]
        m = u.get("marker")
        if not isinstance(m, dict) or not m.get("event"):
            continue
        out.append(MarkedUtterance(u.get("session", ""), str(m["event"]), u["utt_id"], float(u["t_ms"]),
                                   utterance_version(events, i), bool(m.get("condition_change")),
                                   bool(m.get("invalidates_prior")), i,
                                   starts[n + 1] if n + 1 < len(starts) else len(events)))
    return out


# --- §3.2 -----------------------------------------------------------------------------------------


def refresh_latencies(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """조건 변경 이벤트마다 {event, utt_id, t0, t1, latency_ms(미갱신이면 None), invalidates_prior}."""
    out = []
    for m in marked_utterances(events):
        if not m.condition_change:
            continue
        t1 = next((float(e["t_ms"]) for e in events[m.log_index + 1:m.next_index]
                   if e["type"] == EVIDENCE_UPDATED and int(e["version"]) >= m.version), None)
        out.append({"session": m.session, "event": m.event, "utt_id": m.utt_id, "version": m.version, "t0": m.t0,
                    "t1": t1, "latency_ms": None if t1 is None else round(t1 - m.t0, 3),
                    "invalidates_prior": m.invalidates_prior})
    return out


def percentile_inf(values: list[float], q: float) -> float:
    """선형 보간 백분위수(jikgam.eval.metrics.percentile과 같은 규칙). +∞를 값으로 받는다."""
    if not values:
        return math.nan
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[lo]
    if math.isinf(xs[hi]):  # 보간 가중치가 0보다 크면 결과도 +∞
        return math.inf
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def latency_stats(latencies: Iterable[float | None]) -> dict[str, Any]:
    """미갱신(None)을 +∞로 넣은 p50·p95와 이벤트 수, 미갱신 수."""
    vals = [math.inf if v is None else float(v) for v in latencies]
    return {"events": len(vals), "p50": percentile_inf(vals, 50), "p95": percentile_inf(vals, 95),
            "misses": sum(math.isinf(v) for v in vals)}


# --- §3.3 -----------------------------------------------------------------------------------------


def exposures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    basis = {e["suggestion_id"]: int(e["basis_version"]) for e in events if e["type"] == SUGGESTION_CREATED}
    out = []
    for e in events:
        if e["type"] != SUGGESTION_EXPOSED:
            continue
        sid = e["suggestion_id"]
        if sid not in basis:
            raise ValueError(f"suggestion.created 없이 노출된 제안: {sid}")
        out.append({"suggestion_id": sid, "basis_version": basis[sid], "channel": e.get("channel", "unknown"),
                    "start": float(e.get("start_ms", e["t_ms"]))})
    return out


def _after(m: MarkedUtterance, x: dict[str, Any]) -> bool:
    """m이 x의 기준 버전 이후 발화이고 m의 t0가 x의 노출 시각 이전인가."""
    return m.version > x["basis_version"] and m.t0 < x["start"]


def stale_exposure(events: list[dict[str, Any]]) -> dict[str, Any]:
    """세션 하나의 무효 노출. {"by_channel": {채널: {invalid, denominator, unconfirmed, cases}}, "events": [...]}."""
    changes = [m for m in marked_utterances(events) if m.condition_change]
    inval = [m for m in changes if m.invalidates_prior]
    keep = [m for m in changes if not m.invalidates_prior]
    xs = exposures(events)
    by_channel: dict[str, dict[str, Any]] = {}
    for x in xs:
        c = by_channel.setdefault(x["channel"], {"invalid": 0, "denominator": 0, "unconfirmed": 0, "cases": []})
        if any(m.t0 < x["start"] for m in inval):
            c["denominator"] += 1
        causes = [m.event for m in inval if _after(m, x)]
        if causes:
            c["invalid"] += 1
            c["cases"].append({"kind": "invalid", "suggestion_id": x["suggestion_id"], "basis_version": x["basis_version"],
                               "start": x["start"], "events": causes})
            continue
        unconf = [m.event for m in keep if _after(m, x)]
        if unconf:
            c["unconfirmed"] += 1
            c["cases"].append({"kind": "unconfirmed", "suggestion_id": x["suggestion_id"],
                               "basis_version": x["basis_version"], "start": x["start"], "events": unconf})
    per_event = [{"event": m.event, "invalid_exposures": sum(_after(m, x) for x in xs)} for m in inval]
    return {"by_channel": by_channel, "events": per_event}


# --- 세션 묶음 -------------------------------------------------------------------------------------


def session_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"refresh": refresh_latencies(events), "stale": stale_exposure(events)}


def aggregate(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """session_metrics 결과 여러 개를 합친다. 비율은 건수와 함께 낸다(docs/evaluation.md §6)."""
    refresh = [r for s in sessions for r in s["refresh"]]
    channels: dict[str, dict[str, Any]] = {}
    events = []
    for s in sessions:
        for ch, c in s["stale"]["by_channel"].items():
            agg = channels.setdefault(ch, {"invalid": 0, "denominator": 0, "unconfirmed": 0, "cases": []})
            for k in ("invalid", "denominator", "unconfirmed"):
                agg[k] += c[k]
            agg["cases"] += c["cases"]
        events += s["stale"]["events"]
    return {
        "refresh": {"stats": latency_stats(r["latency_ms"] for r in refresh), "events": refresh},
        "stale": {"by_channel": channels,
                  "events_with_invalid": sum(e["invalid_exposures"] > 0 for e in events),
                  "events_total": len(events), "events": events},
    }
