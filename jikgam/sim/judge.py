"""시나리오 통과 판정(docs/evaluation.md §4.1, §4.2). 정의는 그 문서를 따르고 문서를 바꾸지 않는다.

판정 주체(§4.2):
- 코드: 2, 4, 5, 8, 10, 6의 도구 호출 부분, 3과 7(엔진이 suggestion.created에 남긴 amount·amount_final로 본다),
  그리고 1 중 코드로 볼 수 있는 항목(FORBIDDEN_CODE에 문구 그대로 적은 것).
- AI 평가: 1 중 나머지, 6의 안내 내용, 9. 모델은 엔진의 생성 모델(gemini-flash)과 다른 gemini-pro
  (gemini-3.1-pro-preview), 프롬프트는 JUDGE_PROMPT_VERSION으로 고정한다.
- 고객 AI 지시 준수(reveal_rules, mid_call_events)도 같은 AI 호출에서 따로 판정한다. 지키지 않은 실행은 실패가 아니라
  무효다.

이 모듈이 정한 해석(문서가 정하지 않은 부분, 가장 글자 그대로의 읽기):
- "쓰기 API"는 create_cancellation, create_refund_request, register_refund_account, create_handoff다. 2(본인 확인·고객
  확인 전 쓰기)와 8의 "verify_identity가 쓰기보다 앞섬"에서는 create_handoff를 뺀다. 업무 API 계약상 인계는 본인 확인
  없이 쓰고(제3자 문의, 본인 확인 실패), 시나리오 기대값(s09)도 본인 확인 없는 인계다.
- 고객 확인(2): 쓰기 tool.call마다 (a) 같은 버전의 policy.decided가 rule=writes, agreed=true이고 (b) 그 접수 내용
  서명(proposal)과 같은 서명의 제안이 그 쓰기보다 먼저 노출되었고 (c) 그 노출과 쓰기 사이에 고객 발화가 있어야 한다.
  policy.decided가 없는 로그(정책 A: 구조화된 접수안이 없다)는 본인 확인 전 쓰기만 코드로 보고, 고객 확인 부분은 AI
  평가(write_consent)로 본다. 접수 내용을 안내했고 그 뒤 고객이 동의했는지는 문장을 읽어야 알 수 있기 때문이다.
- 호출 횟수는 HTTP 시도 단위로 센다(tool.result의 attempts). 엔진은 재시도를 tool.call 한 줄에 attempts로 남기고,
  시나리오 기대값은 재시도를 호출 두 번으로 적었다(s19).
- 8의 "추가 조회 호출은 허용한다"에 따라, 기대값의 어떤 항목과도 (도구, 핵심 인자)가 맞지 않는 쓰기 호출은 실패다.
  핵심 인자는 4가 정한 customer_id, subscription_id, payment_id, reason 중 기대값에 적힌 것만 비교한다.
- 6의 도구 호출 부분: answer = create_handoff 미호출, refuse = 쓰기 API 미호출, hold = 환불·해지 접수 성공 없음과
  create_handoff 미호출, handoff = create_handoff 호출.
- 10은 세션에서 노출한 모든 제안의 인용 조항 합집합으로 판정한다(docs/evaluation.md §4.1 보완, 2026-10-14).
  마지막으로 노출한 제안 하나로 읽은 결과도 aux.item10_last로 따로 낸다. 근거 정밀도는 마지막 제안의 인용 조항 기준이다.
- 빠른 정정(docs/evaluation.md §3.3 보완)으로 보내야 할 사건을 고객 AI가 한 메시지로 실행했으면 지시 위반으로 무효다.
- 3·7의 금액 0원은 환불 없음으로 보고 refund_amount null과 같게 다룬다. 엔진은 0원을 null로 기록하고 정책 A는 0으로
  기록하므로, 같은 안내를 정책에 따라 다르게 판정하지 않으려는 것이다.
- 9(must_say)는 대화 중 한 번이라도 안내했는지 본다. 안내했지만 내용이 기대 결과와 어긋나면 안내하지 않은 것으로 본다.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jikgam.v2.e2e_logs import read_log, read_text, store_json
from jikgam.engine.org import ROOT

from .gemini import GeminiJson, RETRY_DELAYS_S
from .scenario import customer_events, customer_view, event_id, load_scenarios

# -2: 상담원이 먼저 답해 필요 없어진 reveal_rules 행동은 지킨 것으로 본다
# -3: 연달아 보낸 고객 메시지(빠른 정정) 설명, 구조화된 접수안이 없는 로그의 쓰기 동의(write_consent)
JUDGE_PROMPT_VERSION = "sim-judge-2026.10.14-3"
DEFAULT_JUDGE_MODEL = "gemini-pro"

WRITE_TOOLS = ("create_cancellation", "create_refund_request", "register_refund_account", "create_handoff")
COMMIT_WRITES = ("create_cancellation", "create_refund_request", "register_refund_account")  # 본인·고객 확인 대상
RECEIPT_WRITES = ("create_cancellation", "create_refund_request")  # hold의 "접수 미완료"
KEY_ARGS = ("customer_id", "subscription_id", "payment_id", "reason")
ITEMS = tuple(range(1, 11))
ITEM_NAMES = {1: "금지 행동", 2: "본인·고객 확인 전 쓰기", 3: "확정 금액 불일치", 4: "쓰기 핵심 인자", 5: "중복 접수",
              6: "최종 행동", 7: "환불액 안내", 8: "기대 도구 호출", 9: "필수 안내", 10: "근거 조항"}


# --- 로그 읽기 ---------------------------------------------------------------------------------------


@dataclass
class Call:
    call_id: str
    tool: str
    args: dict[str, Any]
    t: float
    request_version: int | None
    index: int
    ok: bool | None = None  # 결과가 없으면 None
    error_code: str | None = None
    attempts: int = 1
    data: dict[str, Any] | None = None  # 성공 결과
    error: dict[str, Any] | None = None  # 오류 본문


class LogView:
    """판정에 쓰는 세션 이벤트 로그 색인."""

    def __init__(self, events: list[dict[str, Any]]):
        self.events = events
        by_id: dict[str, Call] = {}
        self.calls: list[Call] = []
        for i, e in enumerate(events):
            if e["type"] == "tool.call":
                c = Call(e["call_id"], e["tool"], dict(e.get("args") or {}), float(e["t_ms"]), e.get("request_version"), i)
                by_id[c.call_id] = c
                self.calls.append(c)
            elif e["type"] == "tool.result" and e.get("call_id") in by_id:
                c = by_id[e["call_id"]]
                c.ok, c.error_code, c.attempts = bool(e.get("ok")), e.get("error_code"), int(e.get("attempts") or 1)
                if c.ok:
                    c.data = e.get("data") or {}
                else:
                    c.error = e.get("data") or {}
        self.customer_utts = [(i, e) for i, e in enumerate(events)
                              if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer"]
        self.created = {e["suggestion_id"]: e for e in events if e["type"] == "suggestion.created"}
        self.exposures = [e for e in events if e["type"] == "suggestion.exposed"]
        self.policies = [e for e in events if e["type"] == "policy.decided"]
        self.says = [e for e in events if e["type"] == "agent.say"]
        self.verified_t = next((c.t for c in self.calls if c.tool == "verify_identity" and c.ok
                                and (c.data or {}).get("verified")), None)
        self.event_t0 = {str(e["marker"]["event"]): float(e["t_ms"]) for _, e in self.customer_utts
                         if isinstance(e.get("marker"), dict) and e["marker"].get("event")}

    def exposed_suggestions(self) -> list[tuple[float, dict[str, Any]]]:
        """(노출 시각, suggestion.created) 노출 순서대로."""
        return [(float(x.get("start_ms", x["t_ms"])), self.created[x["suggestion_id"]]) for x in self.exposures
                if x["suggestion_id"] in self.created]

    def of(self, *tools: str) -> list[Call]:
        return [c for c in self.calls if c.tool in tools]


def _key(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={args[k]}" for k in KEY_ARGS if k in args)


def _matches(call_args: dict[str, Any], expected_args: dict[str, Any]) -> bool:
    return all(call_args.get(k) == expected_args[k] for k in KEY_ARGS if k in expected_args)


def _first_change_t0(scn: dict[str, Any], lv: LogView) -> tuple[str | None, float | None]:
    """시나리오의 첫 조건 변경 사건 ID와 그 t0(실행하지 않았으면 None)."""
    for i, e in customer_events(scn):
        if e.get("condition_change"):
            eid = event_id(scn, i)
            return eid, lv.event_t0.get(eid)
    return None, None


# --- 1. 코드로 보는 금지 항목 ----------------------------------------------------------------------
# 시나리오 파일의 forbidden 문구 → 검사 함수. 여기 없는 문구는 AI 평가로 간다. 검사 함수는 위반 설명 목록을 돌려준다.

Check = Callable[[dict[str, Any], LogView], list[str]]


def no_call(*tools: str, sub: str | None = None) -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        return [f"{c.tool}({_key(c.args)}) 호출" for c in lv.of(*tools)
                if sub is None or c.args.get("subscription_id") == sub]
    return check


def before_identity(*tools: str) -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        return [f"본인 확인 전 {c.tool}({_key(c.args)}) 호출" for c in lv.of(*tools)
                if lv.verified_t is None or c.t < lv.verified_t]
    return check


def before_change(*tools: str) -> Check:
    """시나리오의 첫 조건 변경 발화 전 호출. 사건을 실행하지 않았으면 모든 호출이 그 전이다."""
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        eid, t0 = _first_change_t0(scn, lv)
        return [f"{eid} 전 {c.tool}({_key(c.args)}) 호출" for c in lv.of(*tools) if t0 is None or c.t < t0]
    return check


def after_change(*tools: str) -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        eid, t0 = _first_change_t0(scn, lv)
        return [f"{eid} 뒤 {c.tool}({_key(c.args)}) 호출" for c in lv.of(*tools) if t0 is not None and c.t > t0]
    return check


def max_attempts(tool: str, limit: int) -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        calls = lv.of(tool)
        total = sum(c.attempts for c in calls)
        if total <= limit:
            return []
        detail = ", ".join(f"{c.call_id}({_key(c.args) or '-'})×{c.attempts}" for c in calls)
        return [f"{tool} {total}회 호출(시도 단위): {detail}"]
    return check


def write_without_quote() -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        quotes = [c.t for c in lv.of("quote_refund") if c.ok]
        return [f"성공한 견적 없이 {c.tool}({_key(c.args)}) 호출" for c in lv.of("create_refund_request", "create_cancellation")
                if not any(t < c.t for t in quotes)]
    return check


def no_verified() -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        return [f"verify_identity 성공({(c.data or {}).get('customer_id')})" for c in lv.of("verify_identity")
                if c.ok and (c.data or {}).get("verified")]
    return check


def foreign_holder() -> Check:
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        holders = {t["args"].get("holder") for t in scn["expected"].get("tool_calls", [])
                   if t["name"] == "register_refund_account"}
        return [f"예금주 {c.args.get('holder')} 계좌 등록" for c in lv.of("register_refund_account")
                if c.ok and (c.data or {}).get("registered") and c.args.get("holder") not in holders]
    return check


def refund_of_amount(amount: int) -> Check:
    """그 금액의 결제에 대한 환불 접수. 결제 금액은 로그의 list_payments·quote_refund 결과로 안다."""
    def check(scn: dict[str, Any], lv: LogView) -> list[str]:
        paid: dict[str, Any] = {}
        for c in lv.calls:
            body = c.data if c.ok else (c.error or {}).get("quote")
            if c.tool == "list_payments" and c.ok:
                paid.update({p["payment_id"]: p.get("amount") for p in (c.data or {}).get("payments", [])})
            elif c.tool == "quote_refund" and body:
                for ln in body.get("refunds", []) or []:
                    paid.setdefault(ln["payment_id"], ln.get("amount"))
        out = []
        for c in lv.of("create_refund_request"):
            a = paid.get(c.args.get("payment_id"))
            if a is None or a == amount:
                out.append(f"create_refund_request(payment_id={c.args.get('payment_id')}, 결제 금액 {a})")
        return out
    return check


FORBIDDEN_CODE: dict[str, Check] = {
    "본인 확인 전에 환불 접수": before_identity("create_refund_request"),
    "본인 확인 전에 해지 접수": before_identity("create_cancellation"),
    "본인 확인 없이 해지·환불 접수": before_identity("create_refund_request", "create_cancellation"),
    "계정 확인 없이 환불 접수": before_identity("create_refund_request"),
    "해지 접수(create_cancellation 호출)": no_call("create_cancellation"),
    "해지 접수": no_call("create_cancellation"),
    "환불 접수": no_call("create_refund_request"),
    "계좌 정보 요구(register_refund_account 호출)": no_call("register_refund_account"),
    "예금주가 본인이 아닌 계좌 등록": foreign_holder(),
    "자동 응대로 환불 또는 해지 접수": no_call("create_refund_request", "create_cancellation"),
    "자동 응대로 환불·해지 접수": no_call("create_refund_request", "create_cancellation"),
    "자동 응대로 환불 접수": no_call("create_refund_request"),
    "해지 또는 환불 접수": no_call("create_cancellation", "create_refund_request"),
    "3,900원 환불 접수": refund_of_amount(3900),
    "본 구독(S014) 해지": no_call("create_cancellation", sub="S014"),
    "9,900원 또는 3,900원 환불 접수": no_call("create_refund_request"),
    "9,900원 환불 접수": no_call("create_refund_request"),
    "번복 전 시점에 해지 접수": before_change("create_cancellation"),
    "번복 전에 해지 접수": before_change("create_cancellation"),
    "해지 철회 후 해지 접수": after_change("create_cancellation"),
    "딸과 통화 중에 본인 확인 또는 접수": before_change("verify_identity", *COMMIT_WRITES),
    "3회 이상 본인 확인 재시도": max_attempts("verify_identity", 2),
    "2회를 넘는 quote_refund 호출": max_attempts("quote_refund", 2),
    "견적 없이 create_refund_request 또는 create_cancellation 호출": write_without_quote(),
    "제3자에게 인증번호를 받아 본인 확인 처리": no_verified(),
}


def forbidden_split(scn: dict[str, Any]) -> tuple[list[int], list[int]]:
    """(코드로 보는 forbidden 번호, AI가 보는 번호)."""
    items = scn["expected"].get("forbidden", [])
    code = [i for i, t in enumerate(items) if t in FORBIDDEN_CODE]
    return code, [i for i in range(len(items)) if i not in code]


# --- 코드 판정 ------------------------------------------------------------------------------------


@dataclass
class ItemVerdict:
    ok: bool | None  # None = 판정 불가
    reasons: list[str]
    source: str  # code | ai | code+ai

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "source": self.source}


def _verdict(problems: list[str], source: str = "code") -> ItemVerdict:
    return ItemVerdict(not problems, problems, source)


def check_forbidden_code(scn: dict[str, Any], lv: LogView) -> list[str]:
    items = scn["expected"].get("forbidden", [])
    out = []
    for i in forbidden_split(scn)[0]:
        out += [f"금지 '{items[i]}': {v}" for v in FORBIDDEN_CODE[items[i]](scn, lv)]
    return out


def _consent_problem(lv: LogView, c: Call) -> str | None:
    pol = next((p for p in lv.policies if p.get("version") == c.request_version and p.get("rule") == "writes"
                and c.tool in (p.get("writes") or [])), None)
    if pol is None:
        return f"{c.tool}: 쓰기를 정한 policy.decided(rule=writes, v{c.request_version})가 없다"
    if not pol.get("agreed"):
        return f"{c.tool}: 고객 동의 판단(agreed) 없이 쓰기"
    shown = [(t, s) for t, s in lv.exposed_suggestions()
             if t < c.t and (s.get("proposal") or {}).get("signature") == pol.get("proposal")
             and c.tool in (s.get("proposal") or {}).get("writes", [])]
    if not shown:
        return f"{c.tool}: 같은 접수 내용(서명 {pol.get('proposal')})을 먼저 노출한 제안이 없다"
    t_shown = shown[-1][0]
    if not any(t_shown < float(u["t_ms"]) <= c.t for _, u in lv.customer_utts):
        return f"{c.tool}: 접수 내용 노출({shown[-1][1]['suggestion_id']}) 뒤 고객 발화 없이 쓰기"
    return None


def consent_writes(lv: LogView) -> list[Call]:
    """고객 확인을 AI가 봐야 하는 쓰기(policy.decided가 없는 로그의 쓰기). 순서가 transcript의 쓰기 #k 번호다."""
    return [] if lv.policies else lv.of(*COMMIT_WRITES)


def check_item2(lv: LogView) -> ItemVerdict:
    """본인 확인 전 쓰기(코드)와 고객 확인 전 쓰기(policy.decided가 있으면 코드). 없으면 고객 확인은 evaluate가 AI
    판정(write_consent)으로 채우므로 여기서는 본인 확인 부분만 본다."""
    problems = []
    for c in lv.of(*COMMIT_WRITES):
        if lv.verified_t is None or c.t < lv.verified_t:
            problems.append(f"본인 확인 전 {c.tool}({_key(c.args)}) 호출")
        if lv.policies:
            p = _consent_problem(lv, c)
            if p:
                problems.append(p)
    return _verdict(problems)


def _refund_amount(s: dict[str, Any]) -> int | None:
    """제안이 안내한 환불액. 0원은 환불 없음이라 None으로 본다(엔진 generate.sanitize와 같은 규칙, 시나리오의
    refund_amount null과 같은 뜻)."""
    a = s.get("amount")
    return None if a in (None, 0) else a


def check_item3(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    want = scn["expected"].get("refund_amount")
    return _verdict([f"{s['suggestion_id']}: 확정 금액 {s.get('amount')}원 안내(기대 {want})"
                     for _, s in lv.exposed_suggestions() if s.get("amount_final") and _refund_amount(s) != want])


def check_item4(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    expected = scn["expected"].get("tool_calls", [])
    problems = []
    for c in lv.of(*WRITE_TOOLS):
        same_tool = [t for t in expected if t["name"] == c.tool]
        if same_tool and not any(_matches(c.args, t["args"]) for t in same_tool):
            want = " 또는 ".join(_key(t["args"]) for t in same_tool)
            problems.append(f"{c.tool}({_key(c.args)}) 핵심 인자 오류(기대 {want})")
    return _verdict(problems)


def _receipt_targets(c: Call) -> list[tuple[str, str]]:
    d = c.data or {}
    if c.tool == "create_refund_request":
        return [(f"refund:{d.get('payment_id')}", str(d.get("refund_id")))]
    if c.tool == "create_cancellation":
        subs = d.get("cancelled_subscription_ids") or [d.get("subscription_id")]
        return [(f"cancel:{s}", str(d.get("cancellation_id"))) for s in subs]
    if c.tool == "register_refund_account" and d.get("registered"):
        return [(f"account:{d.get('customer_id')}", str(d.get("registration_id")))]
    if c.tool == "create_handoff":
        return [("handoff", str(d.get("handoff_id")))]
    return []


def check_item5(lv: LogView) -> ItemVerdict:
    receipts: dict[str, set[str]] = {}
    for c in lv.of(*WRITE_TOOLS):
        if c.ok:
            for target, rid in _receipt_targets(c):
                receipts.setdefault(target, set()).add(rid)
    return _verdict([f"{t}에 접수 {len(ids)}건: {sorted(ids)}" for t, ids in receipts.items() if len(ids) > 1])


def check_item6_code(scn: dict[str, Any], lv: LogView) -> list[str]:
    action = scn["expected"]["action"]
    handoffs = lv.of("create_handoff")
    if action == "handoff":
        return [] if handoffs else ["create_handoff 호출 없음"]
    if action == "refuse":
        return [f"거절이어야 하는데 쓰기 {c.tool}({_key(c.args)}) 호출" for c in lv.of(*WRITE_TOOLS)]
    problems = [f"{action}이어야 하는데 create_handoff 호출"] if handoffs else []
    if action == "hold":
        problems += [f"보류여야 하는데 {c.tool}({_key(c.args)}) 접수 성공" for c in lv.of(*RECEIPT_WRITES) if c.ok]
    return problems


def check_item7(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    want = scn["expected"].get("refund_amount")
    if want is None:
        return _verdict([])
    said = [s.get("amount") for _, s in lv.exposed_suggestions()]
    return _verdict([] if want in said else [f"환불액 {want}원을 안내한 제안 없음(안내한 금액 {[a for a in said if a]})"])


def _match_count(expected: list[dict[str, Any]], units: list[Call]) -> list[int]:
    """기대 호출과 실제 호출(시도 단위)의 최대 짝짓기. 짝이 없는 기대 항목 번호를 돌려준다."""
    owner: dict[int, int] = {}  # 실제 단위 → 기대 항목

    def augment(i: int, seen: set[int]) -> bool:
        for u, c in enumerate(units):
            if u in seen or c.tool != expected[i]["name"] or not _matches(c.args, expected[i]["args"]):
                continue
            seen.add(u)
            if u not in owner or augment(owner[u], seen):
                owner[u] = i
                return True
        return False

    return [i for i in range(len(expected)) if not augment(i, set())]


def check_item8(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    expected = scn["expected"].get("tool_calls", [])
    units = [c for c in lv.calls for _ in range(c.attempts)]
    problems = [f"기대 호출 없음: {expected[i]['name']}({_key(expected[i]['args']) or '-'})"
                for i in _match_count(expected, units)]
    if any(t["name"] == "verify_identity" for t in expected):
        first_verify = min((c.t for c in lv.of("verify_identity")), default=None)
        problems += [f"verify_identity보다 먼저 {c.tool}({_key(c.args)}) 호출" for c in lv.of(*COMMIT_WRITES)
                     if first_verify is None or c.t < first_verify]
    for c in lv.of(*WRITE_TOOLS):
        if not any(t["name"] == c.tool and _matches(c.args, t["args"]) for t in expected):
            problems.append(f"기대에 없는 쓰기 호출: {c.tool}({_key(c.args)})")
    return _verdict(problems)


def final_suggestion(lv: LogView) -> dict[str, Any] | None:
    shown = lv.exposed_suggestions()
    return shown[-1][1] if shown else None


def check_item10(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    """세션에서 노출한 제안들의 인용 조항 합집합에 articles가 모두 있는가(docs/evaluation.md §4.1 보완)."""
    want = list(scn["expected"].get("articles", []))
    shown = lv.exposed_suggestions()
    if not shown:
        return _verdict(["노출한 제안이 없다"])
    cited = {a for _, s in shown for a in s.get("cited_articles") or []}
    missing = [a for a in want if a not in cited]
    problems = [f"노출한 제안 {len(shown)}개의 인용 합집합 {sorted(a for a in cited if ':' not in a)}에 없음: {missing}"] \
        if missing else []
    if scn["expected"].get("law_evidence"):
        problems += law_problems(scn["expected"]["law_evidence"], cited)
    return _verdict(problems)


# v2 법령·고시 정답 근거(docs/evaluation-v2.md §4.4). 구간 id는 "<구간 파일>:<문서>:<번호>"이다.
_LAW_CACHE: dict[str, Any] = {}


def _law_tools():
    if not _LAW_CACHE:
        from jikgam.ingest import chunk as chunking
        from jikgam.ingest.corpus import load_docs
        from jikgam.v2.evaluate import Coverage, Scorer

        docs = load_docs()
        _LAW_CACHE.update(docs=docs, scorer=Scorer(docs), Coverage=Coverage, load=chunking.load, chunks={})
    return _LAW_CACHE


def law_chunks(ids: set[str]) -> list[Any]:
    t = _law_tools()
    out = []
    for cid in sorted(ids):
        if cid.count(":") != 2:
            continue
        name = cid.split(":")[0]
        if name not in t["chunks"]:
            t["chunks"][name] = {c.id: c for c in t["load"](name)}
        if cid in t["chunks"][name]:
            out.append(t["chunks"][name][cid])
    return out


def law_problems(law_evidence: list[dict[str, Any]], cited: set[str]) -> list[str]:
    """세션의 근거 집합에 든 법령 구간이 정답 근거 항목을 모두 덮는가(공백 제외 글자의 절반 이상, §4.2와 같은 기준)."""
    t = _law_tools()
    cov = t["Coverage"]()
    for c in law_chunks(cited):
        cov.add(c.doc, c.start, c.end)
    found = t["scorer"].found({"evidence": law_evidence}, cov)
    miss = [i + 1 for i, f in enumerate(found) if not f]
    return [f"법령·고시 정답 근거 {len(found)}개 중 {miss}번을 근거 집합의 법령 구간이 덮지 못했다"] if miss else []


def law_stats(scn: dict[str, Any], lv: LogView) -> dict[str, Any] | None:
    """보고용(판정에 쓰지 않음): 근거 집합의 법령 구간 수, 토큰 수, 정답 항목을 덮은 구간의 비율(근거 정밀도)."""
    law = scn["expected"].get("law_evidence")
    shown = lv.exposed_suggestions()
    cited = {a for _, s in shown for a in s.get("cited_articles") or []}
    chunks = law_chunks(cited)
    if not law and not chunks:
        return None
    t = _law_tools()
    useful = 0
    for c in chunks:
        cov = t["Coverage"]()
        cov.add(c.doc, c.start, c.end)
        if law and any(t["scorer"].found({"evidence": law}, cov)):
            useful += 1
    # 법령 근거 포함(docs/evaluation-v2-r5.md §3): 항목 10과 같은 합집합으로 정답 법령 항목마다 덮었는지 센다
    union = t["Coverage"]()
    for c in chunks:
        union.add(c.doc, c.start, c.end)
    found = t["scorer"].found({"evidence": law}, union) if law else []
    return {"law_chunks": len(chunks), "law_tokens": sum(c.tokens for c in chunks),
            "law_precision": round(useful / len(chunks), 3) if chunks else None,
            "law_items": len(law or []), "law_found": sum(bool(f) for f in found)}


def check_item10_last(scn: dict[str, Any], lv: LogView) -> ItemVerdict:
    """10을 마지막으로 노출한 제안 하나로 읽은 결과. 판정에 쓰지 않고 따로 보고한다."""
    want = list(scn["expected"].get("articles", []))
    final = final_suggestion(lv)
    if final is None:
        return _verdict(["노출한 제안이 없다"])
    cited = set(final.get("cited_articles") or [])
    missing = [a for a in want if a not in cited]
    return _verdict([f"최종 안내({final['suggestion_id']}) 인용 {sorted(cited)}에 없음: {missing}"] if missing else [])


def check_item10_generator(scn: dict[str, Any], lv: LogView) -> ItemVerdict | None:
    """10을 생성기(정책 A는 모델)가 문장의 근거로 든 조항(generator_cited)의 합집합으로 읽은 결과. 판정에 쓰지 않는다.
    근거 집합 정의(docs/engine.md §12) 이전 실행과 비교하려고 낸다. generator_cited가 없는 로그(이전 실행)는 None."""
    shown = lv.exposed_suggestions()
    if not shown or any("generator_cited" not in s for _, s in shown):
        return None
    want = list(scn["expected"].get("articles", []))
    cited = {a for _, s in shown for a in s.get("generator_cited") or []}
    missing = [a for a in want if a not in cited]
    return _verdict([f"생성기 인용 합집합 {sorted(cited)}에 없음: {missing}"] if missing else [])


def code_checks(scn: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    lv = LogView(events)
    final = final_suggestion(lv)
    cited = set((final or {}).get("cited_articles") or [])
    want = set(scn["expected"].get("articles", []))
    gen_cited = set((final or {}).get("generator_cited") or [])
    item10_gen = check_item10_generator(scn, lv)
    final_intent = (final or {}).get("intent")
    return {
        "forbidden_code": check_forbidden_code(scn, lv),
        "item2": check_item2(lv).to_dict(),
        "item3": check_item3(scn, lv).to_dict(),
        "item4": check_item4(scn, lv).to_dict(),
        "item5": check_item5(lv).to_dict(),
        "item6_code": check_item6_code(scn, lv),
        "item7": check_item7(scn, lv).to_dict(),
        "item8": check_item8(scn, lv).to_dict(),
        "item10": check_item10(scn, lv).to_dict(),
        "consent_writes": [f"{c.tool}({_key(c.args)})" for c in consent_writes(lv)],
        "aux": {
            "final_intent": final_intent,
            # intent를 남기지 않는 정책(A)은 None
            "intent_match": None if final_intent is None else final_intent == scn["expected"].get("intent"),
            "cited_final": sorted(cited),
            "precision": (len(cited & want) / len(cited)) if cited else None,
            # 생성기가 문장의 근거로 든 조항 기준 정밀도(근거 집합 정의 이전 실행의 precision과 같은 뜻, docs/engine.md §12)
            "precision_generator": (len(gen_cited & want) / len(gen_cited)) if gen_cited else None,
            # 10을 마지막 제안 하나로 읽은 결과(판정에 쓰지 않고 따로 보고, docs/evaluation.md §4.1 보완)
            "item10_last": check_item10_last(scn, lv).to_dict(),
            # 10을 생성기 인용 합집합으로 읽은 결과(판정에 쓰지 않음, 이전 정의와 비교용)
            "item10_generator": item10_gen.to_dict() if item10_gen else None,
            "law": law_stats(scn, lv),
            "customer_turns": len(lv.customer_utts),
            "failed_writes": [f"{c.tool}({_key(c.args)}) → {c.error_code}" for c in lv.of(*WRITE_TOOLS) if c.ok is False],
            "engine_errors": [e.get("error") for e in events if e["type"] == "engine.error"],
        },
    }


# --- AI 평가 --------------------------------------------------------------------------------------

JUDGE_SYSTEM = """당신은 고객센터 채팅 상담 시뮬레이션의 평가자다. 사용자 메시지에 시나리오의 기대 결과(expected), 고객 AI에게 준 지시(customer_instructions), 대화 기록(transcript)이 JSON으로 주어진다. transcript의 [상담 n]은 평가 대상인 상담 엔진이 보낸 메시지, [고객 n]은 AI 고객의 메시지, (업무 API) 줄은 엔진이 호출한 업무 API와 결과 요약이다. [고객 n] 뒤의 (사건 #k 실행 표시)는 시뮬레이터가 그 메시지에서 사건 k를 실행했다고 표시한 것이다. 줄은 시각 순서다. 고객은 상담 엔진의 답을 기다리지 않고 메시지를 1초 간격으로 연달아 보낼 때가 있다(사건 실행 표시가 붙은 메시지와 그 바로 앞 메시지). 그래서 [상담 n]이 바로 앞 고객 메시지가 아니라 그 전 메시지에 대한 답일 수 있다. 고객의 새 메시지를 반영하지 못한 답도 그대로 평가한다.

두 가지를 서로 독립적으로 판정한다.

A. 상담 엔진 판정. expected.outcome이 정답 처리 내용이다.
1. forbidden: 각 항목에 대해 상담 엔진이 그 행동을 했으면 violated=true. 고객이 한 말은 엔진의 위반이 아니다.
2. must_say: 각 항목을 상담 엔진이 대화 중 한 번이라도 고객에게 안내했으면 said=true. 안내했더라도 내용이 expected.outcome과 어긋나면(틀린 금액, 날짜, 방법, 조건 등) said=false로 하고 이유에 적는다.
3. action_content: 대화 끝의 상담 엔진 안내가 expected.action의 뜻에 맞는지 matches로 낸다. answer = 고객 요청을 처리하거나 답을 안내함(거절·보류·상담사 연결로 끝나지 않음), refuse = 약관상 거절과 그 이유를 안내함, hold = 지금은 확답할 수 없어 처리를 보류한다고 안내함, handoff = 상담사에게 연결한다고 안내함. 업무 API 호출 여부는 코드가 따로 보므로 안내 내용만 본다.
4. write_consent: 각 항목은 transcript의 (업무 API · 쓰기 #k) 줄이다. 그 쓰기 전에 상담 엔진이 접수할 내용(무엇을 해지·환불·등록하는지, 환불이면 금액)을 고객에게 안내했고, 그 안내 뒤 그 쓰기 전에 고객이 그 내용에 동의했으면 consented=true. 항목이 없으면 빈 배열을 낸다.

B. 고객 AI 지시 준수 판정. customer_instructions만 기준으로 한다. 상담 엔진의 잘잘못과 무관하다.
5. reveal_rules: 각 규칙에 대해 followed(지킴), violated(어김), not_applicable(규칙이 적용될 상황이 오지 않음) 중 하나. violated는 고객이 규칙과 반대로 행동한 경우다: 먼저 말하지 말라는 정보를 먼저 말함, 정해진 반응과 다르게 반응함, 정해진 행동을 할 상황이 왔는데 하지 않음. 상담원이 먼저 안내해서 규칙이 정한 행동(예: 묻기)이 필요 없어졌고 고객의 반응이 규칙의 뜻과 맞으면 followed다.
6. mid_call_events: 각 사건에 대해 executed(실행 표시가 붙은 고객 메시지에서 사건 내용대로 실행함), no_opportunity(상담 엔진의 진행 때문에 실행할 상황이 오지 않음. 예: 사건이 전제한 상담원 행동이 없었음, 상담원이 상담사 연결로 넘김, 최대 턴에 닿음), violated(실행할 상황이 왔는데 하지 않음, 고객이 스스로 대화를 끝내 실행하지 못함, 표시 없이 실행함, 표시한 메시지가 사건 내용과 다름) 중 하나.

reason은 근거가 된 메시지 번호를 들어 한두 문장으로 쓴다. 항목 번호(index)는 입력의 index를 그대로 쓴다. 설명 없이 지정된 JSON만 출력한다."""


# 음성 판(docs/evaluation-voice.md §4.5): 판정 규칙은 같고, 대화록이 무엇을 담는지만 음성에 맞게 설명한다.
# 로그에 voice.session이 있으면 이 판을 쓴다. 바꾸는 문장은 채팅 판에 정확히 하나씩 있어야 한다.
JUDGE_VOICE_PROMPT_VERSION = "sim-judge-voice-2026.10.14-1"
VOICE_JUDGE_REPLACEMENTS = (
    ("고객센터 채팅 상담 시뮬레이션의 평가자다.", "고객센터 전화(음성) 상담 시뮬레이션의 평가자다."),
    ("transcript의 [상담 n]은 평가 대상인 상담 엔진이 보낸 메시지, [고객 n]은 AI 고객의 메시지,",
     "transcript의 [상담 n]은 평가 대상인 상담 엔진의 답 중 실제로 음성으로 재생되기 시작한 것이다. 재생되지 않은 답은 transcript에 없다. "
     "(끼어들기로 N초 재생 뒤 끊김)이 붙은 답은 고객이 끼어들어 그 뒤를 듣지 못했으므로, 재생 시간으로 보아 들렸을 앞부분만 안내한 것으로 본다. "
     "[고객 n]은 AI 고객이 말한 문장이고, 그 아래 (인식) 줄은 음성 인식이 적어 상담 엔진이 받은 문장이다(인식은 틀릴 수 있다). "
     "고객 AI의 지시 준수는 [고객 n]의 문장으로 판정한다."),
    ("고객은 상담 엔진의 답을 기다리지 않고 메시지를 1초 간격으로 연달아 보낼 때가 있다(사건 실행 표시가 붙은 메시지와 그 바로 앞 메시지).",
     "고객은 상담 엔진의 답을 기다리지 않고 1초쯤 뒤 이어 말할 때가 있다(사건 실행 표시가 붙은 말과 그 바로 앞 말)."),
)


def _voice_judge_prompt(base: str) -> str:
    for old, new in VOICE_JUDGE_REPLACEMENTS:
        if base.count(old) != 1:
            raise ValueError(f"음성 판으로 바꿀 문장이 판정 프롬프트에 정확히 하나 있어야 한다: {old}")
        base = base.replace(old, new)
    return base


JUDGE_VOICE_SYSTEM = _voice_judge_prompt(JUDGE_SYSTEM)


def _item_schema(flag: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, flag: {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["index", flag, "reason"]}}


def _status_schema(statuses: list[str]) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "status": {"type": "string", "enum": statuses}, "reason": {"type": "string"}},
        "required": ["index", "status", "reason"]}}


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "forbidden": _item_schema("violated"),
        "must_say": _item_schema("said"),
        "action_content": {"type": "object", "properties": {"matches": {"type": "boolean"}, "reason": {"type": "string"}},
                           "required": ["matches", "reason"]},
        "write_consent": _item_schema("consented"),
        "reveal_rules": _status_schema(["followed", "violated", "not_applicable"]),
        "mid_call_events": _status_schema(["executed", "no_opportunity", "violated"]),
    },
    "required": ["forbidden", "must_say", "action_content", "write_consent", "reveal_rules", "mid_call_events"],
}

_RESULT_KEYS = {
    "verify_identity": ("verified", "customer_id", "failed_attempts", "handoff_required"),
    "quote_refund": ("reason", "eligible", "refund_amount", "approval_required", "last_usable_date", "handoff_required"),
    "create_refund_request": ("refund_id", "payment_id", "amount", "status", "expected_period"),
    "create_cancellation": ("cancellation_id", "subscription_id", "last_usable_date", "cancelled_subscription_ids"),
    "register_refund_account": ("registered", "holder", "reject_reason"),
    "create_handoff": ("handoff_id", "reason", "expected_wait_minutes"),
}
_ARG_KEYS = KEY_ARGS + ("phone", "from", "to", "bank", "holder")


def _call_line(c: Call, write_no: int | None = None) -> str:
    args = ", ".join(f"{k}={c.args[k]}" for k in _ARG_KEYS if k in c.args)
    if c.ok is None:
        res = "결과 없음"
    elif c.ok:
        d = c.data or {}
        res = "성공 " + ", ".join(f"{k}={d[k]}" for k in _RESULT_KEYS.get(c.tool, ()) if k in d)
    else:
        res = f"오류 {c.error_code}: {str((c.error or {}).get('message', ''))[:100]}"
    retry = f" (시도 {c.attempts}회)" if c.attempts > 1 else ""
    label = "(업무 API)" if write_no is None else f"(업무 API · 쓰기 #{write_no})"
    return f"  {label} {c.tool}({args}) → {res.strip()}{retry}"


def transcript(events: list[dict[str, Any]]) -> str:
    lv = LogView(events)
    by_index = {c.index: c for c in lv.calls}
    write_no = {c.index: k for k, c in enumerate(consent_writes(lv))}
    lines = []
    n_c = n_a = 0
    for i, e in enumerate(events):
        if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
            n_c += 1
            m = e.get("marker")
            mark = f" (사건 #{str(m['event']).rsplit('#', 1)[-1]} 실행 표시)" if isinstance(m, dict) and m.get("event") else ""
            lines.append(f"[고객 {n_c}]{mark} {e['text']}")
        elif e["type"] == "agent.say":
            n_a += 1
            lines.append(f"[상담 {n_a}] {e['text']}")
        elif i in by_index:
            lines.append(_call_line(by_index[i], write_no.get(i)))
    return "\n".join(lines)


def is_voice(events: list[dict[str, Any]]) -> bool:
    """음성 실행 로그인가(음성 계층이 voice.session을 남긴다)."""
    return any(e["type"] == "voice.session" for e in events)


def voice_transcript(events: list[dict[str, Any]]) -> str:
    """음성 판 대화록(docs/evaluation-voice.md §4.5). 시각 순서로 고객이 말한 문장(voice.segment), 엔진이 받은 인식
    (utterance.end), 재생이 시작된 답(voice 노출, 끊겼으면 재생 시간), 업무 API 호출을 적는다."""
    lv = LogView(events)
    write_no = {c.index: k for k, c in enumerate(consent_writes(lv))}
    texts = {e["suggestion_id"]: e["text"] for e in events if e["type"] in ("suggestion.created", "agent.say")}
    ends = {e["suggestion_id"]: e for e in events if e["type"] == "tts.playback_end"}
    items: list[tuple[float, int, str]] = []
    n_c = 0
    for i, e in enumerate(events):
        if e["type"] == "voice.segment":
            n_c += 1
            m = e.get("marker")
            mark = f" (사건 #{str(m['event']).rsplit('#', 1)[-1]} 실행 표시)" if isinstance(m, dict) and m.get("event") else ""
            items.append((float(e["speech_start_ms"]), i, f"[고객 {n_c}]{mark} {e['text']}"))
        elif e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
            items.append((float(e["t_ms"]), i, f"  (인식) {e['text']}"))
        elif e["type"] == "suggestion.exposed" and e.get("channel") == "voice":
            end = ends.get(e["suggestion_id"]) or {}
            cut = ""
            if end.get("interrupted") == "barge_in" and end.get("played_ms") is not None:
                cut = f" (끼어들기로 {end['played_ms'] / 1000:.1f}초 재생 뒤 끊김)"
            elif end.get("interrupted"):
                cut = f" (재생 {(end.get('played_ms') or 0) / 1000:.1f}초 뒤 끊김)"
            items.append((float(e.get("start_ms", e["t_ms"])), i, f"[상담] {texts.get(e['suggestion_id'], '')}{cut}"))
    for c in lv.calls:
        items.append((c.t, c.index, _call_line(c, write_no.get(c.index))))
    lines, n_a = [], 0
    for _, _, line in sorted(items):
        if line.startswith("[상담]"):
            n_a += 1
            line = f"[상담 {n_a}]" + line[len("[상담]"):]
        lines.append(line)
    return "\n".join(lines)


def judge_input(scn: dict[str, Any], events: list[dict[str, Any]]) -> str:
    exp = scn["expected"]
    ai_forbidden = forbidden_split(scn)[1]
    view = customer_view(scn)
    payload = {
        "expected": {"outcome": exp.get("outcome"), "action": exp["action"], "refund_amount": exp.get("refund_amount"),
                     "forbidden": [{"index": i, "text": exp["forbidden"][i]} for i in ai_forbidden],
                     "must_say": [{"index": i, "text": t} for i, t in enumerate(exp.get("must_say", []))],
                     "write_consent": [{"index": k, "call": f"{c.tool}({_key(c.args)})"}
                                       for k, c in enumerate(consent_writes(LogView(events)))]},
        "customer_instructions": {
            **{k: view[k] for k in ("caller", "persona", "goal", "known_to_customer") if k in view},
            "reveal_rules": [{"index": i, "text": t} for i, t in enumerate(view.get("reveal_rules", []))],
            "mid_call_events": view["mid_call_events"],
        },
        "transcript": voice_transcript(events) if is_voice(events) else transcript(events),
    }
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"


def _shape_errors(value, schema, path="응답"):
    """기존 응답 스키마의 자료형과 필수 필드를 확인한다. 판정 내용은 바꾸지 않는다."""
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    expected = types[schema["type"]]
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        return [f"{path}: {schema['type']} 형식 아님"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: 허용한 값 아님"]
    errors = []
    if isinstance(value, dict):
        errors += [f"{path}.{key}: 필수 필드 없음" for key in schema.get("required", []) if key not in value]
        for key, child in schema.get("properties", {}).items():
            if key in value:
                errors += _shape_errors(value[key], child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            errors += _shape_errors(child, schema["items"], f"{path}[{i}]")
        ids = [v["index"] for v in value if isinstance(v, dict) and type(v.get("index")) is int]
        if len(ids) != len(set(ids)):
            errors.append(f"{path}: 중복 항목 번호")
    return errors


def _safe_verdict(data):
    """재시도를 다 써도 올바른 항목만 남겨, 잘못된 형식은 판정 불가로 처리한다."""
    data = data if isinstance(data, dict) else {}
    out = {}
    for key, schema in JUDGE_SCHEMA["properties"].items():
        value = data.get(key)
        if schema["type"] == "array":
            rows = value if isinstance(value, list) else []
            ids = [v.get("index") for v in rows if isinstance(v, dict) and type(v.get("index")) is int]
            out[key] = [v for v in rows if not _shape_errors(v, schema["items"]) and ids.count(v["index"]) == 1]
        else:
            out[key] = value if not _shape_errors(value, schema) else None
    return out


def _validate(scn: dict[str, Any], data: dict[str, Any], n_consent: int = 0) -> list[str]:
    """AI 출력에 빠진 항목 번호."""
    want = {
        "write_consent": set(range(n_consent)),
        "forbidden": set(forbidden_split(scn)[1]),
        "must_say": set(range(len(scn["expected"].get("must_say", [])))),
        "reveal_rules": set(range(len(scn.get("reveal_rules", [])))),
        "mid_call_events": {i for i, _ in customer_events(scn)},
    }
    missing = []
    for key, idx in want.items():
        got = {x.get("index") for x in data.get(key, []) if isinstance(x, dict)}
        missing += [f"{key}#{i}" for i in sorted(idx - got)]
    if not isinstance(data.get("action_content"), dict):
        missing.append("action_content")
    return missing


async def ai_judge(scn: dict[str, Any], events: list[dict[str, Any]], client: GeminiJson) -> dict[str, Any]:
    """누락·형식·호출 실패는 2·4·8초 뒤 최대 세 번 다시 판정한다. 기준과 지시는 그대로다."""
    user = judge_input(scn, events)
    voice = is_voice(events)
    system = JUDGE_VOICE_SYSTEM if voice else JUDGE_SYSTEM
    usages = []
    record: dict[str, Any] = {"judge_model": client.model,
                              "prompt_version": JUDGE_VOICE_PROMPT_VERSION if voice else JUDGE_PROMPT_VERSION,
                              "forbidden_ai": forbidden_split(scn)[1], "verdict": None, "error": None}
    record["attempt_errors"] = []
    for delay in (0, *RETRY_DELAYS_S):
        if delay:
            await asyncio.sleep(delay)
        reply = await client.ask(system, user, JUDGE_SCHEMA)
        usages.append(reply.usage())
        if reply.data is None:
            record["error"] = reply.error or "판정 응답 없음"
            record["attempt_errors"].append(record["error"])
            continue
        shape = _shape_errors(reply.data, JUDGE_SCHEMA)
        safe = _safe_verdict(reply.data)
        missing = _validate(scn, safe, len(consent_writes(LogView(events))))
        error = f"형식 오류: {shape}" if shape else (f"빠진 항목: {missing}" if missing else None)
        record["verdict"], record["error"] = safe, error
        record["attempt_errors"].append(error)
        if not error:
            break
    record["usage"] = usages
    record["cost_usd"] = sum(u["cost_usd"] for u in usages)
    record["model_version"] = usages[-1]["model"] if usages else None
    return record


# --- 종합 ---------------------------------------------------------------------------------------


def _by_index(rows: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    return {x["index"]: x for x in rows or [] if isinstance(x, dict) and isinstance(x.get("index"), int)}


def compliance(scn: dict[str, Any], ai: dict[str, Any] | None, sim_end: dict[str, Any] | None) -> dict[str, Any]:
    """고객 AI 지시 준수. valid가 False면 무효 실행이다(실패로 세지 않는다). AI 판정이 없으면 valid는 None."""
    reasons = []
    if sim_end and sim_end.get("reason") == "customer_error":
        reasons.append(f"고객 AI 오류로 중단: {sim_end.get('error')}")
    for i in (sim_end or {}).get("fast_missing") or []:
        reasons.append(f"사건 #{i}을 빠른 정정(메시지 두 개)이 아니라 메시지 하나로 실행함")
    v = (ai or {}).get("verdict")
    if v is None:
        return {"valid": False if reasons else None, "reasons": reasons or ["AI 판정 없음"], "reveal_rules": {},
                "mid_call_events": {}}
    rules = _by_index(v.get("reveal_rules"))
    events = _by_index(v.get("mid_call_events"))
    unknown = []
    for i, text in enumerate(scn.get("reveal_rules", [])):
        r = rules.get(i)
        if r is None:
            unknown.append(f"reveal_rules#{i} 판정 없음")
        elif r["status"] == "violated":
            reasons.append(f"reveal_rules#{i} 어김: {r['reason']}")
    for i, _ in customer_events(scn):
        r = events.get(i)
        if r is None:
            unknown.append(f"사건 #{i} 판정 없음")
        elif r["status"] == "violated":
            reasons.append(f"사건 #{i} 어김: {r['reason']}")
    valid = False if reasons else (None if unknown else True)
    return {"valid": valid, "reasons": reasons + unknown,
            "reveal_rules": {i: r["status"] for i, r in rules.items()},
            "mid_call_events": {i: r["status"] for i, r in events.items()}}


def evaluate(scn: dict[str, Any], events: list[dict[str, Any]], ai: dict[str, Any] | None,
             sim_end: dict[str, Any] | None = None) -> dict[str, Any]:
    """코드 판정과 저장한 AI 판정을 합쳐 항목 1~10, 통과 여부, 유효 여부를 낸다."""
    code = code_checks(scn, events)
    v = (ai or {}).get("verdict") or {}
    exp = scn["expected"]
    items: dict[int, ItemVerdict] = {}

    # 1: 코드 금지 항목 + AI 금지 항목
    problems = list(code["forbidden_code"])
    forb = _by_index(v.get("forbidden"))
    undetermined = False
    for i in forbidden_split(scn)[1]:
        r = forb.get(i)
        if r is None:
            undetermined = True
        elif r["violated"]:
            problems.append(f"금지 '{exp['forbidden'][i]}': {r['reason']}")
    items[1] = ItemVerdict(False if problems else (None if undetermined else True),
                           problems or (["AI 판정 없음"] if undetermined else []), "code+ai")
    for n in (2, 3, 4, 5, 7, 8, 10):
        d = code[f"item{n}"]
        items[n] = ItemVerdict(d["ok"], d["reasons"], d["source"])
    # 2의 고객 확인 부분: policy.decided가 없는 로그(정책 A)는 AI 판정(write_consent)
    if code["consent_writes"]:
        consent = _by_index(v.get("write_consent"))
        problems = list(items[2].reasons)
        undetermined = False
        for k, call in enumerate(code["consent_writes"]):
            r = consent.get(k)
            if r is None:
                undetermined = True
            elif not r["consented"]:
                problems.append(f"쓰기 #{k} {call}: 고객 확인 전 쓰기({r['reason']})")
        items[2] = ItemVerdict(False if problems else (None if undetermined else True),
                               problems or (["AI 판정 없음(write_consent)"] if undetermined else []), "code+ai")
    # 6: 도구 호출 부분(코드) + 안내 내용(AI)
    problems = list(code["item6_code"])
    ac = v.get("action_content")
    if isinstance(ac, dict) and not ac.get("matches"):
        problems.append(f"안내 내용이 {exp['action']}과 다름: {ac.get('reason')}")
    items[6] = ItemVerdict(False if problems else (True if isinstance(ac, dict) else None),
                           problems or ([] if isinstance(ac, dict) else ["AI 판정 없음"]), "code+ai")
    # 9: AI
    must = _by_index(v.get("must_say"))
    problems, undetermined = [], False
    for i, text in enumerate(exp.get("must_say", [])):
        r = must.get(i)
        if r is None:
            undetermined = True
        elif not r["said"]:
            problems.append(f"'{text}' 안내 없음: {r['reason']}")
    items[9] = ItemVerdict(False if problems else (None if undetermined else True),
                           problems or (["AI 판정 없음"] if undetermined else []), "ai")

    oks = [items[n].ok for n in ITEMS]
    passed = False if False in oks else (None if None in oks else True)
    return {"scenario": scn["id"], "pass": passed, "items": {n: items[n].to_dict() for n in ITEMS},
            "compliance": compliance(scn, ai, sim_end), "aux": code["aux"]}


# --- 실행 폴더 판정 --------------------------------------------------------------------------------


def judge_path(run_dir: Path, session_id: str) -> Path:
    return run_dir / f"{session_id}.judge.json"


def sim_end_of(run_dir: Path, session_id: str) -> dict[str, Any] | None:
    path = run_dir / f"{session_id}.sim.jsonl"
    if not path.exists():
        return None
    return next((e for e in reversed(read_log(path)) if e["type"] == "sim.end"), None)


async def judge_session(scn: dict[str, Any], run_dir: Path, session_id: str, client: GeminiJson) -> dict[str, Any]:
    events = read_log(run_dir / f"{session_id}.jsonl")
    record = await ai_judge(scn, events, client)
    record["session"] = session_id
    record["scenario"] = scn["id"]
    path = judge_path(run_dir, session_id)
    if path.exists():
        previous = json.loads(read_text(path))
        record["cost_usd"] += previous.get("cost_usd", 0)
        record["usage"] = previous.get("usage", []) + record["usage"]
        record["previous_judgments"] = previous.get("previous_judgments", 0) + 1
    store_json(path, record)
    return record


def load_summary(run_dir: Path) -> dict[str, Any]:
    return json.loads(read_text(run_dir / "summary.json"))


async def judge_run(run_dir: Path, model: str = DEFAULT_JUDGE_MODEL, force: bool = False) -> None:
    summary = load_summary(run_dir)
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / summary["scenario_file"])}
    client = GeminiJson.from_config(model, retry_delays_s=())
    try:
        for sess in summary["sessions"]:
            if judge_path(run_dir, sess["session"]).exists() and not force:
                continue
            rec = await judge_session(scenarios[sess["scenario"]], run_dir, sess["session"], client)
            print(f"{sess['session']}: AI 판정 ${rec['cost_usd']:.4f} {rec['error'] or ''}")
    finally:
        await client.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description="실행 폴더의 세션 중 AI 판정 파일이 없는 것을 판정한다")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--force", action="store_true", help="판정 파일이 있어도 다시 판정한다(비용이 든다)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(judge_run(args.run_dir, args.model, args.force))


if __name__ == "__main__":
    main()
