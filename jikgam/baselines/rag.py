"""정책 A: 개선형 RAG 기준선. docs/evaluation.md §3.4, §5.1.

엔진(jikgam/engine/session.py)과 같은 인터페이스(RagSession(org, session_id, log_dir), await start(), await handle(event),
await aclose())와 같은 이벤트 로그 형식(jikgam/engine/events.py)을 쓴다. 같은 재생 파일과 같은 지표 코드로 정책을
비교하려는 것이다.

§5.1을 이렇게 구현했다.
- 문서 처리와 검색: 엔진과 같은 조항 색인, 같은 키워드 검색(BM25)과 구조 확장(jikgam/engine/retrieval.py, 같은 조직
  설정). §5.1의 임베딩 검색은 엔진에도 아직 없으므로 둘 다 키워드 검색이다. 판별 계층이 없어 검색 상태의 요청(intent)은
  비우고, 업무 API 결과에서 바로 나오는 상태(본인 확인 여부·실패, 승인 필요 견적, 인계 사유, 견적·접수의 근거 조항)만
  넣는다. 그래서 요청 라벨로 붙는 절차 조항 규칙(retrieval.rules의 intent)은 RAG에서 걸리지 않는다.
- 판별 계층, 대화 버전, 무효화가 없다. 고객의 확정 발화마다 생성형 LLM을 한 번 부른다. 대화 기록(이전 발화, 이전 답,
  도구 호출과 결과)을 그대로 이어 보내고, 이번 발화와 이번에 검색한 조항을 새 사용자 메시지로 붙인다.
- 업무 API는 Gemini 함수 호출 도구로 준다. 모델이 함수를 부르면 실행해 결과를 돌려주고 다시 부르는 도구 루프를 돈다
  (한 발화에 LLM 요청 최대 MAX_STEPS번). 마지막 답은 final_answer 함수로 받는다(say, cited_articles, amount,
  amount_final, action). 함수 호출 방식을 ANY로 두어 모델이 매 단계 함수를 부르게 한다. 구조화된 답을 확실히 받기
  위해서다. 한 발화의 LLM 요청 묶음을 "생성형 LLM 호출 한 번"으로 본다.
- 생성 모델은 엔진의 응답 생성 모델과 같은 설정(조직 설정 generator, config/decision_models.toml)이다.
- 운영 규칙(본인 확인 먼저, 금액은 견적만, 동의 뒤 접수, 제3자 거절, 장애 시 보류, 인계, 안내 의무)은 시스템 프롬프트에
  적는다. 엔진은 같은 규칙을 코드로 둔다. 기준선을 일부러 약하게 만들지 않으려는 것이며, 비교는 "규칙을 프롬프트로만
  지키는 LLM" 대 "판별 + 코드 정책"이다. 생성 결과의 금액 검사(엔진의 generate.check)도 하지 않는다.
- 업무 API 호출은 엔진과 같은 클라이언트(jikgam/engine/bizapi.py)로 한다. 세션 헤더, 재시도 예산(1회), 쓰기의 멱등 키
  ((세션, 도구, 대상), 대상은 엔진과 같은 규칙)는 실행 배관이며 정책이 아니다. 같은 도구를 다시 부를지는 모델이 정한다.
  선언하지 않은 인자는 빼고 보낸다(업무 API가 모르는 필드를 거절하므로).
- 진행 중인 생성은 새 발화가 와도 취소하지 않는다(§5.1). 발화는 받은 순서대로 하나씩 답한다. 앞 발화의 답을 만드는 동안
  새 발화가 오면 utterance.end만 바로 남기고, 앞 답이 끝나 노출된 뒤에 새 발화의 답을 만든다. 채팅은 고객과 차례로
  주고받으므로 실제로 겹치는 일은 드물다. 음성에서는 고객이 말하는 동안에도 이전 발화의 답이 계속 만들어져 끝나면 그대로
  재생되므로, 고객이 조건을 바꾼 뒤에 지난 조건으로 만든 답이 나갈 수 있다. 엔진은 이 경우 작업을 취소하거나 결과를 버린다.
- 예열이 없다. 판별 백엔드가 없고, 엔진도 생성 모델은 예열하지 않는다(start는 아무것도 하지 않는다).

이벤트 로그(지표가 쓰는 것):
- utterance.end: version은 그 발화 전까지의 고객 확정 발화 수.
- evidence.updated: 답이 나온 시점에 남긴다. version은 그 답을 만들 때 본 고객 확정 발화 수, articles는 답이 근거로 든
  조항(조항 색인에 있는 것), candidates는 검색한 조항. RAG에서 근거 집합과 다음 행동이 다시 정해지는 시점은 LLM 답이
  나온 때이므로, 검색 직후가 아니라 이때를 근거 갱신으로 본다.
- suggestion.created(기준 버전 = 위 version), agent.say(구조화된 amount, amount_final, cited_articles), suggestion.exposed.
- tool.call, tool.result는 엔진과 같은 필드. generation.request, generation.response는 LLM 요청마다(도구 루프의 단계마다).
- version.bumped, suggestion.invalidated, result.discarded, task.cancelled는 남기지 않는다(그런 구조가 없다).
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from jikgam.engine import events as ev
from jikgam.engine import generate as gen
from jikgam.engine.bizapi import WRITE_TOOLS, BizApiClient, ToolResult, mask_args
from jikgam.engine.events import EventLog, now_ms, parse_utterance
from jikgam.engine.org import OrgConfig
from jikgam.engine.retrieval import ArticleIndex, RetrievalContext, parse_terms

PROMPT_VERSION = "rag-2026.10.14"
MAX_STEPS = 8  # 한 발화에서 LLM 요청(도구 루프 단계) 최대 수
CHANNEL = "chat"
FINAL = "final_answer"
ACTIONS = ("answer", "ask", "hold", "handoff")  # 엔진 정책의 행동과 같은 값

SYSTEM_PROMPT = """당신은 구독 서비스 고객센터의 채팅 상담원이다. 사용자 메시지에는 오늘 날짜(today), 고객의 새 발화(customer_utterance), 그 발화로 검색한 약관 조항(retrieved_articles)이 JSON으로 들어 있다. 이전 대화와 도구 결과는 대화 기록에 있다.

업무 시스템은 함수로 호출한다. 고객에게 보낼 답은 반드시 final_answer 함수로 낸다(이번 발화에 대한 메시지 하나).

지켜야 할 것:
1. 약관 조항과 도구 결과만 근거로 답한다. 조항에 없는 규정을 만들지 않는다.
2. 고객 계정의 정보 조회와 해지·환불 처리는 본인 확인(verify_identity: 가입 휴대폰 번호와 고객이 받은 인증번호) 뒤에만 한다. 인증번호는 고객이 말한 값을 숫자로 바꿔 그대로 쓴다. 시스템은 문자를 보내지 않으므로 보냈다고 말하지 않는다.
3. 고객이 말한 날짜·금액·횟수는 고객 진술이다. 시스템 기록과 다르면 기록을 따른다.
4. 환불 금액과 환불 가능 여부는 quote_refund 결과만 쓴다. 직접 계산하거나 추정하지 않는다. 견적이 없으면 환불 금액이나 환불 가능성을 말하지 않는다.
5. 쓰기(create_cancellation, create_refund_request, register_refund_account)는 접수할 내용을 고객에게 안내하고 고객이 동의한 뒤에만 호출한다. 접수 결과가 나온 뒤에만 접수됐다고 말한다.
6. 계정 소유자 본인이 아닌 사람(가족·지인)의 해지·환불 요청은 접수하지 않고 계정 정보도 알려 주지 않는다. 약관의 일반 내용은 안내할 수 있다.
7. 업무 시스템 오류로 확인할 수 없으면 금액을 추정하지 않고 확답을 보류한다.
8. 도구가 HANDOFF_REQUIRED나 handoff_required를 돌려주는 등 상담사 처리가 필요하면 create_handoff를 호출하고 상담사 연결을 안내한다.
9. 해지·환불을 안내하거나 접수할 때는 다음을 안내한다: {notices}.
10. final_answer: say는 존댓말 채팅 2~4문장이고 내부 코드 값(영어 필드 이름, 견적 ID)은 쓰지 않는다. cited_articles는 근거로 쓴 조항 ID. amount는 이 메시지에서 환불 예정액이나 환불액으로 안내한 금액(원, 정수, 없으면 null). amount_final은 그 금액을 견적으로 확정된 금액으로 안내했으면 true. action은 answer(안내·처리), ask(고객에게 정보를 물음), hold(확답 보류), handoff(상담사 연결) 중 하나."""


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_S = {"type": "string"}
_CID = {"customer_id": _S}


def tool_declarations(quote_reasons: list[str]) -> list[dict[str, Any]]:
    """업무 API(domain/org_a/api.md)와 final_answer의 함수 선언. 견적 사유 목록은 조직 설정(policy.quote_priority)에서 받는다."""
    date = {"type": "string", "description": "YYYY-MM-DD"}
    return [
        {"name": "verify_identity", "parametersJsonSchema": _obj({"phone": _S, "otp": _S}, ["phone", "otp"]),
         "description": "본인 확인. 가입 휴대폰 번호와 고객이 받은 인증번호를 확인한다. 같은 세션에서 2회 실패하면 더 할 수 없고 상담사 연결이 필요하다."},
        {"name": "get_customer", "parametersJsonSchema": _obj(_CID, ["customer_id"]),
         "description": "고객 정보: 이름, 생년월일, 계정 상태(active, suspended)."},
        {"name": "list_subscriptions", "parametersJsonSchema": _obj(_CID, ["customer_id"]),
         "description": "구독 목록: 요금제, 가격, 프로모션 여부, 시작일, 다음 결제일, 체험 여부."},
        {"name": "list_payments", "parametersJsonSchema": _obj(_CID, ["customer_id"]),
         "description": "결제 목록: 결제일, 금액, 수단, 이의 제기 여부."},
        {"name": "get_usage", "parametersJsonSchema": _obj({**_CID, "since": date}, ["customer_id"]),
         "description": "이용 이력: 날짜, 유형, 체험 기간 여부. since는 생략할 수 있다."},
        {"name": "list_incidents", "parametersJsonSchema": _obj({"from": date, "to": date}, []),
         "description": "서비스 장애 기록: 시작, 종료, 시간, 귀책. from, to는 생략할 수 있다."},
        {"name": "quote_refund",
         "parametersJsonSchema": _obj({**_CID, "subscription_id": _S, "reason": {"type": "string", "enum": quote_reasons},
                                       "request_date": date}, ["customer_id", "subscription_id", "reason", "request_date"]),
         "description": "환불 견적. 환불 가능 여부, 환불액, 결제별 환불 방법과 기간, 계산 내역, 근거 조항, 승인·인계 필요 여부, "
                        "이용 가능 마지막 날을 업무 시스템이 계산한다. reason: withdrawal(청약철회), annual_cancel(연 구독 중도 "
                        "해지), duplicate(중복 결제), incident(장애), minor(미성년자), trial_cancel(체험 해지), plan_change(월→연 "
                        "변경 차감액)."},
        {"name": "create_cancellation", "parametersJsonSchema": _obj({**_CID, "subscription_id": _S},
                                                                     ["customer_id", "subscription_id"]),
         "description": "구독 해지 접수(쓰기). 본인 확인 뒤, 고객이 내용에 동의한 뒤에만 부른다. 이용 가능 마지막 날을 돌려준다."},
        {"name": "create_refund_request", "parametersJsonSchema": _obj({**_CID, "payment_id": _S, "quote_id": _S},
                                                                       ["customer_id", "payment_id", "quote_id"]),
         "description": "환불 접수(쓰기). quote_refund의 quote_id와 결제 ID로 결제마다 접수한다. 본인 확인 뒤, 고객 동의 뒤에만 부른다."},
        {"name": "register_refund_account",
         "parametersJsonSchema": _obj({**_CID, "bank": _S, "account": _S, "holder": _S},
                                      ["customer_id", "bank", "account", "holder"]),
         "description": "환불 계좌 등록(쓰기). 예금주가 회원 본인이 아니면 거절된다."},
        {"name": "create_handoff", "parametersJsonSchema": _obj({**_CID, "reason": _S, "summary": _S}, ["reason"]),
         "description": "상담사 인계 접수. 본인 확인 전에도 쓸 수 있다. customer_id는 알 때만 넣는다."},
        {"name": FINAL,
         "parametersJsonSchema": _obj({"say": _S, "cited_articles": {"type": "array", "items": _S},
                                       "amount": {"type": ["integer", "null"]}, "amount_final": {"type": "boolean"},
                                       "action": {"type": "string", "enum": list(ACTIONS)}},
                                      ["say", "cited_articles", "amount", "amount_final", "action"]),
         "description": "이번 고객 발화에 대한 답. 반드시 이 함수로 답을 낸다."},
    ]


# 쓰기의 멱등 키 대상. 엔진 정책(jikgam/engine/policy.py)과 같은 규칙이다.
TARGETS: dict[str, Callable[[dict[str, Any]], str]] = {
    "create_cancellation": lambda a: f"cancel:{a.get('subscription_id')}",
    "create_refund_request": lambda a: f"refund:{a.get('payment_id')}",
    "register_refund_account": lambda a: f"account:{a.get('customer_id')}",
    "create_handoff": lambda a: f"handoff:{a.get('reason')}",
}


# --- 생성형 LLM(함수 호출) ------------------------------------------------------------------------


@dataclass
class LLMStep:
    """도구 루프의 한 단계(LLM 요청 하나)의 결과."""

    content: dict[str, Any] | None  # 모델 응답 content. 대화 기록에 그대로 붙인다(사고 서명 포함)
    calls: list[dict[str, Any]]  # [{"name", "args", "id"?}]
    model: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0  # 사고 토큰 포함
    cost_usd: float = 0.0
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ToolLLM(Protocol):
    name: str
    model: str

    async def step(self, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMStep: ...

    async def aclose(self) -> None: ...


class GeminiToolLLM:
    """Gemini generateContent(REST) 함수 호출. 함수 호출 방식 ANY: 매 단계 함수를 하나 이상 부른다."""

    def __init__(self, name: str, model: str, api_key: str, price_input_per_mtok: float, price_output_per_mtok: float,
                 thinking_level: str | None = None, timeout_s: float = 60.0):
        self.name = name
        self.model = model
        self.thinking_level = thinking_level
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": api_key}, timeout=timeout_s)

    async def step(self, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMStep:
        body: dict[str, Any] = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                                "tools": [{"functionDeclarations": tools}],
                                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}
        if self.thinking_level:
            body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": self.thinking_level}}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            return LLMStep(None, [], self.model, (time.perf_counter() - t0) * 1000, error=f"{type(e).__name__}: {e}")
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return LLMStep(None, [], self.model, latency, error=f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        total = usage.get("totalTokenCount")
        tout = int(total) - tin if total is not None else int(usage.get("candidatesTokenCount", 0)) + \
            int(usage.get("thoughtsTokenCount", 0))
        step = LLMStep(None, [], str(data.get("modelVersion") or f"{self.model} (응답에 모델 없음)"), latency, tin, tout,
                       (tin * self.price_in + tout * self.price_out) / 1e6,
                       meta={"thoughts_tokens": int(usage.get("thoughtsTokenCount", 0)),
                             "response_id": data.get("responseId")})
        try:
            cand = data["candidates"][0]
            step.content = cand.get("content") or {}
            step.calls = [p["functionCall"] for p in step.content.get("parts") or [] if "functionCall" in p]
            step.meta["finish_reason"] = cand.get("finishReason")
        except (KeyError, IndexError, TypeError) as e:
            step.error = f"출력 해석 실패: {type(e).__name__}: {str(data)[:300]}"
            return step
        if not step.calls:
            step.error = f"함수 호출 없음(finishReason={cand.get('finishReason')})"
        return step

    async def aclose(self) -> None:
        await self.http.aclose()


def make_tool_llm(name: str) -> GeminiToolLLM:
    """config/decision_models.toml의 이름으로 만든다. 엔진의 응답 생성기(generate.make_generator)와 같은 설정을 쓴다."""
    from jikgam.decision.registry import load_config, secrets

    cfg = load_config()[name]
    if cfg["kind"] != "gemini":
        raise ValueError(f"RAG 생성 모델은 gemini 설정만 쓸 수 있다: {name}")
    return GeminiToolLLM(name, cfg["model"], secrets()[cfg["api_key_env"]], cfg["price_input_per_mtok"],
                         cfg["price_output_per_mtok"], thinking_level=cfg.get("thinking_level"))


# --- 세션 ------------------------------------------------------------------------------------------


@dataclass
class _ToolState:
    """검색 상태에 넣을 업무 API 결과(판별 계층 없이 도구 결과에서 바로 나오는 것만)."""

    verified: bool = False
    identity_failed: bool = False
    approval_required: bool = False
    handoff: bool = False
    article_ids: list[str] = field(default_factory=list)

    def observe(self, r: ToolResult) -> None:
        d = r.data or {}
        err = r.error or {}
        ids = list(d.get("article_ids") or []) + list(err.get("article_ids") or [])
        self.article_ids += [a for a in ids if a not in self.article_ids]
        if r.tool == "verify_identity" and r.ok:
            self.verified = self.verified or bool(d.get("verified"))
            self.identity_failed = self.identity_failed or int(d.get("failed_attempts") or 0) > 0
        if d.get("handoff_required") or r.error_code == "HANDOFF_REQUIRED":
            self.handoff = True
        if r.tool == "quote_refund" and r.ok and d.get("eligible") and d.get("approval_required"):
            self.approval_required = True


def _final_fields(args: dict[str, Any]) -> tuple[str, list[str], int | None, bool, str]:
    say = str(args.get("say") or "").strip()
    cited = [str(a) for a in args.get("cited_articles") or [] if a]
    amt = args.get("amount")
    amount = int(amt) if isinstance(amt, (int, float)) and not isinstance(amt, bool) else None
    action = args.get("action") if args.get("action") in ACTIONS else "answer"
    return say, cited, amount, bool(args.get("amount_final")), action


class RagSession:
    """정책 A 세션. 엔진 Session과 같은 방식으로 쓴다: start → handle(입력 이벤트)* → aclose."""

    def __init__(self, org: OrgConfig, session_id: str, log_dir: Path, *, llm: ToolLLM | None = None,
                 bizapi: BizApiClient | None = None, max_steps: int = MAX_STEPS):
        """llm, bizapi는 테스트에서 가짜를 넣을 때만 쓴다. 기본은 조직 설정으로 만든다."""
        self.org = org
        self.session_id = session_id
        self.log = EventLog(Path(log_dir) / f"{session_id}.jsonl", session_id)
        self.articles = parse_terms(org.terms)
        self.index = ArticleIndex(self.articles, org.retrieval)
        self.llm = llm or make_tool_llm(org.generator)
        self.biz = bizapi or BizApiClient(org.bizapi_url, session_id)
        self.max_steps = max_steps
        self.tools = tool_declarations(list(org.policy.quote_priority))
        self._declared = {t["name"]: set(t["parametersJsonSchema"]["properties"]) for t in self.tools}
        self.system = SYSTEM_PROMPT.format(notices=", ".join(org.required_notices))
        self.history: list[dict[str, Any]] = []  # Gemini contents(사용자 메시지, 모델 함수 호출, 함수 결과, 모델 답)
        self.customer_texts: list[str] = []
        self.tool_state = _ToolState()
        self.n_final = 0  # 받은 고객 확정 발화 수
        self._lock = asyncio.Lock()  # 답은 받은 순서대로 하나씩. 진행 중인 답은 취소하지 않는다
        self._notes: list[str] = []  # 다음 사용자 메시지에 붙일 사람 상담사 발화
        self._n_calls = 0
        self._n_suggestions = 0
        self.log.emit(ev.SESSION_START, 0, org=org.org, today=org.today.isoformat(), policy="A", engine="rag",
                      decision_backend=None, generator=self.llm.model, rag_prompt=PROMPT_VERSION, max_steps=max_steps,
                      max_candidates=org.retrieval.max_candidates, bizapi_url=org.bizapi_url, channel=CHANNEL)

    @property
    def version(self) -> int:
        """받은 고객 확정 발화 수. RAG에는 대화 버전이 없으므로 이벤트의 version 필드에 이 값을 쓴다."""
        return self.n_final

    async def start(self) -> None:
        """예열할 판별 백엔드가 없다. 엔진과 같은 실행기 코드를 쓰려고 둔다."""

    async def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        received = now_ms()
        utt = parse_utterance(event, received)
        out: list[dict[str, Any]] = []
        if not utt.final:
            out.append(self.log.emit(ev.STT_PARTIAL, self.n_final, utt_id=utt.utt_id, text=utt.text))
            return out
        out.append(self.log.emit(ev.UTTERANCE_END, self.n_final, t_ms=utt.t_end_ms, utt_id=utt.utt_id,
                                 speaker=utt.speaker, text=utt.text, final=True, marker=utt.marker, received_ms=received))
        if utt.speaker != "customer":
            self._notes.append(utt.text)
            return out
        self.n_final += 1
        basis = self.n_final
        async with self._lock:
            await self._answer(utt.text, basis, out)
        return out

    async def aclose(self) -> None:
        async with self._lock:
            pass
        await self.llm.aclose()
        await self.biz.aclose()
        self.log.close()

    # --- 한 발화의 답 ---------------------------------------------------------------------------

    def _retrieval_context(self) -> RetrievalContext:
        s = self.tool_state
        return RetrievalContext(customer_texts=tuple(self.customer_texts), intent=None, identity_verified=s.verified,
                                identity_failed=s.identity_failed, approval_required=s.approval_required,
                                handoff_reason=s.handoff, system_article_ids=tuple(s.article_ids))

    async def _answer(self, text: str, basis: int, out: list[dict[str, Any]]) -> None:
        self.customer_texts.append(text)
        t0 = now_ms()
        result = self.index.search(self._retrieval_context())
        out.append(self.log.emit(ev.RETRIEVAL_DONE, self.n_final, started_version=basis,
                                 duration_ms=round(now_ms() - t0, 3), candidates=result.candidates,
                                 sources=result.sources, query=result.query))
        msg: dict[str, Any] = {"today": self.org.today.isoformat(), "customer_utterance": text,
                               "retrieved_articles": [{"id": a, "title": self.articles[a].title,
                                                       "type": self.articles[a].type, "text": self.articles[a].text}
                                                      for a in result.candidates]}
        if self._notes:
            msg["agent_utterances"], self._notes = self._notes, []
        self.history.append({"role": "user", "parts": [{"text": json.dumps(msg, ensure_ascii=False, indent=1)}]})
        final: dict[str, Any] | None = None
        error: str | None = None
        tools_used: list[str] = []
        for step in range(1, self.max_steps + 1):
            out.append(self.log.emit(ev.GENERATION_REQUEST, self.n_final, request_version=basis, model=self.llm.model,
                                     step=step, articles=result.candidates))
            r = await self.llm.step(self.system, self.history, self.tools)
            out.append(self.log.emit(ev.GENERATION_RESPONSE, self.n_final, request_version=basis, model=r.model,
                                     step=step, latency_ms=round(r.latency_ms, 1), input_tokens=r.input_tokens,
                                     output_tokens=r.output_tokens, cost_usd=r.cost_usd,
                                     calls=[c.get("name") for c in r.calls], error=r.error, meta=r.meta))
            if r.error:
                error = r.error
                break
            fin = next((c for c in r.calls if c.get("name") == FINAL), None)
            others = [c for c in r.calls if c.get("name") != FINAL]
            if others:
                parts = [p for p in (r.content or {}).get("parts") or []
                         if (p.get("functionCall") or {}).get("name") != FINAL]
                self.history.append({"role": "model", "parts": parts})
                results = await asyncio.gather(*(self._tool(c, basis, out) for c in others))
                tools_used += [c.get("name") for c in others]
                self.history.append({"role": "user", "parts": [
                    {"functionResponse": {k: v for k, v in (("name", c.get("name")), ("id", c.get("id")),
                                                             ("response", res)) if v is not None}}
                    for c, res in zip(others, results)]})
            if fin is not None:
                final = dict(fin.get("args") or {})
                break
        else:
            error = f"{self.max_steps}단계 안에 final_answer가 없다"
        self._expose(final, error, result.candidates, tools_used, basis, out)

    async def _tool(self, call: dict[str, Any], basis: int, out: list[dict[str, Any]]) -> dict[str, Any]:
        """모델이 부른 업무 API를 실행하고 모델에 돌려줄 결과를 만든다."""
        self._n_calls += 1
        call_id = f"c{self._n_calls}"
        name = str(call.get("name"))
        declared = self._declared.get(name)
        args = {k: (str(v) if k in ("phone", "otp", "account") else v) for k, v in (call.get("args") or {}).items()
                if declared is not None and k in declared and v not in (None, "")}
        target = TARGETS[name](args) if name in WRITE_TOOLS else None
        out.append(self.log.emit(ev.TOOL_CALL, self.n_final, request_version=basis, call_id=call_id, tool=name,
                                 args=mask_args(args), target=target))
        if declared is None:
            r = ToolResult(name, args, False, error_code="UNKNOWN_TOOL",
                           error={"code": "UNKNOWN_TOOL", "message": f"없는 도구: {name}"})
        else:
            try:
                r = await self.biz.call(name, args, target=target)
            except ValueError as e:
                r = ToolResult(name, args, False, error_code="INVALID_REQUEST",
                               error={"code": "INVALID_REQUEST", "message": str(e)})
        out.append(self.log.emit(ev.TOOL_RESULT, self.n_final, request_version=basis, call_id=call_id, tool=name,
                                 ok=r.ok, status=r.status, error_code=r.error_code, attempts=r.attempts,
                                 latency_ms=r.latency_ms, idempotency_key=r.idempotency_key,
                                 data=r.data if r.ok else r.error))
        self.tool_state.observe(r)
        return {"ok": True, "data": r.data} if r.ok else {"ok": False, "error": r.error or {"code": r.error_code}}

    def _expose(self, final: dict[str, Any] | None, error: str | None, candidates: list[str], tools_used: list[str],
                basis: int, out: list[dict[str, Any]]) -> None:
        if final is not None:
            say, cited, amount, amount_final, action = _final_fields(final)
        if final is None or not say:
            # 답을 받지 못하면 금액 없는 보류 문구(엔진의 고정 문구와 같다)
            say, cited, amount, amount_final, action = gen.FALLBACK["hold"], [], None, False, "hold"
            error = error or "final_answer의 say가 비어 있다"
            fallback = True
        else:
            fallback = False
        self.history.append({"role": "model", "parts": [{"text": say}]})
        known = [a for a in dict.fromkeys(cited) if a in self.articles]
        out.append(self.log.emit(ev.EVIDENCE_UPDATED, basis, articles=known, candidates=candidates, sufficient=None,
                                 next_info=None, action=action, intent=None, cited_raw=cited, tools=tools_used,
                                 error=error))
        self._n_suggestions += 1
        sid = f"{self.session_id}-s{self._n_suggestions}"
        out.append(self.log.emit(ev.SUGGESTION_CREATED, basis, suggestion_id=sid, basis_version=basis, text=say,
                                 action=action, rule="rag", intent=None, cited_articles=known, amount=amount,
                                 amount_final=amount_final, proposal=None, fallback=fallback))
        t = now_ms()
        out.append(self.log.emit(ev.AGENT_SAY, basis, t_ms=t, text=say, suggestion_id=sid, cited_articles=known,
                                 amount=amount, amount_final=amount_final))
        out.append(self.log.emit(ev.SUGGESTION_EXPOSED, basis, t_ms=t, suggestion_id=sid, channel=CHANNEL, start_ms=t,
                                 end_ms=t))
