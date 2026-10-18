"""관리 콘솔 통화 리플레이의 자료: 음성 실행의 이벤트 로그와 녹음(ADR 0021, docs/design/data.md).

- 판단: evidence.updated 하나(그 대화 버전의 후보 조항, 조항별 적용 확률, 선택 조항, 행동). 이름은 대화 버전(v6 등).
  같은 버전의 판별 응답(decision.response)에서 요청(intent)을 읽는다.
- 순위: 후보 조항을 적용 확률이 높은 순으로 늘어놓은 것(같으면 후보 순서). 직전 판단 대비 순위 변화와 직전 확률을 함께 둔다.
- 정답 일치도: 시나리오 파일의 정답 조항(expected.articles) 가운데 그 판단 순위 상위 k위 안에 든 개수. k는
  settings.REPLAY_TOP_K(4, ADR 0021).
- 무효가 된 답: 그 판단의 버전에서 무효가 된 제안(suggestion.invalidated의 invalidated_version), 재생된 길이
  (tts.playback_end의 played_ms, 끼어들기로 끊겼는지), 사유. 새 답: 그 버전을 기준으로 만든 제안과 재생 시작 시각.
- 위치(초)는 녹음 시작(voice.recording 또는 recordings.json의 t0_ms) 기준이다. 녹음이 없으면 로그의 첫 시각 기준이다.

미공개 시나리오(h01~h20) 실행은 allow_holdout일 때만 열고, 시나리오 정답도 그때만 읽는다(data/scenarios/HOLDOUT.md).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jikgam.engine import events as ev

from .. import settings
from .tenants import is_event_log, recording_of

VOICE_ROOT = "results/voice"
SCENARIO_FILES = {"s": "data/scenarios/org_a.jsonl", "h": "data/scenarios/org_a_holdout.jsonl"}


@dataclass
class Session:
    run: str
    name: str
    path: Path
    scenario: str | None
    holdout: bool
    policy: str | None
    recording: dict[str, Any] | None


def _scenario_of(name: str) -> str | None:
    m = re.match(r"^([sh]\d{2})\b", name)
    return m.group(1) if m else None


def sessions(root: Path, allow_holdout: bool) -> list[Session]:
    """음성 실행 폴더(results/voice 아래) 의 세션. 미공개 시나리오 세션은 allow_holdout이 아니면 이름만 남기고 잠근다."""
    out = []
    base = root / VOICE_ROOT
    if not base.is_dir():
        return out
    for log in sorted(base.rglob("*.jsonl")):
        if log.name.endswith(".sim.jsonl"):
            continue
        sc = _scenario_of(log.stem)
        holdout = bool(sc and sc.startswith("h"))
        if holdout and not allow_holdout:
            out.append(Session(log.parent.relative_to(base).as_posix(), log.stem, log, sc, True, None, None))
            continue
        if not is_event_log(log):
            continue
        with log.open(encoding="utf-8") as f:
            head = [json.loads(line) for _, line in zip(range(settings.REPLAY_HEAD_LINES), f)]
        if not any(e["type"] == "voice.session" for e in head):
            continue
        policy = next((e.get("policy") for e in head if e["type"] == "voice.session"), None)
        out.append(Session(log.parent.relative_to(base).as_posix(), log.stem, log, sc, holdout, policy, None))
    return out


def find(root: Path, run: str, name: str, allow_holdout: bool) -> Session | None:
    s = next((x for x in sessions(root, allow_holdout) if x.run == run and x.name == name), None)
    if s is None or (s.holdout and not allow_holdout):
        return None
    return s


def scenario(root: Path, sid: str | None, allow_holdout: bool) -> dict[str, Any] | None:
    if not sid or (sid.startswith("h") and not allow_holdout):
        return None
    p = root / SCENARIO_FILES[sid[0]]
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            s = json.loads(line)
            if s.get("id") == sid:
                return s
    return None


def percent(t: float, total: float) -> float:
    return t / total * 100 if total else 0.0


@dataclass
class Rank:
    article: str
    rank: int
    p: float
    prev_rank: int | None
    prev_p: float | None
    selected: bool
    gold: bool


@dataclass
class Answer:
    suggestion: str
    text: str
    basis: int
    created_s: float
    start_s: float | None = None    # 재생(또는 채팅 노출) 시작
    played_s: float | None = None
    cut: bool = False                 # 끼어들기로 끊김
    reasons: list[str] = field(default_factory=list)
    invalidated_at: int | None = None


@dataclass
class Decision:
    index: int
    version: int
    at_s: float
    intent: str | None
    action: str | None
    selected: list[str]
    applies: dict[str, float]
    ranks: list[Rank]
    agree: int | None
    invalidated: list[Answer]
    new: list[Answer]
    cause: str | None                 # 이 버전을 연 것: utterance, tool
    utt_text: str | None
    sufficient: bool | None = None
    latency_ms: float | None = None   # 그 버전 판별 응답의 지연
    model: str | None = None


@dataclass
class Replay:
    session: Session
    duration_s: float
    t0_ms: float
    gold: list[str]
    title: str | None
    threshold: float
    customer: list[tuple[float, float, str]]          # (시작, 끝, 글자)
    ai: list[tuple[float, float, Answer]]
    barges: list[tuple[float, float, str]]            # (위치, 재생된 초, 제안)
    decisions: list[Decision]
    org: str | None = None                            # 통화한 고객사(조항 이름을 그 고객사 약관에서 찾는다)


def build(root: Path, s: Session, allow_holdout: bool) -> Replay:
    events = ev.read_log(s.path)
    rec = recording_of(root, events, s.path.parent)
    s.recording = rec
    start = next(e for e in events if e["type"] == ev.SESSION_START)
    t0 = rec["t0_ms"] if rec else float(events[0]["t_ms"])
    sec = lambda t: round((float(t) - t0) / 1000, 3)  # noqa: E731
    sc = scenario(root, s.scenario, allow_holdout)
    gold = list((sc or {}).get("expected", {}).get("articles", []))

    customer = [(sec(e["speech_start_ms"]), sec(e["speech_end_ms"]), e.get("text", ""))
                for e in events if e["type"] == "voice.segment" and e.get("speech_start_ms")]
    created = {e["suggestion_id"]: e for e in events if e["type"] == ev.SUGGESTION_CREATED}
    played = {e["suggestion_id"]: e for e in events if e["type"] == "tts.playback_end"}
    barge = {e["suggestion_id"]: e for e in events if e["type"] == "voice.barge_in" and e.get("started")}
    answers: dict[str, Answer] = {}
    for sid, c in created.items():
        a = Answer(sid, c.get("text", ""), int(c["basis_version"]), sec(c["t_ms"]))
        pb = played.get(sid)
        if pb and pb.get("start_ms") is not None:
            a.start_s = sec(pb["start_ms"])
            a.played_s = round(float(pb.get("played_ms") or 0) / 1000, 3)
        a.cut = sid in barge or bool(pb and pb.get("interrupted") == "barge_in")
        answers[sid] = a
    for e in events:
        if e["type"] == ev.SUGGESTION_INVALIDATED and e["suggestion_id"] in answers:
            a = answers[e["suggestion_id"]]
            a.reasons = list(e.get("reasons") or [])
            a.invalidated_at = int(e.get("invalidated_version") or e["version"])
    ai = [(a.start_s, a.start_s + (a.played_s or 0), a) for a in answers.values() if a.start_s is not None and a.played_s]
    barges = [(sec(e.get("speech_start_ms") or e["t_ms"]), round(float(e.get("played_ms") or 0) / 1000, 3), e["suggestion_id"])
              for e in barge.values()]

    responses = {int(e.get("request_version", e["version"])): e
                 for e in events if e["type"] == ev.DECISION_RESPONSE and not e.get("error")}
    intents = {v: ((r.get("answers") or {}).get("intent") or {}).get("label") for v, r in responses.items()}
    bumps = {int(e["to"]): e for e in events if e["type"] == ev.VERSION_BUMPED}
    utts = {e["utt_id"]: e.get("text") for e in events if e["type"] == ev.UTTERANCE_END}
    threshold = float(start.get("applies_threshold") or 0.5)
    decisions: list[Decision] = []
    prev: dict[str, tuple[int, float]] = {}
    for e in events:
        if e["type"] != ev.EVIDENCE_UPDATED:
            continue
        v = int(e["version"])
        applies = {k: float(p) for k, p in (e.get("applies") or {}).items()}
        cands = list(e.get("candidates") or applies)
        order = sorted(cands, key=lambda a: (-applies.get(a, 0.0), cands.index(a)))
        ranks = [Rank(a, i + 1, applies.get(a, 0.0), prev.get(a, (None, None))[0], prev.get(a, (None, None))[1],
                      a in (e.get("articles") or []), a in gold) for i, a in enumerate(order)]
        top = order[:settings.REPLAY_TOP_K]
        bump = bumps.get(v, {})
        decisions.append(Decision(
            len(decisions) + 1, v, sec(e["t_ms"]), e.get("intent") or intents.get(v), e.get("action"),
            list(e.get("articles") or []), applies, ranks, sum(a in gold for a in top) if gold else None,
            [a for a in answers.values() if a.invalidated_at == v], [a for a in answers.values() if a.basis == v],
            bump.get("cause"), utts.get(bump.get("utt_id")) if bump.get("utt_id") else None,
            e.get("sufficient"), (responses.get(v) or {}).get("latency_ms"), (responses.get(v) or {}).get("model")))
        prev = {r.article: (r.rank, r.p) for r in ranks}

    ends = [c[1] for c in customer] + [x[1] for x in ai] + [d.at_s for d in decisions]
    duration = rec["duration_ms"] / 1000 if rec else (max(ends) if ends else 0.0)
    return Replay(s, duration, t0, gold, (sc or {}).get("title"), threshold, customer, ai, barges, decisions,
                  start.get("org"))
