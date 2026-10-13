"""네트워크 없이 도는 기준선: 규칙 기준선과 다수 라벨 기준선."""

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from .types import DecisionResult

DecideFn = Callable[[dict[str, Any], dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]


class LocalClient:
    def __init__(self, name: str, fn: DecideFn, model: str):
        self.name = name
        self.fn = fn
        self.model = model

    async def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult:
        t0 = time.perf_counter()
        answers = self.fn(state, questions)
        return DecisionResult(self.name, self.model, answers, latency_ms=(time.perf_counter() - t0) * 1000)

    async def aclose(self) -> None:
        pass


def majority_fn(train_path: Path) -> DecideFn:
    """train의 질문 유형별 라벨 비율을 그대로 확률로 내는 기준선. applies는 모든 조항을 한 유형으로 본다."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for line in train_path.read_text(encoding="utf-8").splitlines():
        for qid, g in json.loads(json.loads(line)["gold"]).items():
            counts[qid.split(":")[0]][g["label"]] += 1

    def decide(state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out = {}
        for qid, q in questions.items():
            c = counts[qid.split(":")[0]]
            if q["type"] == "noul":
                out[qid] = {"type": "noul", "noul": (c["true"] + 1) / (c["true"] + c["false"] + 2)}
            else:
                opts = list(q["criteria"])
                total = sum(c[o] + 1 for o in opts)
                dist = {o: (c[o] + 1) / total for o in opts}
                out[qid] = {"type": "choice", "choice": max(dist, key=dist.get), "probabilities": dist}
        return out

    return decide
