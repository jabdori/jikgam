"""엔진 테스트용 가짜 판별기·생성기·업무 API. 테스트에서만 쓴다."""

import asyncio
from typing import Any

from jikgam.decision.types import DecisionResult
from jikgam.engine.generate import GenerationResult


def choice(criteria: dict[str, str], label: str, p: float = 0.9) -> dict[str, Any]:
    rest = (1 - p) / max(len(criteria) - 1, 1)
    return {"type": "choice", "choice": label, "probabilities": {k: (p if k == label else rest) for k in criteria}}


class ScriptedDecider:
    """마지막 고객 발화의 낱말로 답하는 판별기. 받은 state와 질문을 기록한다.

    intents: (낱말, intent) 목록. 맞는 낱말이 없으면 known_facts.request(직전 판단)를 유지한다.
    changes: 이 낱말이 있으면 condition_changed=참, suggestion_valid=거짓.
    agree: 이 낱말이 있으면 customer_agreed=참.
    """

    name = "fake"

    def __init__(self, intents=(), changes=(), agree=("네",), action="answer", next_info="none"):
        self.intents = list(intents)
        self.changes = tuple(changes)
        self.agree = tuple(agree)
        self.action = action
        self.next_info = next_info
        self.calls: list[tuple[dict, list[str]]] = []

    async def decide(self, state: dict, questions: dict) -> DecisionResult:
        self.calls.append((state, list(questions)))
        last = [u["text"] for u in state["conversation"] if u["speaker"] == "customer"][-1]
        changed = any(k in last for k in self.changes)
        answers: dict[str, Any] = {}
        for q, d in questions.items():
            if q == "intent":
                intent = next((i for k, i in self.intents if k in last), None) or \
                    state["known_facts"].get("request") or "unclear"
                answers[q] = choice(d["criteria"], intent)
            elif q == "condition_changed":
                answers[q] = {"type": "noul", "noul": 0.9 if changed else 0.1}
            elif q == "suggestion_valid":
                answers[q] = {"type": "noul", "noul": 0.1 if changed else 0.9}
            elif q == "customer_agreed":
                answers[q] = {"type": "noul", "noul": 0.95 if any(k in last for k in self.agree) else 0.05}
            elif q == "next_info":
                answers[q] = choice(d["criteria"], self.next_info)
            elif q == "action":
                answers[q] = choice(d["criteria"], self.action)
            else:  # applies:*, sufficient
                answers[q] = {"type": "noul", "noul": 0.8}
        return DecisionResult("fake", "fake-1", answers, latency_ms=1.0)

    async def aclose(self) -> None:
        pass


class GatedDecider(ScriptedDecider):
    """첫 호출을 release 전까지 붙잡는다. ignore_cancel이면 취소를 무시하고 끝까지 답한다(취소되지 않는 백엔드 흉내)."""

    def __init__(self, ignore_cancel: bool, **kw):
        super().__init__(**kw)
        self.ignore_cancel = ignore_cancel
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._first = True

    async def decide(self, state: dict, questions: dict) -> DecisionResult:
        if self._first:
            self._first = False
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
        return await super().decide(state, questions)


class EchoGenerator:
    """정책이 정한 규칙과 행동을 그대로 문장으로 낸다. 제안이 있으면 견적 금액을 amount로 낸다."""

    name = "fake-gen"
    model = "fake-gen"

    def __init__(self):
        self.payloads: list[dict] = []

    async def generate(self, payload: dict, feedback: str | None = None) -> GenerationResult:
        self.payloads.append(payload)
        plan = payload["plan"]
        refund = ((plan.get("proposal") or {}).get("refund") or {})
        amount = refund.get("refund_amount")
        return GenerationResult(f"[{plan['rule']}:{plan['action']}:{plan.get('ask')}]",
                                [a["id"] for a in payload["articles"]], amount, amount is not None, "fake-gen",
                                latency_ms=1.0)

    async def aclose(self) -> None:
        pass


class NoBizApi:
    """호출되면 안 되는 업무 API."""

    async def call(self, tool, args, target=None):
        raise AssertionError(f"업무 API를 부르면 안 된다: {tool}")

    async def aclose(self) -> None:
        pass


def utt(uid: str, text: str, **kw) -> dict:
    return {"type": "utterance", "utt_id": uid, "speaker": "customer", "text": text, "final": True, **kw}
