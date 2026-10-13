"""Jev API 형식(state + typed questions → answers)을 쓰는 판별 모델 클라이언트.

TypeSafe의 Jev, Workers AI의 Clef·Clef-flash, Unsloth로 서빙한 공개 모델이 같은 형식을 쓰므로
주소, 인증, 응답 봉투만 설정으로 바꾼다.
"""

import time
from typing import Any

import httpx

from .types import DecisionResult, to_wire


class SystemOneClient:
    def __init__(self, name: str, url: str, model: str, api_key: str | None,
                 price_input_per_mtok: float, price_output_per_mtok: float = 0.0,
                 envelope: str = "plain", timeout_s: float = 30.0):
        self.name = name
        self.url = url
        self.model = model
        self.envelope = envelope  # "plain"(TypeSafe) 또는 "cloudflare"({"result": ...} 로 감쌈)
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(headers=headers, timeout=timeout_s)

    async def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult:
        wired, back = to_wire(questions)
        body = {"model": self.model, "state": state, "questions": wired}
        t0 = time.perf_counter()
        try:
            r = await self.http.post(self.url, json=body)
            latency = (time.perf_counter() - t0) * 1000
        except httpx.HTTPError as e:
            return DecisionResult(self.name, self.model, {}, latency_ms=(time.perf_counter() - t0) * 1000,
                                  error=f"{type(e).__name__}: {e}")
        meta = {k: v for k, v in r.headers.items() if k.lower() in ("x-typesafe-request-id", "cf-ray", "cf-ai-req-id")}
        if r.status_code != 200:
            return DecisionResult(self.name, self.model, {}, latency_ms=latency,
                                  error=f"HTTP {r.status_code}: {r.text[:500]}", meta=meta)
        data = r.json()
        if self.envelope == "cloudflare":
            data = data.get("result") or {}
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        answers = {back[k]: v for k, v in (data.get("answers") or {}).items() if k in back}
        missing = [q for q in questions if q not in answers]
        return DecisionResult(
            backend=self.name, model=str(data.get("model") or f"{self.model} (응답에 모델 없음)"), answers=answers,
            input_tokens=tin, output_tokens=tout, latency_ms=latency,
            cost_usd=(tin * self.price_in + tout * self.price_out) / 1e6,
            error=f"답 없음: {missing}" if missing else None, meta=meta,
        )

    async def aclose(self) -> None:
        await self.http.aclose()
