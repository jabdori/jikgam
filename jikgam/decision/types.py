"""판별 모델 공통 인터페이스.

Jev, Clef, 학습한 공개 모델, 생성형 LLM 판단, 규칙 기준선이 모두 같은 입력(state, questions)을 받고
같은 형태의 결과를 낸다. 엔진과 평가 스크립트는 이 형태만 본다.

answers는 Jev 응답 형식을 따른다.
    noul:   {"type": "noul", "noul": 0.93}
    choice: {"type": "choice", "choice": "refund", "probabilities": {"refund": 0.9, ...}}
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DecisionResult:
    backend: str  # 설정에 적힌 이름 (예: "jev")
    model: str  # 응답이 알려 준 실제 모델 버전 (예: "jev-1.13.0")
    answers: dict[str, dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0  # 생성형 LLM은 사고 토큰을 포함한다
    latency_ms: float = 0.0  # 요청 전송부터 응답 본문 수신까지, 클라이언트에서 잰 값
    cost_usd: float = 0.0
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # 요청 ID 등 백엔드별 기록


class DecisionClient(Protocol):
    name: str

    async def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult: ...

    async def aclose(self) -> None: ...


def wire_id(qid: str) -> str:
    """요청에 넣는 질문 키. Workers AI는 키가 ^[A-Za-z0-9_.-]{1,100}$ 이어야 해서 applies:A-05의 ':'를 거부한다.
    모든 원격 백엔드가 같은 요청을 받도록 키 변환을 한곳에서 한다."""
    return qid.replace(":", "__")


def to_wire(questions: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """(요청용 질문, 요청 키 → 원래 질문 ID)."""
    wired = {wire_id(q): v for q, v in questions.items()}
    return wired, {wire_id(q): q for q in questions}


def label_and_prob(answer: dict[str, Any]) -> tuple[str, float, dict[str, float]]:
    """Jev 형식 답에서 (예측 라벨, 그 라벨의 확률, 전체 분포)를 꺼낸다. noul 임계값은 0.5."""
    if answer["type"] == "noul":
        p = float(answer["noul"])
        dist = {"true": p, "false": 1.0 - p}
        label = "true" if p >= 0.5 else "false"  # 확률이 정확히 0.5면 true(docs/evaluation.md §2.3 보완)
        return label, dist[label], dist
    dist = {k: float(v) for k, v in answer["probabilities"].items()}
    label = max(dist, key=lambda k: (dist[k], k == answer.get("choice")))
    return label, dist[label], dist
