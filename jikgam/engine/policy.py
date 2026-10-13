"""정책: 판별 결과와 시스템 사실로 다음 행동과 도구 호출을 정하는 코드 규칙. docs/engine.md §5.

val 기준선에서 판별 모델이 가장 약했던 질문은 next_info와 action이었다. 두 질문의 정답은 대부분 정해진 우선순위
규칙이므로, 아래 규칙을 위에서부터 적용하고 아무 규칙도 정하지 못할 때만 판별 결과의 action과 next_info를 쓴다.

 0. 통화자가 제3자(가족·지인)이고 계정 처리 요청이거나 본인 확인 정보를 말함 → answer(거절과 일반 안내).
    본인 확인을 묻지도 하지도 않는다(제3자에게 인증번호를 받아 확인하는 우회도 막는다).
 1. 업무 API가 인계 사유를 돌려줌 → handoff, create_handoff
 2. 업무 API 장애(재시도 예산 소진) → hold
 3. 발화에 인증번호가 있고 전화번호를 앎 → verify_identity
 4. 본인 확인 전이고 고객이 본인 확인을 마칠 수 없다고 말함 → hold
 5. 본인 확인 전이고 요청 처리에 고객 기록이 필요함 → ask(본인 확인)
 6. 본인 확인 직후 → 조회 API 병렬 호출
 7. 통화자가 법정대리인 → 법정대리인이 요청할 수 있는 사유(미성년자)가 시스템 사실로 맞으면 그 사유만 견적
    (업무 API가 인계를 돌려주면 1번이 인계한다), 아니면 0번과 같이 거절
 8. 해지 대상이 부가 상품뿐 → 견적 없이 부가 상품 해지 안내(근거 조항은 조직 설정), 동의하면 해지 접수
 9. 환불·해지 요청이고 계산 입력이 확인됨 → quote_refund(그럴듯한 사유를 병렬로 묻고 코드로 고른다)
10. 계좌 환불이 필요한데 계좌가 없음 → ask(계좌)
11. 접수 내용을 안내했고 고객이 동의함 → 쓰기 API
12. 안내한 접수를 고객이 원하지 않음 → answer(접수하지 않는다고 확인, 다시 묻지 않음)
13. 접수할 내용이 정해짐 → answer(안내하고 동의를 묻는다)
14. 그 밖 → 판별 결과의 action과 next_info

금액과 자격은 quote_refund 결과만 쓴다(ADR 0004). 이 모듈은 금액을 계산하지 않고, 견적 사유를 고를 때도
업무 API가 낸 eligible과 조직 설정의 우선순위만 본다. 날짜 비교(결제 당시 나이)처럼 결정적인 일만 코드로 한다.
Plan.articles는 규칙이 근거로 삼는 조항(조직 설정)이다. 판별의 applies와 관계없이 생성 입력에 넣는다.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .org import PolicyConfig
from .state import GUARDIAN, THIRD_PARTY, QuoteOutcome, SessionState


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
    declined: bool = False  # 이번 발화가 안내한 접수를 원하지 않는다는 뜻인가(판별, 임계값 적용 후)
    identity_unavailable: bool = False  # 고객이 본인 확인을 마칠 수 없다고 말했는가(판별, 임계값 적용 후)


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
    articles: tuple[str, ...] = ()  # 이 규칙의 근거 조항(조직 설정). 생성 입력에 반드시 넣는다
    detail: dict[str, Any] = field(default_factory=dict)  # 생성 입력과 로그에 남길 근거

    @property
    def has_tool_work(self) -> bool:
        return bool(self.calls or self.lookups or self.quote_reasons or self.writes)

    def log(self) -> dict[str, Any]:
        return {"rule": self.rule, "action": self.action, "next_info": self.next_info, "ask": self.ask,
                "calls": [c.tool for c in self.calls], "lookups": self.lookups, "quote_reasons": list(self.quote_reasons),
                "writes": [c.tool for c in self.writes],
                "proposal": self.proposal.signature if self.proposal else None, "articles": list(self.articles),
                "detail": self.detail}


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


def addon_subscription(state: SessionState, cfg: PolicyConfig) -> dict | None:
    """유효한 부가 상품 구독이 하나면 그것."""
    addons = [s for s in state.facts.subscriptions or []
              if s.get("plan") in cfg.addon_plans and s.get("status", "active") == "active"]
    return addons[0] if len(addons) == 1 else None


def _within_window(pay: dict, state: SessionState, cfg: PolicyConfig) -> bool:
    """결제일 다음 날을 1일째로 세어 기간 마지막 날까지인가. 기간 설정이 없으면 거르지 않는다."""
    if cfg.window_days is None:
        return True
    return state.today <= date.fromisoformat(pay["date"]) + timedelta(days=cfg.window_days)


def _plausible(reason: str, sub: dict, state: SessionState, cfg: PolicyConfig) -> bool:
    """이 견적 사유를 물어볼 만한가(시스템 사실로 거른다). 자격 판단은 하지 않는다(업무 API가 한다)."""
    pay = current_payment(state, sub["subscription_id"])
    if reason in ("withdrawal", "incident"):
        # 날짜로 이미 불가능한 사유는 묻지 않는다(설계 원칙: 날짜 비교는 코드로). 기간 판단만 하고 자격은 업무 API가 정한다.
        if pay is None or not _within_window(pay, state, cfg):
            return False
        return reason == "withdrawal" or bool(state.facts.incidents)
    if reason == "annual_cancel":
        return pay is not None and sub.get("plan") == "annual"
    if reason == "duplicate":
        pays = [p for p in state.facts.payments or [] if p.get("subscription_id") == sub["subscription_id"]]
        same_day = len({p["date"] for p in pays}) < len(pays)
        claimed_many = any(int(r.value.rstrip("회")) >= 2 for r in state.claims.get("payment_count", []))
        return same_day or (claimed_many and bool(pays))
    if reason == "minor":
        birth = (state.facts.customer or {}).get("birth")
        return pay is not None and birth is not None and \
            age_on(date.fromisoformat(birth), date.fromisoformat(pay["date"])) < cfg.minor_age
    if reason == "trial_cancel":
        return bool(sub.get("trial"))
    if reason == "plan_change":
        return pay is not None and sub.get("plan") == "monthly"
    return False


def plausible_reasons(intent: str | None, sub: dict | None, state: SessionState, cfg: PolicyConfig) -> list[str]:
    """조직 설정의 요청별 견적 사유 중 시스템 사실로 보아 물어볼 만한 것."""
    if sub is None or intent not in cfg.quote_reasons:
        return []
    return [r for r in cfg.quote_reasons[intent] if _plausible(r, sub, state, cfg)]


def choose_quote(reasons: list[str], quotes: dict[str, QuoteOutcome], cfg: PolicyConfig) -> QuoteOutcome | None:
    """대상(eligible)인 견적 중 조직 설정 우선순위가 가장 앞인 것. 대상이 없으면 None."""
    eligible = [quotes[r] for r in reasons if r in quotes and quotes[r].status == "eligible"]
    if not eligible:
        return None
    return min(eligible, key=lambda q: cfg.quote_priority.index(q.reason))


def _signature(writes: list[ToolCall], summary: dict[str, Any]) -> str:
    """쓰기 인자와 안내한 환불 금액이 모두 같아야 같은 접수 내용이다. 설명용 필드(환불 불가 사유 등)는 넣지 않는다."""
    refund = summary.get("refund") or {}
    money = [refund.get("refund_amount"), [ln.get("amount") for ln in refund.get("lines", [])]]
    raw = json.dumps([[[w.tool, w.target, sorted(w.args.items())] for w in writes], money], ensure_ascii=False,
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
    # 환불 접수는 고객이 환불을 요청했거나, 해지 자체가 환불을 수반하는 사유(연 구독 중도 해지)일 때만 넣는다.
    # 해지만 요청했는데 청약철회처럼 환불이 가능하면 안내만 한다(환불하면 이용 기간이 바로 끝나므로 고객이 정한다).
    refund = chosen is not None and chosen.reason in cfg.refundable_reasons and \
        (intent in cfg.refund_intents or (intent in cfg.cancel_intents and chosen.reason in cfg.cancel_refund_reasons))
    if chosen is not None and not refund:
        summary["refund_available"] = {"reason": chosen.reason, "refund_amount": (chosen.quote or {}).get("refund_amount"),
                                       "message": chosen.message}
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
    elif chosen is None and reasons:
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


def _proposal_plan(proposal: Proposal, inp: PolicyInput, required_notices: tuple[str, ...], articles: tuple[str, ...],
                   detail: dict[str, Any]) -> Plan:
    """접수 내용이 정해졌을 때: 동의했으면 쓰기, 원하지 않았으면 접수하지 않음, 아니면 안내하고 동의를 묻는다."""
    same = inp.pending is not None and inp.pending.signature == proposal.signature
    # 11. 동의한 내용 그대로면 접수
    if inp.agreed and same:
        return Plan("writes", "answer", writes=proposal.writes, proposal=proposal, notices=required_notices,
                    articles=articles, detail=detail)
    # 12. 안내한 접수를 원하지 않음: 접수하지 않고, 이번 발화에서는 같은 내용을 다시 묻지 않는다
    if inp.declined and same:
        return Plan("declined", "answer", articles=articles,
                    detail={**detail, **proposal.summary, "declined": proposal.kind})
    # 13. 안내하고 동의를 묻는다
    return Plan("proposal", "answer", proposal=proposal, notices=required_notices, articles=articles, detail=detail)


def _refusal(cfg: PolicyConfig, reason: str) -> Plan:
    """제3자 요청 거절(제15조 제3항). 계정 정보는 생성 입력에서 뺀다(session._generation_payload)."""
    return Plan("third_party", "answer", articles=cfg.third_party_articles, detail={"refusal": reason})


def _addon_plan(addon: dict, inp: PolicyInput, state: SessionState, cfg: PolicyConfig,
                required_notices: tuple[str, ...]) -> Plan:
    """부가 상품만 해지. 견적 사유가 없으므로 견적을 부르지 않고 조직 설정의 근거 조항(환불 없음, 다음 결제일 종료)으로
    안내한다. 이용 마지막 날은 해지 접수 결과(업무 API)의 값을 쓴다."""
    sid = addon["subscription_id"]
    key = f"cancel:{sid}"
    detail = {"addon_only": True}
    if key in state.facts.receipts:
        return Plan("received", "answer", notices=required_notices, articles=cfg.addon_articles,
                    detail={**detail, "receipts": [key]})
    writes = [ToolCall("create_cancellation", {"customer_id": state.identity.customer_id, "subscription_id": sid},
                       target=key)]
    summary = {"cancel": {"subscription_id": sid, "plan": addon.get("plan"), "price": addon.get("price"),
                          "next_billing": addon.get("next_billing"), "addon_only": True},
               "refund_none_articles": list(cfg.addon_articles)}
    proposal = Proposal("cancel", tuple(writes), _signature(writes, summary), None, summary)
    return _proposal_plan(proposal, inp, required_notices, cfg.addon_articles, detail)


def decide(state: SessionState, inp: PolicyInput, cfg: PolicyConfig, required_notices: tuple[str, ...]) -> Plan:
    f = state.facts
    ident = state.identity
    intent = inp.intent
    cur = state.current_claims()

    # 0. 제3자: 계정 처리 요청이거나 본인 확인 정보를 말하면 거절한다. 본인 확인 규칙(3~5)보다 앞선다.
    if state.caller == THIRD_PARTY:
        if intent in cfg.records_intents or cur.otps or cur.phones:
            return _refusal(cfg, THIRD_PARTY)
        return _decision_plan(inp)

    guardian = state.caller == GUARDIAN
    who_articles = cfg.guardian_articles if guardian else ()

    # 1. 인계
    if f.handoff_reason:
        if "handoff" not in f.receipts:
            summary = f"요청 {intent or '미정'}, 인계 사유 {f.handoff_reason}"
            if guardian:
                summary += ", 통화자 법정대리인"
            if not ident.customer_id and state.phone:  # 본인 확인에 실패하면 업무 API가 고객 ID를 주지 않는다
                summary += f", 고객이 말한 번호 {state.phone}"
            args = {"reason": f.handoff_reason, "summary": summary}
            if ident.customer_id:
                args["customer_id"] = ident.customer_id
            return Plan("handoff", "handoff", writes=(ToolCall("create_handoff", args, target=f"handoff:{f.handoff_reason}"),),
                        articles=who_articles, detail={"handoff_reason": f.handoff_reason})
        return Plan("handoff", "handoff", articles=who_articles, detail={"handoff_reason": f.handoff_reason})

    # 2. 업무 API 장애
    if f.api_failures:
        return Plan("api_failure", "hold", detail={"api_failures": dict(f.api_failures)})

    # 3. 본인 확인 호출
    if not ident.verified and cur.otps:
        if state.phone is None:
            return Plan("identity_required", "ask", "identity", ask="phone", articles=who_articles)
        otp = cur.otps[-1]
        if (state.phone, otp) not in ident.tried:
            return Plan("verify_identity", "ask", "identity",
                        calls=(ToolCall("verify_identity", {"phone": state.phone, "otp": otp}),))

    if not ident.verified and intent in cfg.records_intents:
        # 4. 본인 확인을 마칠 수 없다고 함(등록된 번호가 아님, 인증번호를 받을 수 없음): 계정을 확인할 수 없어 보류.
        #    이번 발화에 번호나 인증번호가 있으면 판별과 관계없이 본인 확인을 계속한다(s29 try1: 인증번호를 말한 발화에
        #    identity_unavailable 0.53).
        if inp.identity_unavailable and not (cur.otps or cur.phones):
            return Plan("identity_unavailable", "hold", "identity",
                        detail={"identity_failed_attempts": ident.failed, "phone_given": state.phone is not None})
        # 5. 본인 확인 요청
        if state.phone is None:
            ask = "phone"
        else:
            ask = "otp_retry" if ident.failed else "otp"
        return Plan("identity_required", "ask", "identity", ask=ask, articles=who_articles)

    if ident.verified:
        # 6. 조회
        if not f.records_loaded:
            return Plan("lookups", "ask", lookups=True)

        # 7. 법정대리인: 제13조 사유만 요청할 수 있다. 사유가 맞으면 견적(업무 API가 인계를 돌려준다), 아니면 거절
        if guardian:
            sub, _ = target_subscription(state, cfg)
            reasons = [r for r in cfg.guardian_reasons if sub is not None and _plausible(r, sub, state, cfg)]
            missing = tuple(r for r in reasons if r not in f.quotes)
            if missing:
                return Plan("quote", "answer", quote_reasons=missing, articles=who_articles,
                            detail={"reasons": reasons})
            return _refusal(cfg, "guardian_not_applicable")

        # 8. 부가 상품만 해지
        addon = addon_subscription(state, cfg) if state.addon_only and intent in cfg.addon_intents else None
        if addon is not None:
            return _addon_plan(addon, inp, state, cfg, required_notices)

        # 9. 견적
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
            # 10. 계좌
            if draft.ask:
                return Plan("account_required", "ask", "account", ask=draft.ask, detail={**detail, **draft.summary})
            if draft.writes:
                kinds = {w.tool for w in draft.writes}
                kind = "refund_cancel" if {"create_refund_request", "create_cancellation"} <= kinds else \
                    "refund" if "create_refund_request" in kinds else "cancel"
                proposal = Proposal(kind, tuple(draft.writes), _signature(draft.writes, draft.summary),
                                    chosen.reason if chosen else None, draft.summary)
                # 11~13. 접수, 접수하지 않음, 안내
                return _proposal_plan(proposal, inp, required_notices, (), detail)
            received = {k: v for k, v in f.receipts.items() if k.startswith(("refund:", "cancel:"))}
            if received:
                return Plan("received", "answer", notices=required_notices, detail={**detail, "receipts": list(received)})
            if intent in cfg.refund_intents:
                return Plan("not_eligible", "answer", detail={**detail, **draft.summary})

    # 14. 판별 결과
    return _decision_plan(inp)


def _decision_plan(inp: PolicyInput) -> Plan:
    if inp.action is None:
        return Plan("decision_unavailable", "hold")
    return Plan("decision", inp.action, inp.next_info,
                ask=inp.next_info if inp.action == "ask" else None)
