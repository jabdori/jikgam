"""세션 진행: 한 턴의 처리 순서, 대화 버전, 작업 취소와 결과 폐기. docs/engine.md §3·§4.

고객 확정 발화가 들어오면:
 1. 버전을 올리고(version.bumped) 이전 버전의 진행 작업을 취소한다.
 2. 발화에서 진술을 뽑는다(코드). 판별 상태의 known_facts에는 넣지 않는다.
 3. 검색과 구조 확장.
 4. 판별 1회(intent, condition_changed, applies:*, sufficient, next_info, action, suggestion_valid, customer_agreed).
 5. 근거 선택(applies 확률 ≥ 임계값). 이전 제안 무효화.
 6. evidence.updated.
 7. 정책. 도구 결과가 시스템 사실을 바꾸면 버전을 올리고 3번부터 다시 한다.
 8. 생성. 끝났을 때 버전이 바뀌었으면 버린다.
 9. 제안 생성(suggestion.created)과 노출(agent.say, suggestion.exposed).

한 버전의 3~9단계는 asyncio 작업 하나(파이프라인)로 돈다. 새 고객 발화가 오면 그 작업을 취소하고(task.cancelled),
취소되지 않고 끝난 단계의 결과가 지난 버전이면 쓰지 않는다(result.discarded). 쓰기 API 묶음은 shield로 감싸 취소하지
않는다. 쓰기가 끝났을 때 그것을 시작한 파이프라인이 이미 없으면, 쓰기 결과로 버전을 올리고 최신 발화로 다시 진행한다.

이전 제안 무효화 규칙(5단계): suggestion_valid가 거짓이거나 condition_changed가 참이면 무효화한다(docs/engine.md §4).
여기에 코드 규칙 하나를 더한다: 이번 판단의 intent가 제안을 만들 때의 intent와 다르면 무효화한다. 요청이 바뀌면 정책
판단이 바뀌므로 그 제안은 맞지 않는다(docs/data-format.md suggestion_valid 기준).

고객 동의(8단계의 쓰기 조건)는 무효화와 따로 본다. 마지막으로 노출한 제안에 접수 내용이 있고, condition_changed가
참이 아니고, customer_agreed가 임계값 이상이면 동의 후보다. 정책은 지금 만든 접수 내용의 서명(쓰기 인자와 금액)이
노출한 것과 같을 때만 쓴다. "네, 그래도 해지해 주세요"처럼 동의하면서 요청 라벨이 cancel_refund에서 cancel로 바뀌어
제안이 무효화되는 경우가 있어서다(개발용 재생 s20에서 확인).
"""

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

from jikgam.decision import llm
from jikgam.decision.registry import make_client
from jikgam.decision.types import DecisionClient, DecisionResult, label_and_prob
from jikgam.eval.questions import load_bank, render

from . import events as ev
from . import generate as gen
from . import policy
from .bizapi import WRITE_TOOLS, BizApiClient, ToolResult
from .claims import extract
from .events import EventLog, now_ms, parse_utterance
from .org import OrgConfig
from .retrieval import ArticleIndex, RetrievalContext, parse_terms
from .state import Evidence, SessionState, Suggestion, quote_summary

MAX_PASSES = 6  # 한 턴에서 도구 결과로 3단계부터 다시 하는 최대 횟수(무한 반복 방지)
DECISION_RETRYABLE = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "ConnectError", "ReadTimeout",
                      "ConnectTimeout", "RemoteProtocolError", "ReadError")
CHANNEL = "chat"
CONVERSATION_FOR_GENERATION = 8  # 생성 입력에 넣는 최근 대화 수


def _mask_args(args: dict[str, Any]) -> dict[str, Any]:
    """로그용. 인증번호와 계좌번호를 가린다(가상 데이터라도 실제 운영과 같은 습관을 둔다)."""
    out = dict(args)
    if "otp" in out:
        out["otp"] = "*" * len(str(out["otp"]))
    if "account" in out:
        digits = str(out["account"]).replace("-", "")
        out["account"] = "*" * (len(digits) - 3) + digits[-3:]
    return out


class Session:
    def __init__(self, org: OrgConfig, session_id: str, log_dir: Path, decision_backend: str | None = None, *,
                 decision_client: DecisionClient | None = None, generator: gen.Generator | None = None,
                 bizapi: BizApiClient | None = None):
        """decision_client, generator, bizapi는 테스트에서 가짜를 넣을 때만 쓴다. 기본은 조직 설정으로 만든다."""
        self.org = org
        self.session_id = session_id
        self.backend = decision_backend or org.decision_backend
        self.log = EventLog(Path(log_dir) / f"{session_id}.jsonl", session_id)
        self.state = SessionState(org.org, org.today)
        self.articles = parse_terms(org.terms)
        self.index = ArticleIndex(self.articles, org.retrieval)
        base_version, base = load_bank(org.question_set)
        extra_version, extra = load_bank(org.extra_question_set)
        if set(base) & set(extra):
            raise ValueError(f"추가 질문 세트의 ID가 기본 질문 세트와 겹친다: {sorted(set(base) & set(extra))}")
        self.bank = {**base, **extra}
        self.decider = decision_client or make_client(self.backend)
        self.generator = generator or gen.make_generator(org.generator)
        self.biz = bizapi or BizApiClient(org.bizapi_url, session_id)
        self._task: asyncio.Task | None = None  # 현재 버전의 파이프라인
        self._latest_utt: str | None = None
        self._writes: set[asyncio.Task] = set()
        self._n_suggestions = 0
        self._n_agent = 0
        self._n_calls = 0
        self.log.emit(ev.SESSION_START, 0, org=org.org, today=org.today.isoformat(), decision_backend=self.backend,
                      generator=getattr(self.generator, "model", None), question_sets=[base_version, extra_version],
                      generation_prompt=gen.PROMPT_VERSION, llm_judge_prompt=llm.PROMPT_VERSION,
                      applies_threshold=org.applies_threshold, agreement_threshold=org.agreement_threshold,
                      max_candidates=org.retrieval.max_candidates, bizapi_url=org.bizapi_url, channel=CHANNEL)

    # --- 입력 --------------------------------------------------------------------------------

    async def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """입력 이벤트 하나를 처리하고 이 턴의 출력 이벤트(로그에 남긴 것과 같은 dict)를 돌려준다.

        고객 확정 발화면 처리가 끝날 때까지 기다린다. 처리 중에 더 새로운 발화가 들어오면 그때까지의 이벤트만 돌려준다.
        """
        received = now_ms()
        utt = parse_utterance(event, received)
        out: list[dict[str, Any]] = []
        if not utt.final:  # 미확정 자막은 버전을 올리지 않는다(선판단은 이 엔진 범위 밖)
            out.append(self.log.emit(ev.STT_PARTIAL, self.state.version, utt_id=utt.utt_id, text=utt.text))
            return out
        out.append(self.log.emit(ev.UTTERANCE_END, self.state.version, t_ms=utt.t_end_ms, utt_id=utt.utt_id,
                                 speaker=utt.speaker, text=utt.text, final=True, marker=utt.marker, received_ms=received))
        if utt.speaker != "customer":  # 사람 상담사 발화는 대화에만 남긴다
            self.state.add_turn(utt.utt_id, utt.speaker, utt.text)
            return out
        out.append(self._bump("utterance", utt_id=utt.utt_id))
        self.state.add_turn(utt.utt_id, "customer", utt.text)
        claims = extract(utt.text, self.org.today)
        self.state.add_claims(utt.utt_id, claims)
        self.state.facts.api_failures.clear()  # 새 발화마다 장애 난 도구를 다시 시도할 기회를 준다
        out.append(self.log.emit(ev.CLAIMS_EXTRACTED, self.state.version, utt_id=utt.utt_id, keys=claims.keys,
                                 phones=list(claims.phones), otps=["*" * len(o) for o in claims.otps],
                                 accounts=len(claims.accounts)))
        self._latest_utt = utt.utt_id
        await self._drive(utt.utt_id, out)
        return out

    async def aclose(self) -> None:
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task})
        await self.decider.aclose()
        await self.generator.aclose()
        await self.biz.aclose()
        self.log.close()

    # --- 버전과 작업 --------------------------------------------------------------------------

    def _bump(self, cause: str, cancel: bool = True, **info: Any) -> dict[str, Any]:
        old = self.state.version
        self.state.version += 1
        event = self.log.emit(ev.VERSION_BUMPED, self.state.version, cause=cause, **{"from": old, "to": self.state.version},
                              **info)
        if cancel and self._task is not None and not self._task.done():
            self._task.cancel()
        return event

    def _stale(self, version: int) -> bool:
        return self.state.version != version

    def _discard(self, stage: str, version: int, **info: Any) -> dict[str, Any]:
        return self.log.emit(ev.RESULT_DISCARDED, self.state.version, stage=stage, started_version=version, **info)

    async def _drive(self, utt_id: str, out: list[dict[str, Any]]) -> None:
        while True:
            task = asyncio.create_task(self._pipeline(self.state.version, out))
            self._task = task
            await asyncio.wait({task})
            if not task.cancelled():
                return
            if self._latest_utt != utt_id:  # 더 새로운 고객 발화가 이어받았다
                return
            # 시작한 파이프라인이 끝난 뒤 완료된 쓰기 결과로 버전이 올랐다. 같은 발화로 다시 진행한다.

    # --- 파이프라인 ----------------------------------------------------------------------------

    async def _pipeline(self, version: int, out: list[dict[str, Any]]) -> None:
        my_v = version
        stage = "retrieval"
        try:
            latest = self.state.latest_suggestion()
            pending = latest.proposal if latest else None
            agreed = False
            plan: policy.Plan | None = None
            for n_pass in range(MAX_PASSES):
                first = n_pass == 0
                # 3. 검색
                stage = "retrieval"
                t0 = now_ms()
                result = self.index.search(self._retrieval_context())
                out.append(self.log.emit(ev.RETRIEVAL_DONE, self.state.version, started_version=my_v,
                                         duration_ms=round(now_ms() - t0, 3), candidates=result.candidates,
                                         sources=result.sources, query=result.query))
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v))
                    return
                # 4. 판별
                stage = "decision"
                res, qids = await self._decide(result.candidates, first, latest, my_v, out)
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v, backend=res.backend))
                    return
                labels, probs = self._labels(res, qids)
                # 5. 근거 선택과 이전 제안 무효화
                selected = [a for a in result.candidates if probs.get(f"applies:{a}", 0.0) >= self.org.applies_threshold]
                if first:
                    intent = labels.get("intent") or self.state.request
                    self._invalidate(latest, labels, intent, my_v, out)
                    # 동의는 노출한 접수 내용에 대한 것이다. 제안이 suggestion_valid나 요청 라벨 변화로 무효가 되었어도
                    # 조건 변경이 없고 정책이 지금 만드는 접수 내용(쓰기와 금액)이 같으면 동의로 본다(같은지는 정책이 본다).
                    if pending is not None and labels.get("condition_changed") != "true":
                        agreed = probs.get("customer_agreed", 0.0) >= self.org.agreement_threshold
                    else:
                        pending, agreed = None, False
                    self.state.request = intent
                sufficient = labels.get("sufficient")
                self.state.evidence = Evidence(my_v, result.candidates, selected,
                                               {a: probs.get(f"applies:{a}", 0.0) for a in result.candidates},
                                               None if sufficient is None else sufficient == "true",
                                               labels.get("next_info"), labels.get("action"), self.state.request)
                # 6. 근거 갱신
                out.append(self.log.emit(ev.EVIDENCE_UPDATED, my_v, articles=selected, candidates=result.candidates,
                                         applies={a: round(p, 3) for a, p in self.state.evidence.applies.items()},
                                         sufficient=self.state.evidence.sufficient, next_info=labels.get("next_info"),
                                         action=labels.get("action"), intent=self.state.request, pass_no=n_pass,
                                         decision_error=res.error))
                # 7. 정책
                stage = "policy"
                plan = policy.decide(self.state, policy.PolicyInput(self.state.request, labels.get("action"),
                                                                    labels.get("next_info"), agreed, pending),
                                     self.org.policy, self.org.required_notices)
                out.append(self.log.emit(ev.POLICY_DECIDED, my_v, agreed=agreed, **plan.log()))
                if not plan.has_tool_work:
                    break
                stage = "tools"
                tools = await self._run_tools(plan, my_v, out)
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v, tools=tools))
                    return
                out.append(self._bump("tool", cancel=False, tools=tools))
                my_v = self.state.version
                agreed = False  # 동의는 쓰기 한 번에만 쓴다
            else:
                plan = policy.Plan("pass_limit", "hold")
                out.append(self.log.emit(ev.POLICY_DECIDED, my_v, agreed=False, **plan.log()))
            # 8. 생성
            stage = "generation"
            payload = self._generation_payload(plan)
            out.append(self.log.emit(ev.GENERATION_REQUEST, my_v, model=self.generator.model, action=plan.action,
                                     rule=plan.rule, articles=[a["id"] for a in payload["articles"]]))
            g = await gen.generate_checked(self.generator, payload, self.org.today)
            out.append(self.log.emit(ev.GENERATION_RESPONSE, self.state.version, request_version=my_v, model=g.model,
                                     latency_ms=round(g.latency_ms, 1), input_tokens=g.input_tokens,
                                     output_tokens=g.output_tokens, cost_usd=g.cost_usd, attempts=g.attempts,
                                     fallback=g.fallback, violations=g.violations, error=g.error))
            if self._stale(my_v):
                out.append(self._discard(stage, my_v))
                return
            # 9. 제안과 노출
            self._expose(plan, g, my_v, out)
        except asyncio.CancelledError:
            out.append(self.log.emit(ev.TASK_CANCELLED, self.state.version, stage=stage, started_version=my_v))
            raise
        except Exception as e:  # 엔진 오류로 세션이 멈추지 않게 보류로 답하고 기록한다
            out.append(self.log.emit(ev.ENGINE_ERROR, self.state.version, stage=stage, started_version=my_v,
                                     error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc(limit=5)))
            if not self._stale(my_v):
                self._expose(policy.Plan("engine_error", "hold"), gen.fallback("hold", None, "fallback"), my_v, out)

    def _retrieval_context(self) -> RetrievalContext:
        f = self.state.facts
        system_ids: list[str] = []
        ordered = sorted(f.quotes.values(), key=lambda q: q.status != "eligible")
        for q in ordered:
            system_ids += [a for a in q.article_ids if a not in system_ids]
        for rec in f.receipts.values():
            system_ids += [a for a in rec.get("article_ids", []) if a not in system_ids]
        return RetrievalContext(
            customer_texts=tuple(self.state.customer_texts()), intent=self.state.request,
            identity_verified=self.state.identity.verified, identity_failed=self.state.identity.failed > 0,
            approval_required=any((q.quote or {}).get("approval_required") for q in f.quotes.values()
                                  if q.status == "eligible"),
            handoff_reason=f.handoff_reason is not None, system_article_ids=tuple(system_ids))

    # --- 판별 --------------------------------------------------------------------------------

    def _question_ids(self, candidates: list[str], first: bool, latest: Suggestion | None) -> list[str]:
        qids: list[str] = []
        if first:
            qids.append("intent")
            if self.state.known_facts():
                qids.append("condition_changed")
        qids += [f"applies:{a}" for a in candidates]
        if candidates:
            qids.append("sufficient")
        qids += ["next_info", "action"]
        if first and latest is not None:
            qids.append("suggestion_valid")
            if latest.proposal is not None:
                qids.append("customer_agreed")
        return qids

    async def _decide(self, candidates: list[str], first: bool, latest: Suggestion | None, my_v: int,
                      out: list[dict[str, Any]]) -> tuple[DecisionResult, list[str]]:
        qids = self._question_ids(candidates, first, latest)
        prev = latest.text if (first and latest is not None) else None
        state = self.state.decision_state([self.articles[a] for a in candidates], prev)
        questions = render(qids, self.bank)
        out.append(self.log.emit(ev.DECISION_REQUEST, my_v, backend=self.backend, questions=qids,
                                 state_chars=len(json.dumps(state, ensure_ascii=False))))
        attempts = 0
        while True:
            attempts += 1
            try:
                res = await self.decider.decide(state, questions)
            except Exception as e:  # 백엔드가 예외를 던져도 판별 실패로 다루고 코드 규칙은 계속 쓴다
                res = DecisionResult(self.backend, "응답 없음", {}, error=f"{type(e).__name__}: {e}")
            if not (res.error and not res.answers and res.error.startswith(DECISION_RETRYABLE)) or attempts == 2:
                break
        answers = {}
        for q, a in res.answers.items():
            label, p, _ = label_and_prob(a)
            answers[q] = {"label": label, "p": round(p, 3)} if a["type"] == "choice" else {"p_true": round(a["noul"], 3)}
        out.append(self.log.emit(ev.DECISION_RESPONSE, self.state.version, request_version=my_v, backend=res.backend,
                                 model=res.model, latency_ms=round(res.latency_ms, 1), attempts=attempts,
                                 input_tokens=res.input_tokens, cost_usd=res.cost_usd, error=res.error, answers=answers,
                                 meta=res.meta))
        return res, qids

    def _labels(self, res: DecisionResult, qids: list[str]) -> tuple[dict[str, str], dict[str, float]]:
        """choice는 가장 높은 선택지, noul은 참일 확률. 라벨은 임계값 0.5(applies·동의는 조직 설정 임계값을 따로 쓴다)."""
        labels: dict[str, str] = {}
        probs: dict[str, float] = {}
        for q in qids:
            a = res.answers.get(q)
            if a is None:
                continue
            if a["type"] == "noul":
                probs[q] = float(a["noul"])
                labels[q] = "true" if probs[q] >= 0.5 else "false"
            else:
                labels[q], probs[q], _ = label_and_prob(a)
        return labels, probs

    def _invalidate(self, latest: Suggestion | None, labels: dict[str, str], intent: str | None, my_v: int,
                    out: list[dict[str, Any]]) -> None:
        if latest is None or latest.status != "active":
            return
        reasons = []
        if labels.get("suggestion_valid") == "false":
            reasons.append("suggestion_valid=false")
        if labels.get("condition_changed") == "true":
            reasons.append("condition_changed=true")
        if intent is not None and latest.intent is not None and intent != latest.intent:
            reasons.append(f"intent_changed:{latest.intent}->{intent}")
        if reasons:
            latest.status = "invalidated"
            out.append(self.log.emit(ev.SUGGESTION_INVALIDATED, my_v, suggestion_id=latest.id,
                                     basis_version=latest.basis_version, invalidated_version=my_v, reasons=reasons))

    # --- 도구 --------------------------------------------------------------------------------

    async def _call(self, call: policy.ToolCall, my_v: int, out: list[dict[str, Any]]) -> ToolResult:
        self._n_calls += 1
        call_id = f"c{self._n_calls}"  # 병렬 호출의 결과를 호출과 짝짓는다
        out.append(self.log.emit(ev.TOOL_CALL, self.state.version, request_version=my_v, call_id=call_id,
                                 tool=call.tool, args=_mask_args(call.args), target=call.target))
        r = await self.biz.call(call.tool, call.args, target=call.target)
        # 서버 상태를 바꾸는 호출(쓰기, 본인 확인)은 버전이 지나도 결과를 반영한다. 조회는 지난 버전이면 버린다.
        stale = self._stale(my_v) and call.tool not in WRITE_TOOLS and call.tool != "verify_identity"
        out.append(self.log.emit(ev.TOOL_RESULT, self.state.version, request_version=my_v, call_id=call_id,
                                 tool=call.tool, ok=r.ok,
                                 status=r.status, error_code=r.error_code, attempts=r.attempts, latency_ms=r.latency_ms,
                                 idempotency_key=r.idempotency_key, data=r.data if r.ok else r.error))
        if stale:
            out.append(self._discard("tool", my_v, tool=call.tool))
        else:
            self.state.apply_tool_result(r)
        return r

    async def _run_tools(self, plan: policy.Plan, my_v: int, out: list[dict[str, Any]]) -> list[str]:
        """정책이 정한 도구를 실행한다. 조회는 병렬, 쓰기는 순서대로 shield 안에서."""
        names: list[str] = []
        if plan.calls:
            for c in plan.calls:
                if c.tool == "verify_identity":
                    self.state.identity.tried.add((c.args["phone"], c.args["otp"]))
            await asyncio.gather(*(self._call(c, my_v, out) for c in plan.calls))
            names += [c.tool for c in plan.calls]
        if plan.lookups:
            cid = self.state.identity.customer_id
            wave1 = [policy.ToolCall(t, {"customer_id": cid})
                     for t in ("get_customer", "list_subscriptions", "list_payments", "get_usage")]
            await asyncio.gather(*(self._call(c, my_v, out) for c in wave1))
            start, end = policy.incident_window(self.state)
            await self._call(policy.ToolCall("list_incidents", {"from": start, "to": end}), my_v, out)
            names += [c.tool for c in wave1] + ["list_incidents"]
        if plan.quote_reasons:
            sub, _ = policy.target_subscription(self.state, self.org.policy)
            calls = [policy.ToolCall("quote_refund", {"customer_id": self.state.identity.customer_id,
                                                      "subscription_id": sub["subscription_id"], "reason": r,
                                                      "request_date": self.org.today.isoformat()})
                     for r in plan.quote_reasons]
            await asyncio.gather(*(self._call(c, my_v, out) for c in calls))
            reasons = plan.detail.get("reasons", list(plan.quote_reasons))
            chosen = policy.choose_quote(reasons, self.state.facts.quotes, self.org.policy)
            out.append(self.log.emit(ev.QUOTE_SELECTED, self.state.version, request_version=my_v,
                                     tried={r: {"status": q.status, "refund_amount": (q.quote or {}).get("refund_amount")}
                                            for r, q in self.state.facts.quotes.items() if r in reasons},
                                     chosen=chosen.reason if chosen else None,
                                     priority=list(self.org.policy.quote_priority)))
            names += ["quote_refund"] * len(calls)
        if plan.writes:
            origin = asyncio.current_task()
            bundle = asyncio.ensure_future(self._run_writes(plan.writes, my_v, out, origin))
            self._writes.add(bundle)
            bundle.add_done_callback(self._writes.discard)
            await asyncio.shield(bundle)
            names += [c.tool for c in plan.writes]
        return names

    async def _run_writes(self, writes: tuple[policy.ToolCall, ...], my_v: int, out: list[dict[str, Any]],
                          origin: asyncio.Task | None) -> list[ToolResult]:
        """쓰기 묶음. 앞의 쓰기가 실패하면 뒤의 쓰기는 하지 않는다(계좌 등록 실패 후 환불 접수 같은 일을 막는다)."""
        results = []
        for call in writes:
            r = await self._call(call, my_v, out)
            results.append(r)
            if not r.ok and r.error_code != "DUPLICATE_REQUEST":
                break
            if call.tool == "register_refund_account" and not (r.data or {}).get("registered"):
                break
        if origin is not None and (origin is not self._task or origin.done()):
            # 시작한 파이프라인이 없어졌다. 쓰기 결과도 시스템 사실을 바꾸므로 버전을 올리고 최신 발화로 다시 진행한다.
            out.append(self._bump("tool", cancel=True, tools=[c.tool for c in writes], background=True))
        return results

    # --- 생성과 노출 ----------------------------------------------------------------------------

    def _generation_payload(self, plan: policy.Plan) -> dict[str, Any]:
        f = self.state.facts
        ids = list(self.state.evidence.selected) if self.state.evidence else []
        for q in sorted(f.quotes.values(), key=lambda q: q.status != "eligible"):
            ids += [a for a in q.article_ids if a not in ids and a in self.articles]
        for rec in f.receipts.values():
            ids += [a for a in rec.get("article_ids", []) if a not in ids and a in self.articles]
        claims = {k: recs[-1].value for k, recs in self.state.claims.items()}
        facts: dict[str, Any] = {}
        if f.customer:
            facts["customer"] = {"name": f.customer.get("name"), "status": f.customer.get("status")}
        for k in ("subscriptions", "payments", "usage", "incidents"):
            if getattr(f, k) is not None:
                facts[k] = getattr(f, k)
        if f.receipts:
            facts["receipts"] = f.receipts
        if f.account_rejections:
            facts["refund_account_rejections"] = f.account_rejections
        if f.api_failures:
            facts["api_failures"] = f.api_failures
        return {
            "today": self.org.today.isoformat(),
            "plan": {"action": plan.action, "rule": plan.rule, "ask": plan.ask, "next_info": plan.next_info,
                     "notices": list(plan.notices), "proposal": plan.proposal.summary if plan.proposal else None,
                     "detail": plan.detail},
            "intent": self.state.request,
            "identity_verified": self.state.identity.verified,
            "sufficient": self.state.evidence.sufficient if self.state.evidence else None,
            "articles": [{"id": a, "title": self.articles[a].title, "type": self.articles[a].type,
                          "text": self.articles[a].text} for a in ids],
            "quotes": {r: quote_summary(q) for r, q in f.quotes.items()},
            "system_facts": facts,
            "customer_claims": claims,
            "conversation": [{"speaker": t.speaker, "text": t.text}
                             for t in self.state.conversation[-CONVERSATION_FOR_GENERATION:]],
        }

    def _expose(self, plan: policy.Plan, g: gen.GenerationResult, my_v: int, out: list[dict[str, Any]]) -> None:
        self._n_suggestions += 1
        sid = f"{self.session_id}-s{self._n_suggestions}"
        prev = self.state.latest_suggestion()
        if prev is not None:
            prev.status = "superseded"
        # 고정 문구로 대신했으면 접수 내용을 안내하지 못했으므로 동의 대상으로 두지 않는다
        proposal = plan.proposal if plan.rule == "proposal" and not g.fallback else None
        s = Suggestion(sid, my_v, g.say, plan.action, self.state.request, plan.rule, g.cited_articles, g.amount,
                       g.amount_final, proposal)
        self.state.suggestions.append(s)
        out.append(self.log.emit(ev.SUGGESTION_CREATED, my_v, suggestion_id=sid, basis_version=my_v, text=g.say,
                                 action=plan.action, rule=plan.rule, intent=self.state.request,
                                 cited_articles=g.cited_articles, amount=g.amount, amount_final=g.amount_final,
                                 proposal=({"signature": proposal.signature, "kind": proposal.kind,
                                            "writes": [w.tool for w in proposal.writes]} if proposal else None),
                                 fallback=g.fallback))
        self._n_agent += 1
        self.state.add_turn(f"a{self._n_agent}", "agent", g.say)  # 엔진 발화는 버전을 올리지 않는다
        s.exposed = True
        t = now_ms()
        out.append(self.log.emit(ev.AGENT_SAY, my_v, t_ms=t, text=g.say, suggestion_id=sid))
        out.append(self.log.emit(ev.SUGGESTION_EXPOSED, my_v, t_ms=t, suggestion_id=sid, channel=CHANNEL,
                                 start_ms=t, end_ms=t))
