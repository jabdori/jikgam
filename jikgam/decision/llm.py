"""생성형 LLM으로 같은 판별 질문에 답하는 기준선.

판별 모델과 같은 state와 질문 정의만 받는다. 라벨 기준 문서나 필드 설명은 주지 않는다(입력 정보를 같게 두기 위해).
한 사례의 모든 질문을 한 번의 호출로 묻고, 답은 JSON 스키마로 강제한다. 확률은 모델이 직접 말한 값이다.
프롬프트를 바꾸면 기존 측정과 비교할 수 없으므로 PROMPT_VERSION을 올린다.
"""

import json
import time
from typing import Any

import httpx

from .types import DecisionResult, to_wire

PROMPT_VERSION = "llm-judge-2026.10.14"

SYSTEM_PROMPT = """당신은 판별기다. 사용자 메시지에 상태(state)와 질문 목록(questions)이 JSON으로 주어진다. state만 근거로, 각 질문에 그 질문의 instructions와 criteria에 따라 서로 독립적으로 답한다.
- type이 noul인 질문: instructions의 물음에 대한 답이 '예'일 확률을 p_true(0~1)로 낸다.
- type이 choice인 질문: criteria의 각 선택지가 정답일 확률을 probabilities로 낸다. 합은 1이다.
확률은 그 답이 실제로 맞을 가능성을 반영해야 한다. 설명 없이 지정된 JSON만 출력한다."""


def response_schema(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    props = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            props[qid] = {"type": "object", "properties": {"p_true": {"type": "number"}}, "required": ["p_true"]}
        else:
            opts = list(q["criteria"])
            props[qid] = {"type": "object", "properties": {"probabilities": {
                "type": "object", "properties": {o: {"type": "number"} for o in opts}, "required": opts}},
                "required": ["probabilities"]}
    return {"type": "object", "properties": props, "required": list(questions)}


def user_message(state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> str:
    return ("state:\n```json\n" + json.dumps(state, ensure_ascii=False, indent=1) + "\n```\n\n"
            "questions:\n```json\n" + json.dumps(questions, ensure_ascii=False, indent=1) + "\n```")


def to_answers(parsed: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """모델 출력을 Jev 형식 답으로 바꾼다. 확률은 [0,1]로 자르고 choice는 합이 1이 되게 정규화한다."""
    answers = {}
    for qid, q in questions.items():
        a = parsed.get(qid)
        if not isinstance(a, dict):
            continue
        if q["type"] == "noul":
            p = a.get("p_true")
            if isinstance(p, (int, float)):
                answers[qid] = {"type": "noul", "noul": min(max(float(p), 0.0), 1.0)}
        else:
            raw = a.get("probabilities") or {}
            dist = {o: max(float(raw.get(o, 0) or 0), 0.0) for o in q["criteria"]}
            total = sum(dist.values())
            dist = {o: (v / total if total > 0 else 1 / len(dist)) for o, v in dist.items()}
            answers[qid] = {"type": "choice", "choice": max(dist, key=dist.get), "probabilities": dist}
    return answers


class GeminiJudgeClient:
    def __init__(self, name: str, model: str, api_key: str, price_input_per_mtok: float,
                 price_output_per_mtok: float, thinking_level: str | None = None, timeout_s: float = 120.0):
        self.name = name
        self.model = model
        self.thinking_level = thinking_level
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.http = httpx.AsyncClient(headers={"x-goog-api-key": api_key}, timeout=timeout_s)

    async def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult:
        wired, back = to_wire(questions)
        gen = {"responseMimeType": "application/json", "responseJsonSchema": response_schema(wired)}
        if self.thinking_level:
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        body = {"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": user_message(state, wired)}]}],
                "generationConfig": gen}
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
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0))
        thoughts = int(usage.get("thoughtsTokenCount", 0))
        tout = int(usage.get("candidatesTokenCount", 0)) + thoughts
        result = DecisionResult(self.name, str(data.get("modelVersion", self.model)), {}, input_tokens=tin,
                                output_tokens=tout, latency_ms=latency,
                                cost_usd=(tin * self.price_in + tout * self.price_out) / 1e6,
                                meta={"thoughts_tokens": thoughts, "response_id": data.get("responseId")})
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
            result.answers = {back[k]: v for k, v in to_answers(json.loads(text), wired).items()}
        except (KeyError, IndexError, ValueError) as e:
            result.error = f"출력 해석 실패: {type(e).__name__}: {str(data)[:300]}"
            return result
        missing = [q for q in questions if q not in result.answers]
        if missing:
            result.error = f"답 없음: {missing}"
        return result

    async def aclose(self) -> None:
        await self.http.aclose()
