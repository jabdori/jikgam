"""시뮬레이터와 평가기가 쓰는 Gemini JSON 호출(REST).

요청 형식은 jikgam/decision/llm.py, jikgam/engine/generate.py와 같다(systemInstruction, responseJsonSchema,
thinkingConfig). 모델 ID, 키, 단가, 사고 수준은 config/decision_models.toml의 이름으로 가져온다.
출력 과금에는 사고 토큰을 포함한다(docs/evaluation.md §2.3).

재시도: HTTP 429·5xx와 연결 오류는 2, 4, 8초 뒤 최대 3번 다시 보낸다(docs/evaluation.md §2.4와 같은 규칙).
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

RETRY_DELAYS_S = (2, 4, 8)


@dataclass
class JsonReply:
    data: dict[str, Any] | None
    model: str
    latency_ms: float = 0.0  # 마지막 시도의 요청 전송부터 응답 수신까지
    input_tokens: int = 0
    output_tokens: int = 0  # 사고 토큰 포함
    thoughts_tokens: int = 0
    cost_usd: float = 0.0  # 모든 시도의 합
    attempts: int = 1
    error: str | None = None

    def usage(self) -> dict[str, Any]:
        return {"model": self.model, "latency_ms": round(self.latency_ms, 1), "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "thoughts_tokens": self.thoughts_tokens,
                "cost_usd": self.cost_usd, "attempts": self.attempts, "error": self.error}


class GeminiJson:
    def __init__(self, name: str, model: str, api_key: str, price_input_per_mtok: float, price_output_per_mtok: float,
                 thinking_level: str | None = None, timeout_s: float = 180.0,
                 transport: httpx.AsyncBaseTransport | None = None, retry_delays_s: tuple[float, ...] = RETRY_DELAYS_S):
        self.name = name
        self.model = model
        self.thinking_level = thinking_level
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.retry_delays_s = retry_delays_s
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": api_key}, timeout=timeout_s, transport=transport)

    @classmethod
    def from_config(cls, name: str, **kw: Any) -> "GeminiJson":
        from jikgam.decision.registry import load_config, secrets

        cfg = load_config()[name]
        if cfg["kind"] != "gemini":
            raise ValueError(f"gemini 설정만 쓸 수 있다: {name}")
        return cls(name, cfg["model"], secrets()[cfg["api_key_env"]], cfg["price_input_per_mtok"],
                   cfg["price_output_per_mtok"], thinking_level=cfg.get("thinking_level"), **kw)

    async def ask(self, system: str, user: str, schema: dict[str, Any], seed: int | None = None) -> JsonReply:
        """seed는 generationConfig.seed로 보낸다. 같은 seed라도 출력이 같다는 보장은 없다(재현을 돕는 기록용)."""
        gen: dict[str, Any] = {"responseMimeType": "application/json", "responseJsonSchema": schema}
        if seed is not None:
            gen["seed"] = seed
        if self.thinking_level:
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        cost = 0.0
        attempt = 0
        while True:
            attempt += 1
            reply = await self._once(body)
            cost += reply.cost_usd
            retryable = reply.error is not None and reply.data is None and reply.error.startswith(
                ("HTTP 429", "HTTP 5", "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError", "ReadError"))
            if not retryable or attempt > len(self.retry_delays_s):
                reply.cost_usd, reply.attempts = cost, attempt
                return reply
            await asyncio.sleep(self.retry_delays_s[attempt - 1])

    async def _once(self, body: dict[str, Any]) -> JsonReply:
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            return JsonReply(None, self.model, (time.perf_counter() - t0) * 1000, error=f"{type(e).__name__}: {e}")
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return JsonReply(None, self.model, latency, error=f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        thoughts = int(usage.get("thoughtsTokenCount", 0))
        total = usage.get("totalTokenCount")
        tout = int(total) - tin if total is not None else int(usage.get("candidatesTokenCount", 0)) + thoughts
        reply = JsonReply(None, str(data.get("modelVersion") or f"{self.model} (응답에 모델 없음)"), latency, tin, tout,
                          thoughts, (tin * self.price_in + tout * self.price_out) / 1e6)
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 객체가 아니다")
            reply.data = parsed
        except (KeyError, IndexError, ValueError, TypeError) as e:
            reply.error = f"출력 해석 실패: {type(e).__name__}: {str(data)[:300]}"
        return reply

    async def aclose(self) -> None:
        await self.http.aclose()
