"""OpenAI 호환 생성형 모델로 판별 질문에 답하는 어댑터(ADR 0021, ADR 0007 확장).

POST {url}/chat/completions에 생성형 LLM 판단(jikgam.decision.llm)과 같은 시스템 프롬프트와 질문을 보내고, 답은
JSON 스키마(response_format json_schema)로 강제한다. 확률은 모델이 직접 말한 값이다. 새 모델은 설정 파일
(config/decision_models.d/<이름>.toml, kind = "openai")만 더해 붙인다.
"""

import json
import time
from typing import Any

import httpx

from .llm import PROMPT_VERSION, SYSTEM_PROMPT, response_schema, to_answers, user_message
from .types import DecisionResult, to_wire

__all__ = ["OpenAICompatJudgeClient", "PROMPT_VERSION"]


class OpenAICompatJudgeClient:
    def __init__(self, name: str, url: str, model: str, api_key: str | None, price_input_per_mtok: float,
                 price_output_per_mtok: float, timeout_s: float = 120.0, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.model = model
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.url = url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(headers=headers, timeout=timeout_s, transport=transport)

    async def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult:
        wired, back = to_wire(questions)
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user_message(state, wired)}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "decisions", "schema": response_schema(wired)}}}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
            latency = (time.perf_counter() - t0) * 1000
        except httpx.HTTPError as e:
            return DecisionResult(self.name, self.model, {}, latency_ms=(time.perf_counter() - t0) * 1000,
                                  error=f"{type(e).__name__}: {e}")
        if r.status_code != 200:
            return DecisionResult(self.name, self.model, {}, latency_ms=latency, error=f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        result = DecisionResult(self.name, str(data.get("model") or f"{self.model} (응답에 모델 없음)"), {},
                                input_tokens=tin, output_tokens=tout, latency_ms=latency,
                                cost_usd=(tin * self.price_in + tout * self.price_out) / 1e6, meta={"response_id": data.get("id")})
        try:
            text = data["choices"][0]["message"]["content"]
            result.answers = {back[k]: v for k, v in to_answers(json.loads(text), wired).items()}
        except (KeyError, IndexError, TypeError, ValueError) as e:
            result.error = f"출력 해석 실패: {type(e).__name__}: {str(data)[:300]}"
            return result
        missing = [q for q in questions if q not in result.answers]
        if missing:
            result.error = f"답 없음: {missing}"
        return result

    async def aclose(self) -> None:
        await self.http.aclose()
