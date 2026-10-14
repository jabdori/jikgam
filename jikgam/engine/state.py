"""세션 상태: 대화, 시스템 사실, 고객 진술, 본인 확인, 요청, 근거, 제안, 대화 버전. docs/engine.md §3.

- 시스템 사실은 업무 API 결과만 담는다. 고객 진술은 따로 두고 판별 상태에서 값 뒤에 "(고객 진술)"을 붙인다(ADR 0003).
- 판별 상태의 known_facts는 마지막 고객 발화 **이전**까지의 사실이다. 진술은 출처 발화와 함께 쌓아 두고, 상태를 만들 때
  마지막 고객 발화에서 나온 진술을 뺀다. 그래서 같은 발화로 판단을 다시 돌려도(도구 결과로 버전이 올라 3단계부터 다시 할 때)
  마지막 발화의 진술은 known_facts에 들어가지 않는다.
- request에는 직전 판단의 intent 라벨을 둔다.
- caller는 통화자와 계정의 관계다(engine-extra 질문 caller에 코드 규칙을 더한 값, session._update_caller).
  owner(본인이거나 아니라는 단서 없음), third_party(가족·지인), guardian(미성년 회원의 법정대리인).
- addon_only는 해지 대상이 부가 상품뿐이라는 마지막 판단이다(부가 상품 구독이 조회됐을 때만 묻는다).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from .bizapi import ToolResult
from .claims import AccountClaim, Claims
from .retrieval import Article

if TYPE_CHECKING:
    from .policy import Proposal

CLAIM_MARK = "(고객 진술)"

# 통화자와 계정의 관계(engine-extra 질문 caller의 선택지)
OWNER, THIRD_PARTY, GUARDIAN = "owner", "third_party", "guardian"


@dataclass
class Turn:
    utt_id: str
    speaker: str
    text: str
    final: bool
    version: int


@dataclass
class ClaimRecord:
    key: str
    value: str
    utt_id: str
    version: int


@dataclass
class Identity:
    verified: bool = False
    customer_id: str | None = None
    attempts: int = 0
    failed: int = 0
    tried: set[tuple[str, str]] = field(default_factory=set)  # 이미 보낸 (전화번호, 인증번호)
    tried_utt: dict[tuple[str, str], str] = field(default_factory=dict)  # (전화번호, 인증번호) → 처음 보낸 발화
    unavailable: bool = False  # 본인 확인을 마칠 수 없다고 판단해 보류함. 새 번호·인증번호가 오면 지운다


@dataclass
class QuoteOutcome:
    """견적 한 사유의 결과. status: eligible | zero(대상이지만 0원) | not_eligible | handoff | failed."""

    reason: str
    status: str
    quote: dict[str, Any] | None = None
    message: str | None = None
    article_ids: tuple[str, ...] = ()


@dataclass
class SystemFacts:
    customer: dict[str, Any] | None = None  # None은 아직 조회하지 않음
    subscriptions: list[dict[str, Any]] | None = None
    payments: list[dict[str, Any]] | None = None
    usage: list[dict[str, Any]] | None = None
    incidents: list[dict[str, Any]] | None = None
    quotes: dict[str, QuoteOutcome] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)  # refund:<결제>, cancel:<구독>, account, handoff
    account_rejections: list[dict[str, Any]] = field(default_factory=list)
    account_required: bool = False  # 접수 시 ACCOUNT_REQUIRED를 받음
    handoff_reason: str | None = None
    api_failures: dict[str, str] = field(default_factory=dict)  # 재시도 예산을 다 쓴 도구 → 오류 코드
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def records_loaded(self) -> bool:
        return None not in (self.customer, self.subscriptions, self.payments, self.usage, self.incidents)


@dataclass
class Evidence:
    version: int
    candidates: list[str]
    selected: list[str]
    applies: dict[str, float]
    sufficient: bool | None
    next_info: str | None
    action: str | None
    intent: str | None
    # 상담 상태로 붙인 절차 조항(조직 설정 retrieval.rules: 본인 확인, 안내 의무, 승인, 인계 등). 판별의 applies와
    # 관계없이 응답의 근거 집합에 들어간다(docs/engine.md §12)
    attached: list[str] = field(default_factory=list)


@dataclass
class Suggestion:
    id: str
    basis_version: int
    text: str
    action: str
    intent: str | None
    rule: str
    cited_articles: list[str]
    amount: int | None
    amount_final: bool
    proposal: "Proposal | None" = None
    status: str = "active"  # active | invalidated | superseded
    exposed: bool = False


class SessionState:
    def __init__(self, org: str, today: date):
        self.org = org
        self.today = today
        self.version = 0
        self.conversation: list[Turn] = []
        self.claims: dict[str, list[ClaimRecord]] = {}  # 진술 키 → 기록(오래된 것부터)
        self.claims_by_utt: dict[str, Claims] = {}
        self.phone: str | None = None  # 가장 최근에 말한 전화번호
        self.account: AccountClaim | None = None  # 여러 발화에 걸쳐 합친 계좌 진술
        self.identity = Identity()
        self.facts = SystemFacts()
        self.request: str | None = None
        self.caller: str = OWNER
        self.addon_only: bool = False
        self.evidence: Evidence | None = None
        self.suggestions: list[Suggestion] = []
        # 답하지 못한 앞 발화에서 넘겨받은 인증번호 (발화 ID, 인증번호). 그 발화의 파이프라인이 본인 확인을 시작하기 전에
        # 새 발화로 취소되면 세션이 넘겨 둔다(빠른 정정, docs/engine.md §13). 본인 확인을 시작하거나 답을 내면 지운다.
        self.carried_otp: tuple[str, str] | None = None

    # --- 대화와 진술 ---

    def add_turn(self, utt_id: str, speaker: str, text: str, final: bool = True) -> None:
        self.conversation.append(Turn(utt_id, speaker, text, final, self.version))

    def last_customer_utt_id(self) -> str | None:
        return next((t.utt_id for t in reversed(self.conversation) if t.speaker == "customer"), None)

    def customer_texts(self) -> list[str]:
        return [t.text for t in self.conversation if t.speaker == "customer"]

    def add_claims(self, utt_id: str, claims: Claims) -> None:
        self.claims_by_utt[utt_id] = claims
        for key, value in claims.keys.items():
            self.claims.setdefault(key, []).append(ClaimRecord(key, value, utt_id, self.version))
        if claims.phones:
            self.phone = claims.phones[-1]
        for acc in claims.accounts:
            prev = self.account
            self.account = acc if prev is None else AccountClaim(
                text=acc.text, bank=acc.bank or prev.bank, number=acc.number or prev.number,
                holder_name=acc.holder_name or (None if acc.holder_self or acc.holder_relation else prev.holder_name),
                holder_self=acc.holder_self or (prev.holder_self and not (acc.holder_name or acc.holder_relation)),
                holder_relation=acc.holder_relation or (None if acc.holder_self or acc.holder_name else prev.holder_relation))

    def current_claims(self) -> Claims:
        uid = self.last_customer_utt_id()
        return self.claims_by_utt.get(uid, Claims()) if uid else Claims()

    def otp_to_verify(self) -> tuple[str, str] | None:
        """본인 확인에 쓸 (발화 ID, 인증번호). 이번 발화의 마지막 인증번호, 없으면 답하지 못한 앞 발화에서 넘겨받은 것."""
        cur = self.current_claims()
        if cur.otps:
            return self.last_customer_utt_id() or "", cur.otps[-1]
        return self.carried_otp

    # --- 제안 ---

    def latest_suggestion(self) -> Suggestion | None:
        """채널에 낸 마지막 제안이 아직 유효하면 그것. 판별 상태의 previous_suggestion이 된다."""
        s = next((s for s in reversed(self.suggestions) if s.exposed), None)
        return s if s and s.status == "active" else None

    # --- 도구 결과 반영 ---

    def apply_tool_result(self, r: ToolResult) -> bool:
        """업무 API 결과를 시스템 사실에 반영한다. 사실이 바뀌었으면 True."""
        f = self.facts
        if r.exhausted:
            f.api_failures[r.tool] = r.error_code or "UNAVAILABLE"
            if r.tool == "quote_refund":
                f.quotes[r.args["reason"]] = QuoteOutcome(r.args["reason"], "failed", message=r.error_code)
            return True
        if not r.ok:
            return self._apply_error(r)
        d = r.data or {}
        if r.tool == "verify_identity":
            self.identity.attempts = int(d.get("attempts", self.identity.attempts + 1))
            self.identity.failed = int(d.get("failed_attempts", self.identity.failed))
            if d.get("verified"):
                self.identity.verified, self.identity.customer_id = True, d["customer_id"]
            if d.get("handoff_required"):
                f.handoff_reason = "identity_failed"
        elif r.tool == "get_customer":
            f.customer = d
        elif r.tool == "list_subscriptions":
            f.subscriptions = list(d.get("subscriptions", []))
        elif r.tool == "list_payments":
            f.payments = list(d.get("payments", []))
        elif r.tool == "get_usage":
            f.usage = list(d.get("usage", []))
        elif r.tool == "list_incidents":
            f.incidents = list(d.get("incidents", []))
        elif r.tool == "quote_refund":
            reason = r.args["reason"]
            f.quotes[reason] = QuoteOutcome(reason, "eligible" if d.get("eligible") else "zero", d, d.get("message"),
                                            tuple(d.get("article_ids", ())))
            if d.get("handoff_required"):
                f.handoff_reason = d.get("handoff_reason") or "handoff"
        elif r.tool == "create_refund_request":
            f.receipts[f"refund:{d['payment_id']}"] = d
        elif r.tool == "create_cancellation":
            for sid in d.get("cancelled_subscription_ids") or [d["subscription_id"]]:
                f.receipts[f"cancel:{sid}"] = d
        elif r.tool == "register_refund_account":
            if d.get("registered"):
                f.receipts["account"] = d
            else:
                f.account_rejections.append({"holder": r.args.get("holder"), "account": r.args.get("account"),
                                             "reason": d.get("reject_reason"), "message": d.get("message")})
        elif r.tool == "create_handoff":
            f.receipts["handoff"] = d
        return True

    def _apply_error(self, r: ToolResult) -> bool:
        f = self.facts
        err = r.error or {}
        code = r.error_code
        if code == "HANDOFF_REQUIRED":
            quote = err.get("quote") or {}
            if r.tool == "quote_refund":
                f.quotes[r.args["reason"]] = QuoteOutcome(r.args["reason"], "handoff", quote, err.get("message"),
                                                          tuple(err.get("article_ids", ())))
            f.handoff_reason = quote.get("handoff_reason") or ("identity_failed" if r.tool != "quote_refund" else "handoff")
        elif code == "NOT_ELIGIBLE" and r.tool == "quote_refund":
            f.quotes[r.args["reason"]] = QuoteOutcome(r.args["reason"], "not_eligible", err.get("quote"),
                                                      err.get("message"), tuple(err.get("article_ids", ())))
        elif code == "DUPLICATE_REQUEST" and err.get("receipt"):
            rec = err["receipt"]
            if r.tool == "create_refund_request":
                f.receipts[f"refund:{rec['payment_id']}"] = rec
            elif r.tool == "create_cancellation":
                for sid in rec.get("cancelled_subscription_ids") or [rec["subscription_id"]]:
                    f.receipts[f"cancel:{sid}"] = rec
            elif r.tool == "register_refund_account":
                f.receipts["account"] = rec
        elif code == "ACCOUNT_REQUIRED":
            f.account_required = True
        elif code == "IDENTITY_REQUIRED":
            self.identity.verified = False
        elif code == "INVALID_REQUEST":  # api.md: 엔진 오류로 기록하고 상담사 인계
            f.errors.append({"tool": r.tool, "code": code, "message": err.get("message")})
            f.handoff_reason = "engine_error"
        else:
            f.errors.append({"tool": r.tool, "code": code, "message": err.get("message")})
        return True

    # --- 판별 상태 ---

    def known_facts(self) -> dict[str, Any]:
        """마지막 고객 발화 이전까지의 사실. 시스템 값은 그대로, 고객 진술은 값 뒤에 (고객 진술)."""
        kf: dict[str, Any] = {}
        f = self.facts
        if self.identity.customer_id:
            kf["customer_id"] = self.identity.customer_id
        if f.customer:
            kf["name"] = f.customer.get("name")
            kf["birth"] = f.customer.get("birth")
            kf["account_status"] = f.customer.get("status")
        if f.subscriptions is not None:
            kf["subscriptions"] = [{k: v for k, v in s.items() if v not in (None, False) or k in ("promo", "trial")}
                                   for s in f.subscriptions]
        if f.payments is not None:
            kf["payments"] = f.payments
        if f.usage is not None:
            kf["usage"] = [f"{u['date']} {u['type']}" + ("(체험 기간)" if u.get("during_trial") else "") for u in f.usage]
        if f.incidents is not None:
            kf["incidents"] = [f"{i['incident_id']} {i['start']}~{i['end']} {i['hours']:g}시간 귀책 {i['cause']}"
                               for i in f.incidents]
        if f.quotes:
            kf["refund_quotes"] = {r: quote_summary(q) for r, q in f.quotes.items()}
        if f.receipts:
            kf["receipts"] = {k: {x: v for x, v in rec.items() if x not in ("article_ids", "customer_id")}
                              for k, rec in f.receipts.items()}
        if f.account_rejections:
            kf["refund_account_rejected"] = [r["reason"] for r in f.account_rejections]
        if self.identity.failed:
            kf["identity_failed_attempts"] = self.identity.failed
        if f.handoff_reason:
            kf["handoff_reason"] = f.handoff_reason
        if f.api_failures:
            kf["api_failures"] = dict(f.api_failures)
        last = self.last_customer_utt_id()
        for key, records in self.claims.items():
            before = [r for r in records if r.utt_id != last]
            if before:
                kf[key] = before[-1].value + CLAIM_MARK
        if self.request:
            kf["request"] = self.request
        return kf

    def decision_state(self, articles: list[Article], previous_suggestion: str | None) -> dict[str, Any]:
        """판별 모델에 보내는 상태. 판별 데이터의 state와 같은 형식이다(docs/data-format.md)."""
        state: dict[str, Any] = {
            "org": self.org,
            "today": self.today.isoformat(),
            "identity_verified": self.identity.verified,
            "conversation": [{"speaker": t.speaker, "text": t.text, "final": t.final} for t in self.conversation],
            "known_facts": self.known_facts(),
        }
        if previous_suggestion:
            state["previous_suggestion"] = previous_suggestion
        state["articles"] = [{"id": a.id, "title": a.title, "text": a.text} for a in articles]
        return state


def quote_summary(q: QuoteOutcome) -> dict[str, Any]:
    """판별 상태와 생성 입력에 넣는 견적 요약. 계산 내역은 금액 근거 설명에 필요하므로 둔다."""
    out: dict[str, Any] = {"status": q.status}
    quote = q.quote or {}
    for k in ("quote_id", "eligible", "refund_amount", "approval_required", "handoff_required", "handoff_reason",
              "last_usable_date"):
        if k in quote and quote[k] not in (None, ""):
            out[k] = quote[k]
    if quote.get("refunds"):
        out["refunds"] = [{k: line[k] for k in ("payment_id", "amount", "refund_method", "account_required",
                                                 "expected_period") if k in line} for line in quote["refunds"]]
    if quote.get("breakdown"):
        out["breakdown"] = quote["breakdown"]
    if q.message:
        out["message"] = q.message
    if q.article_ids:
        out["article_ids"] = list(q.article_ids)
    return out
