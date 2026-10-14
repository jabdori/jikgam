"""상담 콘솔의 화면 메시지와 화면 상태. 입력은 엔진 이벤트 로그 줄(dict)뿐이다.

- ui_message(event): 이벤트 한 줄을 화면 메시지 하나로 옮긴다(활동 기록용 한 줄 요약과 주요 필드). 모르는 유형도 버리지
  않고 kind "other"로 낸다.
- ConsoleView: 이벤트를 차례로 받아 콘솔 패널 상태를 만든다. 대화, 제안과 무효화, 근거 조항과 applies 확률, 충분성,
  다음 행동, 도구 호출과 결과, 시스템 사실과 고객 진술, 폐기·취소된 작업. snapshot()은 JSON으로 보낼 수 있는 dict다.

시스템 사실은 tool.result에서만, 고객 진술은 claims.extracted에서만 만든다(ADR 0003의 구분을 화면에서도 지킨다).
엔진이 지난 버전이라 버린 조회 결과(tool.result 바로 뒤의 result.discarded stage=tool)는 사실에 넣지 않는다. 엔진 세션
객체를 읽지 않으므로 끝난 세션의 로그 파일에도 같은 함수를 쓸 수 있다. 정책 A(RAG) 로그에는 버전 증가, 무효화, 판별
확률이 없으므로 그 패널은 비어 있다.
"""

from typing import Any

from jikgam.engine import events as ev
from jikgam.engine.bizapi import RETRYABLE, WRITE_TOOLS

from . import labels as lb

RECEIPT_IDS = ("refund_id", "cancellation_id", "registration_id", "handoff_id")


def _won(x: Any) -> str:
    return f"{x:,}원" if isinstance(x, (int, float)) and not isinstance(x, bool) else "-"


def _args_text(args: dict[str, Any]) -> str:
    keep = ("customer_id", "subscription_id", "payment_id", "reason", "phone", "otp", "from", "to", "bank", "holder")
    return ", ".join(f"{k}={args[k]}" for k in keep if k in args)


def tool_summary(tool: str, ok: bool, error_code: str | None, data: dict[str, Any] | None,
                 args: dict[str, Any] | None = None) -> str:
    """도구 결과 한 줄 요약."""
    d = data or {}
    reason = lb.label(lb.QUOTE_REASON, (args or {}).get("reason"))
    if not ok:
        msg = d.get("message")
        head = f"{reason}: " if tool == "quote_refund" and reason else ""
        return f"{head}{error_code}" + (f" — {msg}" if msg else "")
    if tool == "verify_identity":
        if d.get("verified"):
            return f"본인 확인됨 ({d.get('customer_id')})"
        return f"본인 확인 실패 (누적 실패 {d.get('failed_attempts', '?')}회)" + (", 인계 필요" if d.get("handoff_required") else "")
    if tool == "get_customer":
        return f"{d.get('name')} · 상태 {d.get('status')}"
    if tool in ("list_subscriptions", "list_payments", "get_usage", "list_incidents"):
        key = {"list_subscriptions": "subscriptions", "list_payments": "payments", "get_usage": "usage",
               "list_incidents": "incidents"}[tool]
        return f"{len(d.get(key) or [])}건"
    if tool == "quote_refund":  # 차감액 사유(조직 설정 deduction_reasons)도 같은 필드라 "환불"이라고 쓰지 않는다
        s = f"{reason}: 견적 {_won(d.get('refund_amount'))}" + ("" if d.get("eligible") else " (0원)")
        return s + (", 상담사 승인 필요" if d.get("approval_required") else "")
    if tool == "create_refund_request":
        return f"환불 접수 {d.get('refund_id')} {_won(d.get('amount'))}, 상태 {d.get('status')}"
    if tool == "create_cancellation":
        return f"해지 접수 {d.get('cancellation_id')}, 마지막 이용일 {d.get('last_usable_date')}"
    if tool == "register_refund_account":
        if d.get("registered"):
            return f"계좌 등록 {d.get('registration_id')} ({d.get('bank')} {d.get('account_masked')})"
        return f"계좌 등록 거절: {d.get('reject_reason')}"
    if tool == "create_handoff":
        return f"인계 접수 {d.get('handoff_id')}, 예상 대기 {d.get('expected_wait_minutes')}분"
    return "성공"


def ui_message(e: dict[str, Any]) -> dict[str, Any]:
    """이벤트 한 줄을 화면 메시지로. 모든 메시지에 t_ms, type(원래 유형), kind, version, text(한 줄 요약)가 있다."""
    t = e["type"]
    m: dict[str, Any] = {"t_ms": e.get("t_ms"), "type": t, "version": e.get("version")}

    def put(kind: str, text: str, **fields: Any) -> dict[str, Any]:
        m.update(kind=kind, text=text, **fields)
        return m

    if t == ev.SESSION_START:
        policy = e.get("policy") or ("B" if e.get("decision_backend") == "gemini-flash" else "C")
        return put("session", f"세션 시작: 정책 {policy}, 판별 {e.get('decision_backend') or '없음(RAG)'}, "
                              f"생성 {e.get('generator')}", policy=policy, decision_backend=e.get("decision_backend"),
                   generator=e.get("generator"), org=e.get("org"), today=e.get("today"), channel=e.get("channel"))
    if t == ev.WARMUP:
        return put("warmup", f"예열 {e.get('backend')} {e.get('latency_ms')} ms" + (f", 오류 {e['error']}" if e.get("error") else ""),
                   latency_ms=e.get("latency_ms"), error=e.get("error"))
    if t == ev.UTTERANCE_END:
        who = {"customer": "고객", "agent": "상담사"}.get(e.get("speaker"), e.get("speaker"))
        mark = e.get("marker") or {}
        flag = " [조건 변경 표시]" if mark.get("condition_change") else ""
        return put("utterance", f"{who}({e.get('utt_id')}) {e.get('text')}{flag}", speaker=e.get("speaker"),
                   utt_id=e.get("utt_id"), utterance=e.get("text"), marker=e.get("marker"))
    if t in (ev.STT_PARTIAL, ev.STT_FINAL):
        return put("partial" if t == ev.STT_PARTIAL else "stt_final", f"자막({e.get('utt_id')}) {e.get('text')}",
                   utt_id=e.get("utt_id"), utterance=e.get("text"))
    if t == ev.VERSION_BUMPED:
        cause = lb.label(lb.BUMP_CAUSE, e.get("cause"))
        return put("version", f"버전 v{e.get('from')} → v{e.get('to')} ({cause}{', ' + e['utt_id'] if e.get('utt_id') else ''})",
                   cause=e.get("cause"), to=e.get("to"), utt_id=e.get("utt_id"))
    if t == ev.CLAIMS_EXTRACTED:
        keys = e.get("keys") or {}
        parts = [f"{lb.label(lb.CLAIM, k)} {v}" for k, v in keys.items()] + [f"번호 {p}" for p in e.get("phones") or []]
        parts += ["인증번호 " + o for o in e.get("otps") or []]
        return put("claims", f"진술 추출({e.get('utt_id')}): " + (", ".join(parts) if parts else "없음"),
                   utt_id=e.get("utt_id"), keys=keys)
    if t == ev.RETRIEVAL_DONE:
        return put("retrieval", f"검색 {e.get('duration_ms')} ms, 후보 {len(e.get('candidates') or [])}개",
                   candidates=e.get("candidates") or [], duration_ms=e.get("duration_ms"))
    if t == ev.DECISION_REQUEST:
        return put("decision_request", f"판별 요청 {e.get('backend')} 질문 {len(e.get('questions') or [])}개",
                   backend=e.get("backend"), questions=len(e.get("questions") or []))
    if t == ev.DECISION_RESPONSE:
        err = f", 오류 {e['error']}" if e.get("error") else ""
        return put("decision", f"판별 응답 {e.get('backend')} {e.get('latency_ms')} ms{err}", backend=e.get("backend"),
                   latency_ms=e.get("latency_ms"), error=e.get("error"), request_version=e.get("request_version"))
    if t == ev.EVIDENCE_UPDATED:
        suff = {True: "충분", False: "부족", None: "판단 없음"}[e.get("sufficient")]
        return put("evidence", f"근거 갱신 v{e.get('version')}: {', '.join(e.get('articles') or []) or '선택 조항 없음'} · "
                               f"충분성 {suff} · 행동 {lb.label(lb.ACTION, e.get('action')) or '-'}",
                   articles=e.get("articles") or [], sufficient=e.get("sufficient"), action=e.get("action"),
                   intent=e.get("intent"))
    if t == ev.POLICY_DECIDED:
        ask = f" · 물을 것 {lb.label(lb.ASK, e['ask'])}" if e.get("ask") else ""
        return put("policy", f"정책 {lb.label(lb.RULE, e.get('rule'))} ({e.get('rule')}) → {lb.label(lb.ACTION, e.get('action'))}{ask}",
                   rule=e.get("rule"), action=e.get("action"), ask=e.get("ask"))
    if t == ev.TOOL_CALL:
        return put("tool_call", f"도구 호출 {e.get('tool')}({_args_text(e.get('args') or {})})", tool=e.get("tool"),
                   call_id=e.get("call_id"))
    if t == ev.TOOL_RESULT:
        res = "성공" if e.get("ok") else f"오류 {e.get('error_code')}"
        return put("tool_result", f"도구 결과 {e.get('tool')} {res} ({e.get('latency_ms')} ms, 시도 {e.get('attempts')})",
                   tool=e.get("tool"), call_id=e.get("call_id"), ok=e.get("ok"), error_code=e.get("error_code"))
    if t == ev.QUOTE_SELECTED:
        return put("quote", f"견적 선택: {lb.label(lb.QUOTE_REASON, e.get('chosen')) or '없음'}", chosen=e.get("chosen"))
    if t == ev.GENERATION_REQUEST:
        step = f" 단계 {e['step']}" if e.get("step") else ""
        return put("generation_request", f"생성 요청 {e.get('model')}{step}", model=e.get("model"))
    if t == ev.GENERATION_RESPONSE:
        extra = "".join([f", 고정 문구" if e.get("fallback") else "", f", 검사 위반 {e['violations']}" if e.get("violations") else "",
                         f", 오류 {e['error']}" if e.get("error") else ""])
        return put("generation", f"생성 응답 {e.get('latency_ms')} ms{extra}", latency_ms=e.get("latency_ms"),
                   fallback=e.get("fallback"), error=e.get("error"))
    if t == ev.SUGGESTION_CREATED:
        return put("suggestion", f"제안 {_short(e.get('suggestion_id'))} 생성(기준 v{e.get('basis_version')}, "
                                 f"{lb.label(lb.ACTION, e.get('action'))})", suggestion_id=e.get("suggestion_id"),
                   basis_version=e.get("basis_version"))
    if t == ev.AGENT_SAY:
        return put("say", f"상담({_short(e.get('suggestion_id'))}) {e.get('text')}", suggestion_id=e.get("suggestion_id"),
                   utterance=e.get("text"))
    if t == ev.SUGGESTION_EXPOSED:
        return put("exposed", f"제안 {_short(e.get('suggestion_id'))} 노출({e.get('channel')})",
                   suggestion_id=e.get("suggestion_id"), channel=e.get("channel"))
    if t == ev.SUGGESTION_INVALIDATED:
        reasons = [lb.invalidation_reason(r) for r in e.get("reasons") or []]
        return put("invalidated", f"제안 {_short(e.get('suggestion_id'))} 무효화됨: {'; '.join(reasons)}",
                   suggestion_id=e.get("suggestion_id"), reasons=reasons)
    if t == ev.RESULT_DISCARDED:
        return put("discarded", f"{lb.label(lb.STAGE, e.get('stage'))} 결과 폐기(시작 v{e.get('started_version')}, 현재 v{e.get('version')})",
                   stage=e.get("stage"), started_version=e.get("started_version"))
    if t == ev.TASK_CANCELLED:
        return put("cancelled", f"{lb.label(lb.STAGE, e.get('stage'))} 단계에서 작업 취소(시작 v{e.get('started_version')}, 현재 v{e.get('version')})",
                   stage=e.get("stage"), started_version=e.get("started_version"))
    if t == ev.WRITES_WAIT:
        return put("writes_wait", f"앞 턴의 쓰기 {e.get('writes')}건이 끝나기를 기다림")
    if t == ev.ENGINE_ERROR:
        return put("error", f"엔진 오류({lb.label(lb.STAGE, e.get('stage'))}): {e.get('error')}", error=e.get("error"))
    if t == ev.FAULT_INJECTED:
        return put("fault", f"장애 주입({e.get('before_utt')} 전): {e.get('faults')}", faults=e.get("faults"))
    return put("other", t)


def _short(sid: str | None) -> str:
    return (sid or "").rsplit("-", 1)[-1]


class ConsoleView:
    """이벤트를 차례로 받아 콘솔 패널 상태를 만든다."""

    def __init__(self, titles: dict[str, str] | None = None, deduction_reasons: frozenset[str] | tuple = ()):
        """titles: 조항 ID → 제목. deduction_reasons: 견적 금액이 환불이 아니라 차감액인 사유(조직 설정)."""
        self.titles = titles or {}
        self.deduction_reasons = frozenset(deduction_reasons)
        self.meta: dict[str, Any] = {}
        self.version = 0
        self.transcript: list[dict[str, Any]] = []
        self.suggestions: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, Any] | None = None
        self.policy: dict[str, Any] | None = None
        self.sources: dict[str, str] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.claims: list[dict[str, Any]] = []
        self.discards: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.quote_chosen: str | None = None
        self.handoff_requested = False
        self.warmup: dict[str, Any] | None = None
        self.costs = {"decision_usd": 0.0, "generation_usd": 0.0, "warmup_usd": 0.0, "decision_calls": 0,
                      "generation_calls": 0}
        self.n_events = 0
        self._last_result: str | None = None  # 바로 앞 줄이 tool.result였으면 그 call_id

    def apply(self, e: dict[str, Any]) -> None:
        t = e["type"]
        self.n_events += 1
        self.version = max(self.version, int(e.get("version") or 0))
        last_result, self._last_result = self._last_result, None
        if t == ev.SESSION_START:
            msg = ui_message(e)
            self.meta = {k: msg.get(k) for k in ("policy", "decision_backend", "generator", "org", "today", "channel")}
            self.meta["session"] = e.get("session")
        elif t == ev.WARMUP:
            self.warmup = {"backend": e.get("backend"), "latency_ms": e.get("latency_ms"), "error": e.get("error")}
            self.costs["warmup_usd"] += e.get("cost_usd") or 0
        elif t == ev.UTTERANCE_END:
            self.transcript.append({"utt_id": e.get("utt_id"), "speaker": e.get("speaker"), "text": e.get("text"),
                                    "t_ms": e.get("t_ms"), "marker": e.get("marker"), "final": True})
        elif t == ev.STT_PARTIAL:
            cur = next((x for x in self.transcript if x["utt_id"] == e.get("utt_id") and not x["final"]), None)
            if cur is None:
                self.transcript.append({"utt_id": e.get("utt_id"), "speaker": "customer", "text": e.get("text"),
                                        "t_ms": e.get("t_ms"), "marker": None, "final": False})
            else:
                cur["text"] = e.get("text")
        elif t == ev.VERSION_BUMPED:
            if e.get("cause") == "agent_handoff":
                self.handoff_requested = True
        elif t == ev.CLAIMS_EXTRACTED:
            for k, v in (e.get("keys") or {}).items():
                self.claims.append({"key": k, "label": lb.label(lb.CLAIM, k), "value": v, "utt_id": e.get("utt_id")})
            for p in e.get("phones") or []:
                self.claims.append({"key": "phone", "label": "휴대폰 번호", "value": p, "utt_id": e.get("utt_id")})
            for o in e.get("otps") or []:
                self.claims.append({"key": "otp", "label": "인증번호(가림)", "value": o, "utt_id": e.get("utt_id")})
        elif t == ev.RETRIEVAL_DONE:
            self.sources = dict(e.get("sources") or {})
        elif t == ev.DECISION_RESPONSE:
            self.costs["decision_usd"] += e.get("cost_usd") or 0
            self.costs["decision_calls"] += 1
        elif t == ev.EVIDENCE_UPDATED:
            self._evidence(e)
        elif t == ev.POLICY_DECIDED:
            self.policy = {"version": e.get("version"), "rule": e.get("rule"), "rule_label": lb.label(lb.RULE, e.get("rule")),
                           "action": e.get("action"), "action_label": lb.label(lb.ACTION, e.get("action")),
                           "ask": e.get("ask"), "ask_label": lb.label(lb.ASK, e.get("ask")),
                           "next_info": e.get("next_info"), "next_info_label": lb.label(lb.NEXT_INFO, e.get("next_info")),
                           "calls": e.get("calls") or [], "lookups": bool(e.get("lookups")),
                           "quote_reasons": e.get("quote_reasons") or [], "writes": e.get("writes") or [],
                           "agreed": e.get("agreed"), "declined": e.get("declined"), "detail": e.get("detail") or {}}
        elif t == ev.TOOL_CALL:
            self.tools[e["call_id"]] = {"call_id": e["call_id"], "tool": e.get("tool"),
                                        "label": lb.label(lb.TOOL, e.get("tool")), "args": e.get("args") or {},
                                        "request_version": e.get("request_version"), "t_call": e.get("t_ms"),
                                        "result": None, "discarded": False, "cancelled": False}
        elif t == ev.TOOL_RESULT:
            call = self.tools.setdefault(e["call_id"], {"call_id": e["call_id"], "tool": e.get("tool"),
                                                        "label": lb.label(lb.TOOL, e.get("tool")), "args": {},
                                                        "request_version": e.get("request_version"), "t_call": None,
                                                        "result": None, "discarded": False, "cancelled": False})
            call["result"] = {"ok": e.get("ok"), "status": e.get("status"), "error_code": e.get("error_code"),
                              "attempts": e.get("attempts"), "latency_ms": e.get("latency_ms"), "data": e.get("data"),
                              "t_ms": e.get("t_ms")}
            self._last_result = e["call_id"]
        elif t == ev.QUOTE_SELECTED:
            self.quote_chosen = e.get("chosen")
        elif t == ev.GENERATION_RESPONSE:
            self.costs["generation_usd"] += e.get("cost_usd") or 0
            self.costs["generation_calls"] += 1
        elif t == ev.SUGGESTION_CREATED:
            for s in self.suggestions.values():  # 엔진 _expose와 같이 앞 제안은 새 제안으로 바뀐다
                if s["status"] == "active":
                    s["status"] = "superseded"
            self.suggestions[e["suggestion_id"]] = {
                "id": e["suggestion_id"], "short_id": _short(e["suggestion_id"]), "basis_version": e.get("basis_version"),
                "text": e.get("text"), "action": e.get("action"), "action_label": lb.label(lb.ACTION, e.get("action")),
                "rule": e.get("rule"), "rule_label": lb.label(lb.RULE, e.get("rule")), "intent": e.get("intent"),
                "intent_label": lb.label(lb.INTENT, e.get("intent")), "cited_articles": e.get("cited_articles") or [],
                "amount": e.get("amount"), "amount_final": e.get("amount_final"),
                "proposal": (e.get("proposal") or {}).get("writes"), "fallback": e.get("fallback"),
                "status": "active", "reasons": [], "invalidated_version": None, "exposed": False,
                "t_created": e.get("t_ms"), "t_exposed": None}
        elif t == ev.AGENT_SAY:
            self.transcript.append({"utt_id": _short(e.get("suggestion_id")), "speaker": "engine", "text": e.get("text"),
                                    "t_ms": e.get("t_ms"), "marker": None, "final": True,
                                    "suggestion_id": e.get("suggestion_id")})
        elif t == ev.SUGGESTION_EXPOSED:
            s = self.suggestions.get(e.get("suggestion_id"))
            if s is not None:
                s["exposed"], s["t_exposed"], s["channel"] = True, e.get("start_ms", e.get("t_ms")), e.get("channel")
        elif t == ev.SUGGESTION_INVALIDATED:
            s = self.suggestions.get(e.get("suggestion_id"))
            if s is not None:
                s["status"] = "invalidated"
                s["invalidated_version"] = e.get("invalidated_version")
                s["reasons"] = [{"code": r, "label": lb.invalidation_reason(r)} for r in e.get("reasons") or []]
        elif t in (ev.RESULT_DISCARDED, ev.TASK_CANCELLED):
            kind = "discarded" if t == ev.RESULT_DISCARDED else "cancelled"
            if kind == "cancelled":  # 취소된 파이프라인의 조회는 결과 없이 끝난다. 쓰기는 shield라 나중에 결과가 온다
                for call in self.tools.values():
                    if (call["result"] is None and call["tool"] not in WRITE_TOOLS
                            and call["request_version"] == e.get("started_version")):
                        call["cancelled"] = True
            self.discards.append({"kind": kind, "stage": e.get("stage"), "stage_label": lb.label(lb.STAGE, e.get("stage")),
                                  "started_version": e.get("started_version"), "version": e.get("version"),
                                  "t_ms": e.get("t_ms"), "detail": e.get("tool") or e.get("backend")})
            if kind == "discarded" and e.get("stage") == "tool" and last_result is not None:
                self.tools[last_result]["discarded"] = True  # 엔진이 시스템 사실에 반영하지 않은 조회 결과
        elif t == ev.ENGINE_ERROR:
            self.errors.append({"stage": e.get("stage"), "error": e.get("error"), "t_ms": e.get("t_ms")})
        elif t == ev.FAULT_INJECTED:
            self.faults.append({"before_utt": e.get("before_utt"), "faults": e.get("faults")})

    def _evidence(self, e: dict[str, Any]) -> None:
        applies = e.get("applies")  # 정책 A(RAG)에는 없다
        selected = set(e.get("articles") or [])
        cands = []
        for a in e.get("candidates") or []:
            cands.append({"id": a, "title": self.titles.get(a), "p": None if applies is None else applies.get(a),
                          "selected": a in selected, "source": self.sources.get(a)})
        for a in e.get("articles") or []:  # RAG가 후보 밖 조항을 인용한 경우
            if a not in {c["id"] for c in cands}:
                cands.append({"id": a, "title": self.titles.get(a), "p": None, "selected": True, "source": None})
        self.evidence = {"version": e.get("version"), "t_ms": e.get("t_ms"), "candidates": cands,
                         "has_probabilities": applies is not None, "sufficient": e.get("sufficient"),
                         "next_info": e.get("next_info"), "next_info_label": lb.label(lb.NEXT_INFO, e.get("next_info")),
                         "action": e.get("action"), "action_label": lb.label(lb.ACTION, e.get("action")),
                         "intent": e.get("intent"), "intent_label": lb.label(lb.INTENT, e.get("intent")),
                         "caller": e.get("caller"), "caller_label": lb.label(lb.CALLER, e.get("caller")),
                         "pass_no": e.get("pass_no"), "decision_error": e.get("decision_error") or e.get("error")}

    # --- 시스템 사실 ------------------------------------------------------------------------------

    def facts(self) -> dict[str, Any]:
        """반영된 tool.result로 만든 시스템 사실. 진행 중인 호출과 폐기된 조회 결과는 넣지 않는다."""
        out: dict[str, Any] = {"identity": {"verified": False, "customer_id": None, "attempts": 0, "failed_attempts": 0,
                                            "handoff_required": False},
                               "customer": None, "subscriptions": None, "payments": None, "usage": None,
                               "incidents": None, "quotes": {}, "receipts": [], "account_rejections": [],
                               "api_errors": []}
        lists = {"list_subscriptions": "subscriptions", "list_payments": "payments", "get_usage": "usage",
                 "list_incidents": "incidents"}
        for call in self.tools.values():
            r = call["result"]
            if r is None or call["discarded"]:
                continue
            tool, d, args = call["tool"], r["data"] or {}, call["args"]
            if r["ok"]:
                if tool == "verify_identity":
                    ident = out["identity"]
                    ident["attempts"] = d.get("attempts", ident["attempts"])
                    ident["failed_attempts"] = d.get("failed_attempts", ident["failed_attempts"])
                    ident["handoff_required"] = ident["handoff_required"] or bool(d.get("handoff_required"))
                    if d.get("verified"):
                        ident["verified"], ident["customer_id"] = True, d.get("customer_id")
                elif tool == "get_customer":
                    out["customer"] = {k: d.get(k) for k in ("customer_id", "name", "birth", "status")}
                elif tool in lists:
                    out[lists[tool]] = d.get(lists[tool]) or []
                elif tool == "quote_refund":
                    self._quote(out, args.get("reason"), "eligible" if d.get("eligible") else "zero", d)
                elif tool == "register_refund_account" and not d.get("registered"):  # 엔진도 거절로 본다(state.py)
                    out["account_rejections"].append({"holder": args.get("holder"), "reason": d.get("reject_reason"),
                                                      "message": d.get("message")})
                elif tool in WRITE_TOOLS:
                    out["receipts"].append(self._receipt(tool, d, existing=False))
            else:
                code = r["error_code"]
                if tool == "quote_refund" and code in ("NOT_ELIGIBLE", "HANDOFF_REQUIRED"):
                    self._quote(out, args.get("reason"), "not_eligible" if code == "NOT_ELIGIBLE" else "handoff",
                                d.get("quote") or {}, d.get("message"), d.get("article_ids"))
                elif tool == "quote_refund" and code in RETRYABLE:
                    self._quote(out, args.get("reason"), "failed", {}, code)
                    out["api_errors"].append({"tool": tool, "code": code, "message": d.get("message")})
                elif code == "DUPLICATE_REQUEST" and d.get("receipt"):
                    out["receipts"].append(self._receipt(tool, d["receipt"], existing=True))
                else:
                    out["api_errors"].append({"tool": tool, "code": code, "message": d.get("message")})
        out["quotes"] = list(out["quotes"].values())
        return out

    def _quote(self, out: dict[str, Any], reason: str | None, status: str, q: dict[str, Any], message: str | None = None,
               article_ids: list[str] | None = None) -> None:
        deduction = reason in self.deduction_reasons
        status_label = "차감액(환불 아님)" if deduction and status == "eligible" else lb.label(lb.QUOTE_STATUS, status)
        out["quotes"][reason] = {"reason": reason, "reason_label": lb.label(lb.QUOTE_REASON, reason), "status": status,
                                 "kind": "deduction" if deduction else "refund",
                                 "status_label": status_label, "refund_amount": q.get("refund_amount"),
                                 "approval_required": bool(q.get("approval_required")),
                                 "last_usable_date": q.get("last_usable_date"),
                                 "article_ids": article_ids or q.get("article_ids") or [], "message": message,
                                 "chosen": reason == self.quote_chosen}

    @staticmethod
    def _receipt(tool: str, d: dict[str, Any], existing: bool) -> dict[str, Any]:
        rid = next((d[k] for k in RECEIPT_IDS if d.get(k)), None)
        return {"tool": tool, "label": lb.label(lb.TOOL, tool), "id": rid, "existing": existing,
                "summary": tool_summary(tool, True, None, d)}

    # --- 내보내기 ---------------------------------------------------------------------------------

    def pending(self) -> bool:
        """처리 중인가: 고객 발화가 있는데 아직 제안이 없거나, 마지막 제안의 기준 버전보다 버전이 앞섰다."""
        has_customer = any(x["speaker"] == "customer" for x in self.transcript)
        last = next(reversed(self.suggestions.values()), None)
        if last is None:
            return has_customer
        return self.version > (last["basis_version"] or 0)

    def snapshot(self) -> dict[str, Any]:
        claims = []
        for i, c in enumerate(self.claims):
            later = any(x["key"] == c["key"] for x in self.claims[i + 1:])
            claims.append({**c, "superseded": later})
        tools = []
        for call in self.tools.values():
            r = call["result"]
            if r is None:
                status = "취소됨(결과 없음)" if call["cancelled"] else "진행 중"
            else:
                status = "성공" if r["ok"] else f"오류 {r['error_code']}"
            tools.append({**{k: call[k] for k in ("call_id", "tool", "label", "args", "request_version", "discarded",
                                                  "cancelled")},
                          "status": status, "ok": None if r is None else r["ok"],
                          "attempts": None if r is None else r["attempts"],
                          "latency_ms": None if r is None else r["latency_ms"],
                          "summary": None if r is None else tool_summary(call["tool"], r["ok"], r["error_code"], r["data"],
                                                                         call["args"]),
                          "data": None if r is None else r["data"]})
        facts = self.facts()
        suggestions = list(self.suggestions.values())
        receipt = next((r for r in facts["receipts"] if r["tool"] == "create_handoff"), None)
        return {
            "meta": self.meta, "version": self.version, "pending": self.pending(), "n_events": self.n_events,
            "transcript": self.transcript, "suggestions": suggestions,
            "current": suggestions[-1] if suggestions else None, "evidence": self.evidence, "policy": self.policy,
            "tools": tools, "facts": facts, "claims": claims, "discards": self.discards, "errors": self.errors,
            "faults": self.faults, "warmup": self.warmup,
            "handoff": {"requested": self.handoff_requested, "receipt": receipt},
            "costs": {**self.costs, "total_usd": self.costs["decision_usd"] + self.costs["generation_usd"]
                      + self.costs["warmup_usd"]},
        }
