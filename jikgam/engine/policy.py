"""정책: 판별 결과와 시스템 사실로 다음 행동과 도구 호출을 정하는 코드 규칙. docs/engine.md §5.

val 기준선에서 판별 모델이 가장 약했던 질문은 next_info와 action이었다. 두 질문의 정답은 대부분 정해진 우선순위
규칙이므로, 아래 규칙을 위에서부터 적용하고 아무 규칙도 정하지 못할 때만 판별 결과의 action과 next_info를 쓴다.

 1. 업무 API가 인계 사유를 돌려줌 → handoff, create_handoff
 2. 업무 API 장애(재시도 예산 소진) → hold
 3. 발화에 인증번호가 있고 전화번호를 앎 → verify_identity
 4. 본인 확인 전이고 요청 처리에 고객 기록이 필요함 → ask(본인 확인)
 5. 본인 확인 직후 → 조회 API 병렬 호출
 6. 환불·해지 요청이고 계산 입력이 확인됨 → quote_refund(그럴듯한 사유를 병렬로 묻고 코드로 고른다)
 7. 계좌 환불이 필요한데 계좌가 없음 → ask(계좌)
 8. 접수 내용을 안내했고 고객이 동의함 → 쓰기 API
 9. 접수할 내용이 정해짐 → answer(안내하고 동의를 묻는다)
10. 그 밖 → 판별 결과의 action과 next_info

금액과 자격은 quote_refund 결과만 쓴다(ADR 0004). 이 모듈은 금액을 계산하지 않고, 견적 사유를 고를 때도
업무 API가 낸 eligible과 조직 설정의 우선순위만 본다. 날짜 비교(결제 당시 나이)처럼 결정적인 일만 코드로 한다.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .org import PolicyConfig
from .state import QuoteOutcome, SessionState


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]
    target: str | None = None  # 쓰기의 멱등 키 대상


@dataclass(frozen=True)
class Proposal:
    """고객 동의를 받은 뒤 실행할 쓰기 묶음. signature가 같아야 동의한 내용과 실행할 내용이 같다."""

    kind: str  # refund_cancel | refund | cancel
    writes: tuple[ToolCall, ...]
    signature: str
    quote_reason: str | None
    summary: dict[str, Any]


@dataclass(frozen=True)
class PolicyInput:
    intent: str | None
    action: str | None  # 판별 결과
    next_info: str | None  # 판별 결과
    agreed: bool  # 이번 발화가 안내한 접수 내용에 대한 동의인가(판별, 임계값 적용 후)
    pending: Proposal | None  # 유효한 마지막 제안에 붙은 접수 내용


@dataclass
class Plan:
    rule: str
    action: str  # answer | ask | handoff | hold
    next_info: str | None = None
    ask: str | None = None  # 무엇을 물을지: phone, otp, otp_retry, account, own_account, account_holder, plan_type
    calls: tuple[ToolCall, ...] = ()  # 병렬 조회
    lookups: bool = False  # 본인 확인 직후 조회 묶음
    quote_reasons: tuple[str, ...] = ()  # 병렬 견적
    writes: tuple[ToolCall, ...] = ()  # 순서대로 실행하고 취소하지 않는다
    proposal: Proposal | None = None
    notices: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)  # 생성 입력과 로그에 남길 근거

    @property
    def has_tool_work(self) -> bool:
        return bool(self.calls or self.lookups or self.quote_reasons or self.writes)

    def log(self) -> dict[str, Any]:
        return {"rule": self.rule, "action": self.action, "next_info": self.next_info, "ask": self.ask,
                "calls": [c.tool for c in self.calls], "lookups": self.lookups, "quote_reasons": list(self.quote_reasons),
                "writes": [c.tool for c in self.writes],
                "proposal": self.proposal.signature if self.proposal else None, "detail": self.detail}


# --- 시스템 사실 도우미 ------------------------------------------------------------------------


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def target_subscription(state: SessionState, cfg: PolicyConfig) -> tuple[dict | None, bool]:
    """처리 대상 구독과 모호 여부. 부가 상품을 뺀 유효 구독이 하나면 그것이다."""
    subs = [s for s in state.facts.subscriptions or [] if s.get("plan") not in cfg.addon_plans]
    active = [s for s in subs if s.get("status", "active") == "active"] or subs
    if len(active) == 1:
        return active[0], False
    return None, len(active) > 1


def current_payment(state: SessionState, sub_id: str) -> dict | None:
    """기준일 현재 이용 기간의 결제(그 구독의 기준일 이전 결제 중 가장 최근)."""
    pays = [p for p in state.facts.payments or []
            if p.get("subscription_id") == sub_id and date.fromisoformat(p["date"]) <= state.today]
    return max(pays, key=lambda p: p["date"]) if pays else None


def incident_window(state: SessionState) -> tuple[str, str]:
    """장애 기록 조회 구간: 유효 구독의 현재 결제일 중 가장 이른 날부터 기준일까지. 결제가 없으면 최근 30일."""
    starts = []
    for s in state.facts.subscriptions or []:
        p = current_payment(state, s["subscription_id"])
        if p:
            starts.append(p["date"])
    start = min(starts) if starts else (state.today - timedelta(days=30)).isoformat()
    return start, state.today.isoformat()


def plausible_reasons(intent: str | None, sub: dict | None, state: SessionState, cfg: PolicyConfig) -> list[str]:
    """조직 설정의 요청별 견적 사유 중 시스템 사실로 보아 물어볼 만한 것. 자격 판단은 하지 않는다(업무 API가 한다)."""
    if sub is None or intent not in cfg.quote_reasons:
        return []
    pay = current_payment(state, sub["subscription_id"])
    pays = [p for p in state.facts.payments or [] if p.get("subscription_id") == sub["subscription_id"]]
    same_day = len({p["date"] for p in pays}) < len(pays)
    claimed_many = any(int(r.value.rstrip("회")) >= 2 for r in state.claims.get("payment_count", []))
    birth = (state.facts.customer or {}).get("birth")
    out = []
    for reason in cfg.quote_reasons[intent]:
        if reason == "withdrawal":
            ok = pay is not None
        elif reason == "incident":
            ok = pay is not None and bool(state.facts.incidents)
        elif reason == "annual_cancel":
            ok = pay is not None and sub.get("plan") == "annual"
        elif reason == "duplicate":
            ok = same_day or (claimed_many and bool(pays))
        elif reason == "minor":
            ok = pay is not None and birth is not None and \
                age_on(date.fromisoformat(birth), date.fromisoformat(pay["date"])) < cfg.minor_age
        elif reason == "trial_cancel":
            ok = bool(sub.get("trial"))
        elif reason == "plan_change":
            ok = pay is not None and sub.get("plan") == "monthly"
        else:
            ok = False
        if ok:
            out.append(reason)
    return out


def choose_quote(reasons: list[str], quotes: dict[str, QuoteOutcome], cfg: PolicyConfig) -> QuoteOutcome | None:
    """대상(eligible)인 견적 중 조직 설정 우선순위가 가장 앞인 것. 대상이 없으면 None."""
    eligible = [quotes[r] for r in reasons if r in quotes and quotes[r].status == "eligible"]
    if not eligible:
        return None
    return min(eligible, key=lambda q: cfg.quote_priority.index(q.reason))


def _signature(writes: list[ToolCall], summary: dict[str, Any]) -> str:
    """쓰기 인자와 안내 내용(금액, 해지 대상 등)이 모두 같아야 같은 접수 내용이다."""
    raw = json.dumps([[[w.tool, w.target, sorted(w.args.items())] for w in writes], summary], ensure_ascii=False,
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _mask(number: str) -> str:
    digits = number.replace("-", "")
    return "*" * (len(digits) - 3) + digits[-3:]


@dataclass
class _Draft:
    writes: list[ToolCall]
    summary: dict[str, Any]
    ask: str | None = None  # 계좌가 필요한데 받을 수 없을 때 무엇을 물을지


def _draft(intent: str | None, sub: dict | None, chosen: QuoteOutcome | None, reasons: list[str],
           state: SessionState, cfg: PolicyConfig) -> _Draft:
    """요청과 견적으로 접수할 쓰기 묶음을 만든다. 이미 접수된 것은 뺀다. 순서: 계좌 등록 → 환불 → 해지."""
    f = state.facts
    cid = state.identity.customer_id
    writes: list[ToolCall] = []
    summary: dict[str, Any] = {}
    refund = chosen is not None and chosen.reason in cfg.refundable_reasons and \
        intent in (cfg.refund_intents | cfg.cancel_intents)
    if refund:
        q = chosen.quote or {}
        lines = [ln for ln in q.get("refunds", []) if f"refund:{ln['payment_id']}" not in f.receipts]
        summary["refund"] = {"reason": chosen.reason, "quote_id": q.get("quote_id"),
                             "refund_amount": q.get("refund_amount"), "approval_required": q.get("approval_required"),
                             "last_usable_date": q.get("last_usable_date"), "lines": lines}
        needs_account = (any(ln.get("account_required") for ln in lines) or f.account_required) \
            and "account" not in f.receipts
        if lines and needs_account:
            acc = state.account
            holder = None
            if acc and acc.holder_name:
                holder = acc.holder_name
            elif acc and acc.holder_self and f.customer:
                holder = f.customer.get("name")
            rejected = acc is not None and any(r["account"] == acc.number and r["holder"] == holder
                                               for r in f.account_rejections)
            if acc is None or not acc.number or not acc.bank:
                return _Draft([], summary, "own_account" if f.account_rejections else "account")
            if rejected:
                return _Draft([], summary, "own_account")
            if holder is None:
                return _Draft([], summary, "account_holder")
            writes.append(ToolCall("register_refund_account",
                                   {"customer_id": cid, "bank": acc.bank, "account": acc.number, "holder": holder},
                                   target=f"account:{cid}"))
            summary["register_account"] = {"bank": acc.bank, "account": _mask(acc.number), "holder": holder}
        for ln in lines:
            writes.append(ToolCall("create_refund_request",
                                   {"customer_id": cid, "payment_id": ln["payment_id"], "quote_id": q["quote_id"]},
                                   target=f"refund:{ln['payment_id']}"))
    elif intent in cfg.refund_intents and reasons:
        summary["refund_unavailable"] = {r: f.quotes[r].message for r in reasons if r in f.quotes}
    if intent in cfg.cancel_intents and sub is not None and f"cancel:{sub['subscription_id']}" not in f.receipts \
            and sub.get("status", "active") == "active":
        writes.append(ToolCall("create_cancellation", {"customer_id": cid, "subscription_id": sub["subscription_id"]},
                               target=f"cancel:{sub['subscription_id']}"))
        summary["cancel"] = {"subscription_id": sub["subscription_id"], "plan": sub.get("plan"),
                             "next_billing": sub.get("next_billing"),
                             "last_usable_date": (chosen.quote or {}).get("last_usable_date") if refund else None}
    return _Draft(writes, summary)


# --- 규칙 --------------------------------------------------------------------------------------


def decide(state: SessionState, inp: PolicyInput, cfg: PolicyConfig, required_notices: tuple[str, ...]) -> Plan:
    f = state.facts
    ident = state.identity
    intent = inp.intent

    # 1. 인계
    if f.handoff_reason:
        if "handoff" not in f.receipts:
            args = {"reason": f.handoff_reason, "summary": f"요청 {intent or '미정'}, 인계 사유 {f.handoff_reason}"}
            if ident.customer_id:
                args["customer_id"] = ident.customer_id
            return Plan("handoff", "handoff", writes=(ToolCall("create_handoff", args, target=f"handoff:{f.handoff_reason}"),),
                        detail={"handoff_reason": f.handoff_reason})
        return Plan("handoff", "handoff", detail={"handoff_reason": f.handoff_reason})

    # 2. 업무 API 장애
    if f.api_failures:
        return Plan("api_failure", "hold", detail={"api_failures": dict(f.api_failures)})

    # 3. 본인 확인 호출
    cur = state.current_claims()
    if not ident.verified and cur.otps:
        if state.phone is None:
            return Plan("identity_required", "ask", "identity", ask="phone")
        otp = cur.otps[-1]
        if (state.phone, otp) not in ident.tried:
            return Plan("verify_identity", "ask", "identity",
                        calls=(ToolCall("verify_identity", {"phone": state.phone, "otp": otp}),))

    # 4. 본인 확인 요청
    if not ident.verified and intent in cfg.records_intents:
        if state.phone is None:
            ask = "phone"
        else:
            ask = "otp_retry" if ident.failed else "otp"
        return Plan("identity_required", "ask", "identity", ask=ask)

    if ident.verified:
        # 5. 조회
        if not f.records_loaded:
            return Plan("lookups", "ask", lookups=True)

        # 6. 견적
        sub, ambiguous = target_subscription(state, cfg)
        if intent in cfg.quote_reasons and ambiguous:
            return Plan("target_ambiguous", "ask", "plan_type", ask="plan_type")
        reasons = plausible_reasons(intent, sub, state, cfg)
        missing = tuple(r for r in reasons if r not in f.quotes)
        if missing:
            return Plan("quote", "answer", quote_reasons=missing, detail={"reasons": reasons})
        chosen = choose_quote(reasons, f.quotes, cfg)
        detail = {"reasons": reasons, "chosen": chosen.reason if chosen else None}

        if intent in (cfg.refund_intents | cfg.cancel_intents):
            draft = _draft(intent, sub, chosen, reasons, state, cfg)
            # 7. 계좌
            if draft.ask:
                return Plan("account_required", "ask", "account", ask=draft.ask, detail={**detail, **draft.summary})
            if draft.writes:
                kinds = {w.tool for w in draft.writes}
                kind = "refund_cancel" if {"create_refund_request", "create_cancellation"} <= kinds else \
                    "refund" if "create_refund_request" in kinds else "cancel"
                proposal = Proposal(kind, tuple(draft.writes), _signature(draft.writes, draft.summary),
                                    chosen.reason if chosen else None, draft.summary)
                # 8. 동의한 내용 그대로면 접수
                if inp.agreed and inp.pending is not None and inp.pending.signature == proposal.signature:
                    return Plan("writes", "answer", writes=proposal.writes, proposal=proposal,
                                notices=required_notices, detail=detail)
                # 9. 안내하고 동의를 묻는다
                return Plan("proposal", "answer", proposal=proposal, notices=required_notices, detail=detail)
            received = {k: v for k, v in f.receipts.items() if k.startswith(("refund:", "cancel:"))}
            if received:
                return Plan("received", "answer", notices=required_notices, detail={**detail, "receipts": list(received)})
            if intent in cfg.refund_intents:
                return Plan("not_eligible", "answer", detail={**detail, **draft.summary})

    # 10. 판별 결과
    if inp.action is None:
        return Plan("decision_unavailable", "hold")
    return Plan("decision", inp.action, inp.next_info,
                ask=inp.next_info if inp.action == "ask" else None)
