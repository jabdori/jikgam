"""통화 하나의 기록: 엔진 이벤트 로그에서 고객사 화면이 보여 줄 사실을 꺼낸다(ADR 0021, docs/design/data.md).

입력은 이벤트 목록뿐이다. 끝난 통화의 로그 전체에도, 재생 피드가 지금까지 흘린 앞부분에도 같은 함수를 쓴다.
엔진 내부 값(판별 확률, 대화 버전, 지연)은 결과에 넣지 않는다. 그런 값은 관리 콘솔의 자료(jikgam/web/data/replay.py)가 다룬다.

- 고객: verify_identity의 customer_id와 get_customer의 이름. 본인 확인 전이면 이름이 없다.
- 요청: 판별 응답의 intent를 발화 순서대로 모은 것(같은 값이 이어지면 하나로).
- 처리 결과: 상담사 연결(create_handoff 성공 또는 마지막 정책 행동 handoff) > 접수(create_cancellation,
  create_refund_request 성공) > 보류(마지막 행동 hold) > 안내 완료.
- 고객이 요청을 바꾼 순간: 시나리오가 조건 변경으로 표시한 고객 발화(utterance.end의 marker). 그 순간의 잘못된 안내는
  평가와 같은 함수(jikgam.sim.metrics.stale_exposure)로 센다.
- 대화: 고객 확정 발화와 고객에게 나간 AI 답(suggestion.exposed). 음성이면 녹음 위치(초)를 함께 둔다. 고객 발화의
  전화번호는 가운데를 가리고, 인증번호를 말한 발화의 4~6자리 숫자는 가린다(화면의 가림 규칙을 대화에도 같게 쓴다).
"""

import re

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jikgam.engine import events as ev
from jikgam.sim import metrics as mt

INTENT = {"refund": "환불", "cancel": "해지", "cancel_refund": "해지·환불", "plan_change": "요금제 변경",
          "billing_inquiry": "결제일 문의", "compensation": "장애 보상", "other": "기타 문의"}
PLAN = {"monthly": "월 구독", "annual": "연 구독", "family_addon": "가족 공유", "family": "가족 이용권", "trial": "무료 체험"}
METHOD = {"card": "카드", "mobile": "휴대폰", "easy_pay": "간편결제", "point": "포인트"}
HANDOFF_REASON = {"minor": "미성년자 결제 취소", "dispute": "카드사 이의 제기", "identity_failed": "본인 확인 반복 실패",
                  "repeat_refund": "최근 환불 반복", "approval": "승인 기준 초과", "customer_request": "고객 요청"}
HOLD_REASON = {"api_failure": "업무 시스템 응답 없음", "identity_unavailable": "고객이 본인 확인 정보를 댈 수 없음",
               "pass_limit": "처리 한도 도달", "engine_error": "처리 오류", "decision_unavailable": "처리 오류"}
ANSWER_KIND = {"not_eligible": "환불 불가 안내", "third_party": "본인 아님 안내", "declined": "접수하지 않음",
               "compensation": "장애 보상 안내", "compensation_none": "보상 대상 아님 안내", "decision": "문의 안내",  # decision은 요청 이름을 붙인다
               "received": "접수 결과 안내", "proposal": "접수 동의 대기", "identity_required": "본인 확인 중",
               "lookups": "조회 중", "quote": "환불 확인 중", "target_ambiguous": "대상 확인 중",
               "account_required": "환불 계좌 요청", "verify_identity": "본인 확인 중", "handoff": "상담사 연결"}
LOOKUP_TOOLS = ("get_customer", "list_subscriptions", "list_payments", "get_usage", "list_incidents")
OUTCOME = {"filed": "해지·환불 접수", "answered": "안내 완료", "handoff": "상담사 연결", "hold": "보류", "dropped": "중간 종료"}
# 끝난 통화의 마지막 정책 행동이 확인 질문(ask)이면 고객이 답하기 전에 통화가 끝난 것이다(중간 종료).
ASK_TOPIC = {"phone": "휴대폰 번호", "otp": "인증번호", "otp_retry": "인증번호", "account": "환불 계좌",
             "own_account": "본인 명의 계좌", "account_holder": "예금주", "plan_type": "요금제 종류", "identity": "본인 확인"}


@dataclass
class Line:
    kind: str                 # customer, ai
    text: str
    t_ms: float
    start_s: float | None = None   # 녹음 위치(초). 음성 통화만
    end_s: float | None = None
    cut: bool = False         # 고객이 말을 끊어 AI 음성이 중간에 멈춤
    played_s: float | None = None  # 음성으로 나간 길이(초). 끊긴 답이면 고객이 들은 길이
    articles: list[str] = field(default_factory=list)  # AI 답이 근거로 든 조항
    playing: bool = False     # 재생 피드에서 지금 나가는 중
    elapsed_s: float = 0.0    # 통화 시작 뒤 시각(초)
    sid: str | None = None    # AI 답의 제안 id(재생 피드가 그 답의 재생 시각을 찾는다)

    @property
    def key(self) -> str:
        """화면이 줄을 다시 그리지 않고 이어 붙일 때 쓰는 줄 이름(화자와 시각)."""
        return f"{self.kind}:{self.t_ms:.0f}"


@dataclass
class Change:
    text: str                 # 요청을 바꾼 고객 발화
    t_ms: float
    before: str | None        # 바뀌기 전 요청(INTENT 이름)
    after: str | None
    wrong: int                # 그 변경 뒤 나간 잘못된 안내 수
    line_index: int           # 대화에서 이 발화의 위치(그 뒤에 한 줄 표시를 둔다)

    @property
    def key(self) -> str:
        return f"change:{self.t_ms:.0f}"


@dataclass
class Step:
    label: str
    result: str
    done: bool


@dataclass
class Call:
    id: str
    session: str
    org: str
    channel: str              # voice, chat
    started: datetime | None
    start_ms: float
    end_ms: float
    customer_id: str | None = None
    name: str | None = None
    phone: str | None = None  # 가린 번호
    verified: bool = False
    intents: list[str] = field(default_factory=list)
    outcome: str = "answered"
    outcome_label: str = "안내 완료"
    reason: str | None = None  # 상담사 연결·보류 사유, 안내 완료의 종류
    cancelled: bool = False
    refunded: bool = False
    refund_amount: int | None = None
    refund_pending_approval: bool = False
    refund_denied_articles: list[str] = field(default_factory=list)  # 환불 대상 아님의 근거 조항(견적 결과)
    cancel_articles: list[str] = field(default_factory=list)  # 해지 접수 결과가 근거로 든 조항
    asked: str | None = None   # 중간 종료일 때 AI가 묻던 것
    last_usable_date: str | None = None
    receipts: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    articles: list[str] = field(default_factory=list)  # 고객에게 나간 답이 근거로 든 조항 ID(처음 나온 순서)
    changes: list[Change] = field(default_factory=list)
    wrong_total: int = 0
    lines: list[Line] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    recording: dict[str, Any] | None = None
    handoff_requested: bool = False
    customer_turns: int = 0
    lookups: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)  # 업무 API 동작별 호출 수

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000)

    @property
    def topic(self) -> str:
        return " → ".join(INTENT.get(i, INTENT["other"]) for i in self.intents) or "문의 확인 중"

    @property
    def changed(self) -> bool:
        return bool(self.changes)


PHONE_RE = re.compile(r"(01[016789])-?(\d{3,4})-?(\d{4})")
CODE_RE = re.compile(r"(?<![\d,])\d{4,6}(?![\d,]|원|년|월|일)")


def mask_text(text: str) -> str:
    """고객 발화의 전화번호 가운데와 인증번호(인증을 말한 발화의 4~6자리 숫자)를 가린다."""
    out = PHONE_RE.sub(lambda m: f"{m.group(1)}-{'*' * len(m.group(2))}-{m.group(3)}", text)
    if "인증" in out:
        out = CODE_RE.sub(lambda m: "*" * len(m.group(0)), out)
    return out


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    parts = phone.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}"
    return phone[:3] + "*" * max(0, len(phone) - 7) + phone[-4:]


def _intents(events: list[dict[str, Any]]) -> tuple[list[str], dict[int, str]]:
    """발화 순서의 요청 목록과 대화 버전 → 그 버전에서 판단한 요청."""
    by_version: dict[int, str] = {}
    for e in events:
        if e["type"] == ev.DECISION_RESPONSE and not e.get("error"):
            label = ((e.get("answers") or {}).get("intent") or {}).get("label")
            if label and label != "unclear":
                by_version[int(e.get("request_version", e["version"]))] = label
    seq: list[str] = []  # 처음 나온 순서(같은 요청으로 돌아온 것은 다시 넣지 않는다)
    for v in sorted(by_version):
        if by_version[v] not in seq:
            seq.append(by_version[v])
    return seq, by_version


def _recording_offset(events: list[dict[str, Any]], recording: dict[str, Any] | None) -> float | None:
    if recording is None:
        return None
    return float(recording["t0_ms"])


def build_call(events: list[dict[str, Any]], call_id: str, recording: dict[str, Any] | None = None,
               now_ms: float | None = None) -> Call:
    """이벤트 목록 → Call. recording은 {"file", "t0_ms", "duration_ms"}(엔진 시계 기준 녹음 시작). now_ms는 재생 피드의 현재
    시각(그때 재생 중인 AI 답을 표시한다)."""
    start = next((e for e in events if e["type"] == ev.SESSION_START), events[0] if events else {})
    voice = any(e["type"] == "voice.session" for e in events)
    rec_t0 = _recording_offset(events, recording)
    first_speech = [float(e["speech_start_ms"]) for e in events if e["type"] == "voice.segment" and e.get("speech_start_ms")]
    first_utt = next((float(e["t_ms"]) for e in events if e["type"] == ev.UTTERANCE_END
                      and e.get("speaker", "customer") == "customer"), None)
    if voice and first_speech:
        t_start = min(first_speech)
    elif first_utt is not None:
        t_start = first_utt
    else:
        t_start = float(start.get("t_ms", 0.0))
    wall = start.get("wall")
    call = Call(id=call_id, session=start.get("session", ""), org=start.get("org", ""), channel="voice" if voice else "chat",
                started=datetime.fromisoformat(wall) if wall else None, start_ms=t_start, end_ms=t_start)

    suggestions = {e["suggestion_id"]: e for e in events if e["type"] == ev.SUGGESTION_CREATED}
    playback = {e["suggestion_id"]: e for e in events if e["type"] == "tts.playback_end"}
    segments = {e.get("line"): e for e in events if e["type"] == "voice.segment"}
    barged = {e["suggestion_id"] for e in events if e["type"] == "voice.barge_in" and e.get("started")}
    seq, by_version = _intents(events)
    call.intents = seq
    call_args: dict[str, dict[str, Any]] = {}
    last_policy: dict[str, Any] | None = None
    verify_fail = 0
    end_ms = t_start

    def audio(t: float | None) -> float | None:
        return None if t is None or rec_t0 is None else round((t - rec_t0) / 1000, 3)

    n_customer = 0
    for e in events:
        t = e["type"]
        tm = float(e["t_ms"])
        if t == ev.UTTERANCE_END and e.get("speaker", "customer") == "customer":
            n_customer += 1
            seg = segments.get(n_customer) if voice else None
            s0 = audio(float(seg["speech_start_ms"])) if seg and seg.get("speech_start_ms") else None
            s1 = audio(float(seg["speech_end_ms"])) if seg and seg.get("speech_end_ms") else None
            call.lines.append(Line("customer", mask_text(e.get("text", "")), tm, s0, s1, elapsed_s=(tm - t_start) / 1000))
            end_ms = max(end_ms, tm)
        elif t == ev.SUGGESTION_EXPOSED:
            sug = suggestions.get(e["suggestion_id"], {})
            pb = playback.get(e["suggestion_id"])
            st = float(e.get("start_ms") or tm)
            en = float(pb["end_ms"]) if pb and pb.get("end_ms") else (float(e["end_ms"]) if e.get("end_ms") else st)
            playing = now_ms is not None and voice and (pb is None or now_ms < en)
            call.lines.append(Line("ai", sug.get("text", ""), st, audio(st), audio(en),
                                   cut=e["suggestion_id"] in barged or bool(pb and pb.get("interrupted") == "barge_in"),
                                   playing=playing, elapsed_s=(st - t_start) / 1000,
                                   played_s=round(float(pb["played_ms"]) / 1000, 2) if pb and pb.get("played_ms") else None,
                                   articles=list(sug.get("cited_articles") or []), sid=e["suggestion_id"]))
            for a in sug.get("cited_articles") or []:
                if a not in call.articles:
                    call.articles.append(a)
            end_ms = max(end_ms, en)
        elif t == ev.TOOL_CALL:
            call_args[e["call_id"]] = e.get("args") or {}
            call.tool_counts[e.get("tool", "")] = call.tool_counts.get(e.get("tool", ""), 0) + 1
            if e.get("tool") == "verify_identity" and (e.get("args") or {}).get("phone"):
                call.phone = mask_phone(e["args"]["phone"])
        elif t == ev.TOOL_RESULT:
            tool, d, ok = e.get("tool"), e.get("data") or {}, bool(e.get("ok"))
            if tool == "verify_identity":
                if ok and d.get("verified"):
                    call.verified = True
                    call.customer_id = d.get("customer_id")
                else:
                    verify_fail += 1
            elif tool == "get_customer" and ok:
                call.name = d.get("name") or call.name
            elif tool in LOOKUP_TOOLS and ok:
                call.lookups += 1
            if tool in LOOKUP_TOOLS and ok:
                _facts(call, tool, d)
            if tool == "create_cancellation" and ok:
                call.cancelled = True
                call.cancel_articles = list(d.get("article_ids") or [])
                call.last_usable_date = d.get("last_usable_date") or call.last_usable_date
                call.receipts.append(d.get("cancellation_id", ""))
            elif tool == "create_refund_request" and ok:
                call.refunded = True
                call.refund_amount = (call.refund_amount or 0) + int(d.get("amount") or 0)
                call.refund_pending_approval = call.refund_pending_approval or d.get("status") == "pending_approval"
                call.last_usable_date = d.get("last_usable_date") or call.last_usable_date
                call.receipts.append(d.get("refund_id", ""))
            elif tool == "create_handoff" and ok:
                call.handoff_requested = True
                call.reason = HANDOFF_REASON.get(d.get("reason") or call_args.get(e["call_id"], {}).get("reason"), "상담사 연결 요청")
        elif t == ev.POLICY_DECIDED:
            last_policy = e
            if e.get("action") == "handoff":
                call.handoff_requested = True
        elif t == "voice.barge_in":
            end_ms = max(end_ms, tm)
    call.customer_turns = n_customer
    call.end_ms = end_ms if now_ms is None else max(end_ms, now_ms)

    rule = (last_policy or {}).get("rule")
    action = (last_policy or {}).get("action")
    if call.handoff_requested:
        call.outcome = "handoff"
        call.reason = call.reason or "상담사 연결 요청"
        call.outcome_label = "상담사 연결"
    elif call.cancelled or call.refunded:
        call.outcome = "filed"
        call.outcome_label = "해지·환불 접수" if call.cancelled and call.refunded else ("해지 접수" if call.cancelled else "환불 접수")
    elif action == "ask" and now_ms is None:
        call.outcome = "dropped"
        call.asked = ASK_TOPIC.get((last_policy or {}).get("ask"), None)
        call.reason = call.asked
        call.outcome_label = OUTCOME["dropped"]
    elif action == "hold":
        call.outcome = "hold"
        call.reason = HOLD_REASON.get(rule, "보류")
        call.outcome_label = "보류"
    else:
        call.outcome = "answered"
        last_intent = INTENT.get(seq[-1]) if seq else None
        call.reason = f"{last_intent} 안내" if rule == "decision" and last_intent else ANSWER_KIND.get(rule, "문의 안내")
        call.outcome_label = call.reason

    call.steps = _steps(call, verify_fail, events)
    call.changes = _changes(events, call, by_version)
    stale = mt.stale_exposure(events) if any(e["type"] == ev.SUGGESTION_CREATED for e in events) else {"by_channel": {}}
    call.wrong_total = sum(c["invalid"] for c in stale["by_channel"].values())
    if recording is not None:
        call.recording = recording
    return call


def _facts(call: Call, tool: str, d: dict[str, Any]) -> None:
    """고객 정보 칸: 요금제, 최근 결제, 다음 결제일, 최근 이용. 업무 API 조회 결과에서만 만든다(ADR 0003)."""
    facts = dict(call.facts)
    if tool == "list_subscriptions":
        subs = [s for s in d.get("subscriptions") or [] if s.get("status") == "active"] or d.get("subscriptions") or []
        if subs:
            s = subs[0]
            facts["요금제"] = f"{PLAN.get(s.get('plan'), '구독')} {s.get('price'):,}원" if s.get("price") is not None \
                else PLAN.get(s.get("plan"), "구독")
            if s.get("next_billing"):
                facts["다음 결제일"] = s["next_billing"]
    elif tool == "list_payments":
        pays = sorted(d.get("payments") or [], key=lambda p: p.get("date", ""))
        if pays:
            p = pays[-1]
            facts["최근 결제"] = f"{p.get('date')} · {METHOD.get(p.get('method'), '기타')}"
    elif tool == "get_usage":
        uses = sorted(u.get("date", "") for u in d.get("usage") or [])
        facts["최근 이용"] = uses[-1] if uses else "기록 없음"
    order = ("요금제", "최근 결제", "다음 결제일", "최근 이용")
    call.facts = [(k, facts[k]) for k in order if k in facts]


def _steps(call: Call, verify_fail: int, events: list[dict[str, Any]]) -> list[Step]:
    """처리 내역: 본인 확인, 기록 조회, 접수(또는 상담사 연결), 환불."""
    out = []
    if call.verified:
        out.append(Step("본인 확인", "휴대폰 인증 완료" if not verify_fail else f"인증 완료(실패 {verify_fail}회 뒤)", True))
    elif verify_fail:
        out.append(Step("본인 확인", f"실패 {verify_fail}회", False))
    else:
        out.append(Step("본인 확인", "하지 않음", False))
    out.append(Step("구독·결제 조회", "완료" if call.lookups else "하지 않음", bool(call.lookups)))
    if call.outcome == "handoff":
        out.append(Step("상담사 연결", call.reason or "연결 요청", True))
    elif call.cancelled:
        out.append(Step("해지 접수", "완료", True))
    if call.refunded:
        out.append(Step("환불 접수", "승인 대기" if call.refund_pending_approval else "완료", True))
    elif call.outcome != "handoff":
        quotes = [e for e in events if e["type"] == ev.TOOL_RESULT and e.get("tool") == "quote_refund"]
        if not quotes and call.cancelled:
            out.append(Step("환불", "접수 안 함", False))
        elif quotes and not any((q.get("data") or {}).get("eligible") for q in quotes):
            out.append(Step("환불", "대상 아님", False))
            for q in quotes:
                for a in (q.get("data") or {}).get("article_ids") or []:
                    if a not in call.refund_denied_articles:
                        call.refund_denied_articles.append(a)
    return out


def _changes(events: list[dict[str, Any]], call: Call, by_version: dict[int, str]) -> list[Change]:
    """시나리오가 조건 변경으로 표시한 고객 발화마다 바뀌기 전·후 요청과 그 뒤 잘못된 안내 수."""
    out = []
    marked = [m for m in mt.marked_utterances(events) if m.condition_change]
    stale = mt.stale_exposure(events) if any(e["type"] == ev.SUGGESTION_CREATED for e in events) else {"events": []}
    wrong_by_event = {x["event"]: x["invalid_exposures"] for x in stale["events"]}
    customer_lines = [i for i, ln in enumerate(call.lines) if ln.kind == "customer"]
    for m in marked:
        before = next((by_version[v] for v in sorted(by_version, reverse=True) if v < m.version), None)
        after = next((by_version[v] for v in sorted(by_version) if v >= m.version), None)
        idx = next((i for i in customer_lines if abs(call.lines[i].t_ms - m.t0) < 1e-6), None)
        if idx is None:
            continue
        out.append(Change(call.lines[idx].text, m.t0, before, after, wrong_by_event.get(m.event, 0), idx))
    return out


def live_steps(call: Call, taken_over: bool = False) -> list[tuple[str, str, str]]:
    """진행 중인 통화의 단계(본인 확인 → 조회 → 안내 → 접수). (이름, 상태 done·now·todo, 상태 글자)."""
    answered = any(ln.kind == "ai" for ln in call.lines) and call.lookups > 0
    filed = call.cancelled or call.refunded
    what = INTENT.get(call.intents[-1]) if call.intents else None
    guide, receipt = (f"{what} 안내", f"{what} 접수") if what else ("안내", "접수")
    out = [("본인 확인", "done" if call.verified else "now", "완료" if call.verified else "진행 중")]
    if call.verified:
        out.append(("구독·결제 조회", "done" if call.lookups else "now", "완료" if call.lookups else "진행 중"))
    else:
        out.append(("구독·결제 조회", "todo", "본인 확인 후"))
    if filed:
        out.append((guide, "done", "완료"))
    elif answered:
        out.append((guide, "now", "진행 중"))
    else:
        out.append((guide, "todo", "대기"))
    if call.handoff_requested or taken_over:
        out.append(("상담사 연결", "done" if taken_over else "now", "상담사 응대 중" if taken_over else "연결 요청"))
    elif filed:
        out.append((receipt, "done", "완료"))
    else:
        out.append((receipt, "todo", "고객 동의 후"))
    return out
