"""응답 생성. docs/engine.md §6.

생성형 LLM(Gemini, REST)은 정책이 정한 행동, 선택한 조항, 시스템 사실, 견적 결과, 필수 안내만 받아 JSON
{say, cited_articles, amount, amount_final}을 낸다. 요청 형식은 jikgam/decision/llm.py와 같다.

생성 결과는 코드로 검사한다.
- 문장에 나온 금액(원)은 견적, 시스템 사실(결제·요금), 일반 조항 본문에 있는 금액이어야 한다. 별표의 계산 예시 금액은
  고객의 금액으로 오해될 수 있어 허용하지 않는다.
- amount는 견적이나 접수 결과의 금액이어야 하고, amount_final은 그때만 참일 수 있다.
- cited_articles는 입력한 조항 안에서만 남긴다.
금액 위반이 있으면 지적 내용을 붙여 한 번 다시 만들고, 그래도 위반이거나 호출이 실패하면 금액 없는 고정 문구로 대신한다.
프롬프트를 바꾸면 PROMPT_VERSION을 올린다.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx

from .claims import extract

PROMPT_VERSION = "engine-gen-2026.10.14"

SYSTEM_PROMPT = """당신은 구독 서비스 고객센터의 채팅 상담 메시지를 쓰는 생성기다. 사용자 메시지의 JSON 입력만 근거로, 지금 고객에게 보낼 메시지 하나를 쓴다.

규칙:
1. plan.action을 따른다.
   - ask: plan.ask가 가리키는 정보 하나만 묻는다. phone=본인 확인용 가입 휴대폰 번호, otp=그 번호로 받은 인증번호, otp_retry=인증번호가 맞지 않았다고 알리고 다시 묻기, account=환불받을 본인 명의 계좌(은행, 계좌번호, 예금주), own_account=회원 본인 명의 계좌만 가능하다고 알리고 다시 묻기, account_holder=예금주 이름, plan_type=어느 구독인지. plan.ask가 없으면 plan.next_info를 묻는다.
   - answer: 확인된 사실과 근거 조항으로 안내한다. plan.proposal이 있으면 접수할 내용을 안내한 뒤 이대로 진행할지 고객의 동의를 묻는다. 아직 접수 전이므로 접수했다고 말하지 않는다. system_facts.receipts에 접수 결과가 있으면 접수됐다고 그 결과를 안내한다.
   - hold: 지금은 확답할 수 없다고 알리고 이유와 다음 절차를 말한다. 금액을 추정하지 않는다.
   - handoff: 상담사에게 연결한다고 알린다. 대기 시간이 입력에 있으면 함께 말한다.
2. 금액은 quotes, system_facts에 있는 숫자만 그대로 말한다. 계산하거나 추정하지 않는다. 견적이 없으면 환불 금액을 말하지 않는다.
3. customer_claims는 고객이 말한 내용일 뿐 확인된 사실이 아니다. 시스템 기록과 다르면 기록을 기준으로 안내하고, 확인되지 않은 내용을 사실로 확정하지 않는다.
4. plan.notices의 항목은 입력에 그 정보가 있으면 모두 안내한다.
5. 근거로 쓴 조항 ID를 cited_articles에 넣는다. articles에 없는 ID는 넣지 않는다.
6. amount: 이 메시지에서 환불 예정액이나 환불액으로 안내한 금액(원, 정수). 없으면 null. amount_final: 그 금액을 견적으로 확정된 금액으로 안내했으면 true.
7. 존댓말로 채팅 2~4문장. 내부 필드 이름, 견적 ID, 영어 코드 값(withdrawal, card 등)은 쓰지 않는다.
설명 없이 지정된 JSON만 출력한다."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "cited_articles": {"type": "array", "items": {"type": "string"}},
        "amount": {"type": ["integer", "null"]},
        "amount_final": {"type": "boolean"},
    },
    "required": ["say", "cited_articles", "amount", "amount_final"],
}

# 생성이 실패하거나 금액 검사를 통과하지 못할 때 쓰는 고정 문구. 금액과 확정 표현이 없다.
FALLBACK_ASK = {
    "phone": "본인 확인을 위해 가입하신 휴대폰 번호를 알려 주세요.",
    "otp": "휴대폰으로 받으신 인증번호를 알려 주세요.",
    "otp_retry": "인증번호가 일치하지 않습니다. 받으신 인증번호를 다시 한 번 알려 주세요.",
    "account": "환불받으실 본인 명의 계좌의 은행, 계좌번호, 예금주를 알려 주세요.",
    "own_account": "환불 계좌는 회원 본인 명의여야 합니다. 본인 명의 계좌를 알려 주세요.",
    "account_holder": "계좌의 예금주 성함을 알려 주세요.",
    "plan_type": "어느 구독에 대한 문의인지 알려 주세요.",
}
FALLBACK = {
    "ask": "정확히 안내해 드리기 위해 확인이 필요합니다. 문의하신 내용을 조금 더 자세히 말씀해 주시겠어요?",
    "hold": "지금은 필요한 내용을 확인할 수 없어 바로 확답을 드리기 어렵습니다. 확인되는 대로 다시 안내해 드리겠습니다.",
    "handoff": "상담사에게 연결해 드리겠습니다. 잠시만 기다려 주세요.",
    "answer": "안내 내용을 정리하는 중에 문제가 생겼습니다. 확인되는 대로 다시 안내해 드리겠습니다.",
}


@dataclass
class GenerationResult:
    say: str
    cited_articles: list[str]
    amount: int | None
    amount_final: bool
    model: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    violations: list[str] = field(default_factory=list)
    attempts: int = 1
    fallback: bool = False


class Generator(Protocol):
    name: str
    model: str

    async def generate(self, payload: dict[str, Any], feedback: str | None = None) -> GenerationResult: ...

    async def aclose(self) -> None: ...


class GeminiGenerator:
    def __init__(self, name: str, model: str, api_key: str, price_input_per_mtok: float, price_output_per_mtok: float,
                 thinking_level: str | None = None, timeout_s: float = 60.0):
        self.name = name
        self.model = model
        self.thinking_level = thinking_level
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": api_key}, timeout=timeout_s)

    async def generate(self, payload: dict[str, Any], feedback: str | None = None) -> GenerationResult:
        text = "입력:\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n```"
        if feedback:
            text += "\n\n이전 출력의 문제(고쳐서 다시 쓴다): " + feedback
        gen: dict[str, Any] = {"responseMimeType": "application/json", "responseJsonSchema": RESPONSE_SCHEMA}
        if self.thinking_level:
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        body = {"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": gen}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            return GenerationResult("", [], None, False, self.model, (time.perf_counter() - t0) * 1000,
                                    error=f"{type(e).__name__}: {e}")
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return GenerationResult("", [], None, False, self.model, latency, error=f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        total = usage.get("totalTokenCount")
        tout = int(total) - tin if total is not None else int(usage.get("candidatesTokenCount", 0)) + \
            int(usage.get("thoughtsTokenCount", 0))
        result = GenerationResult("", [], None, False, str(data.get("modelVersion") or f"{self.model} (응답에 모델 없음)"),
                                  latency, tin, tout, (tin * self.price_in + tout * self.price_out) / 1e6)
        try:
            out = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
            parsed = json.loads(out)
            result.say = str(parsed.get("say") or "").strip()
            result.cited_articles = [str(a) for a in parsed.get("cited_articles") or []]
            amt = parsed.get("amount")
            result.amount = int(amt) if isinstance(amt, (int, float)) else None
            result.amount_final = bool(parsed.get("amount_final"))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            result.error = f"출력 해석 실패: {type(e).__name__}: {str(data)[:300]}"
        return result

    async def aclose(self) -> None:
        await self.http.aclose()


def make_generator(name: str) -> GeminiGenerator:
    """config/decision_models.toml의 이름으로 생성기를 만든다(모델 ID, 키, 단가, 사고 수준을 그대로 쓴다)."""
    from jikgam.decision.registry import load_config, secrets

    cfg = load_config()[name]
    if cfg["kind"] != "gemini":
        raise ValueError(f"생성기는 gemini 설정만 쓸 수 있다: {name}")
    return GeminiGenerator(name, cfg["model"], secrets()[cfg["api_key_env"]], cfg["price_input_per_mtok"],
                           cfg["price_output_per_mtok"], thinking_level=cfg.get("thinking_level"))


# --- 검사 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowed:
    amounts: frozenset[int]  # 문장에 나와도 되는 금액
    quote_amounts: frozenset[int]  # amount 필드에 넣을 수 있는 금액(견적·접수 결과)
    articles: frozenset[str]


def _ints(x: Any) -> set[int]:
    if isinstance(x, bool):
        return set()
    if isinstance(x, int):
        return {x}
    if isinstance(x, dict):
        return set().union(*(_ints(v) for v in x.values())) if x else set()
    if isinstance(x, list):
        return set().union(*(_ints(v) for v in x)) if x else set()
    return set()


def allowed_from(payload: dict[str, Any], today: date) -> Allowed:
    """생성 입력에서 허용 금액과 조항을 모은다."""
    quote_amounts = _ints([q.get("refund_amount") for q in payload.get("quotes", {}).values()]) | \
        _ints([[ln.get("amount") for ln in q.get("refunds", [])] for q in payload.get("quotes", {}).values()]) | \
        _ints([r.get("amount") for r in payload.get("system_facts", {}).get("receipts", {}).values()])
    amounts = set(quote_amounts) | _ints(payload.get("quotes", {})) | _ints(payload.get("system_facts", {}))
    for a in payload.get("articles", []):
        if a.get("type") != "table":
            amounts |= {x.won for x in extract(a["text"], today).amounts}
    return Allowed(frozenset(amounts), frozenset(quote_amounts), frozenset(a["id"] for a in payload.get("articles", [])))


def check(result: GenerationResult, allowed: Allowed, today: date) -> list[str]:
    """금액 위반 목록. 조항과 amount_final은 위반으로 보지 않고 sanitize에서 고친다."""
    problems = []
    if not result.say:
        problems.append("say가 비어 있다")
    for a in extract(result.say, today).amounts:
        if a.won not in allowed.amounts:
            problems.append(f"'{a.text}'는 견적이나 시스템 사실에 없는 금액이다")
    if result.amount is not None and result.amount not in allowed.quote_amounts:
        problems.append(f"amount {result.amount}는 견적이나 접수 결과의 금액이 아니다")
    return problems


def sanitize(result: GenerationResult, allowed: Allowed) -> None:
    result.cited_articles = [a for a in dict.fromkeys(result.cited_articles) if a in allowed.articles]
    if result.amount is None or result.amount not in allowed.quote_amounts:
        result.amount_final = False


def fallback(action: str, ask: str | None, model: str) -> GenerationResult:
    text = FALLBACK_ASK.get(ask or "") if action == "ask" else None
    return GenerationResult(text or FALLBACK.get(action, FALLBACK["answer"]), [], None, False, model, fallback=True)


async def generate_checked(gen: Generator, payload: dict[str, Any], today: date) -> GenerationResult:
    """생성 → 검사 → (위반이면) 지적을 붙여 한 번 더 → 그래도 안 되면 고정 문구."""
    allowed = allowed_from(payload, today)
    plan = payload["plan"]
    total_ms, tin, tout, cost = 0.0, 0, 0, 0.0
    feedback = None
    violations: list[str] = []
    for attempt in (1, 2):
        r = await gen.generate(payload, feedback)
        total_ms, tin, tout, cost = total_ms + r.latency_ms, tin + r.input_tokens, tout + r.output_tokens, cost + r.cost_usd
        if r.error:
            violations.append(r.error)
            break
        problems = check(r, allowed, today)
        if not problems:
            sanitize(r, allowed)
            r.latency_ms, r.input_tokens, r.output_tokens, r.cost_usd, r.attempts = total_ms, tin, tout, cost, attempt
            r.violations = violations
            return r
        violations += problems
        feedback = "; ".join(problems) + ". 금액은 입력에 있는 숫자만 쓴다."
    fb = fallback(plan["action"], plan.get("ask"), gen.model)
    fb.latency_ms, fb.input_tokens, fb.output_tokens, fb.cost_usd = total_ms, tin, tout, cost
    fb.violations, fb.attempts = violations, attempt
    fb.error = violations[-1] if violations else None
    return fb
