"""검색 평가 세트(질문과 정답 근거)를 만든다(docs/evaluation-v2.md §2.3).

`gemini-3.1-pro-preview`가 문서 묶음 전체를 읽고, 고객 말투로 질문을 쓰고, 답의 근거를 원문에서 그대로 옮긴다.
코드가 옮긴 원문을 추출 결과에서 찾아 원문 위치(글자 범위)로 바꾼다. 공백과 표 구분자 "|"는 무시하고 비교한다.
근거를 찾지 못한 질문은 버리고 그 수를 묶음별로 센다.

- 근거 항목 하나는 원문의 이어진 한 부분이다. 같은 내용이 다른 곳에도 적혀 있으면 그 위치를 대체 근거로 따로 받는다.
  옮긴 원문이 문서에 여러 번 똑같이 나오면, 나온 곳 모두를 대체 근거로 둔다.
- 답이 있는 질문: 묶음마다 문서 전체를 넣고, 호출마다 다른 "중심 구역"을 줘서 질문이 문서 전체에 퍼지게 한다.
  목표 수보다 넉넉히(OVER배) 만든 뒤 묶음 × 유형별로 고정한 seed로 목표 수만큼 고른다.
- 묶음을 넘나드는 질문: 묶음이 다른 두 문서를 함께 넣어 만든다.
- 근거 없음 질문: 묶음마다 후보를 만들고, 세 묶음 각각의 글 전체로 "답할 수 있는가"를 같은 모델로 확인한다.
  세 묶음 모두 답할 수 없다고 한 후보만 남긴다.
- 한 번에 넣는 글은 20만 토큰 아래다(넘으면 단가가 달라진다).

비용: 같은 묶음의 호출은 앞부분(지시와 문서)이 같아 암묵 캐시가 붙는다. 그래서 묶음마다 첫 호출을 먼저 보내고
나머지를 보낸다. 예산은 응답의 캐시 토큰 수를 반영한 청구 추정으로 세고, 시험 호출까지 합쳐 BUDGET_USD를 넘지 않는다.

열람 규칙(§2.6): 만든 질문은 분할 배정 전이라 test가 될 수 있다. 그래서 이 스크립트는 수와 비용만 출력하고
내용은 파일로만 쓴다. --pilot만 예외다(시험 호출이고, 그 결과는 세트에 넣지 않는다).

결과:
- 법령·고시만 담은 항목: data/v2/qa/items.jsonl
- 표준약관이 들어간 항목: data/v2/local/qa/items.jsonl(저장소 밖, ADR 0023 보완)
- 호출 기록(응답 원문 포함): data/v2/local/qa/raw/, 시험 호출은 data/v2/local/qa/pilot/
- 수와 비용: data/v2/qa/generation.json

    uv run python -m jikgam.v2.qa_gen --pilot 3
    uv run python -m jikgam.v2.qa_gen
"""

import argparse
import asyncio
import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from jikgam.ingest.corpus import GROUPS, ROOT, Doc, load_docs, nonspace_len

SEED = 20261016
MODEL = "gemini-pro"  # config/decision_models.toml의 이름(gemini-3.1-pro-preview)
BUDGET_USD = 13.0  # 사용자 승인 범위(§9 데이터 만들기 7~13). 시험 호출을 합쳐 넘으면 새 호출을 보내지 않는다
CONCURRENCY = 4
RESERVE_USD = 0.8  # 보내는 중인 호출 하나의 비용 상한으로 미리 잡는 값(예산을 넘지 않게)
MAX_OUTPUT_TOKENS = 65536
OVER = 1.15  # 목표 수보다 넉넉히 만든다(버리는 질문이 있으므로)
MIN_QUOTE = 10  # 이보다 짧은 근거 원문(공백 제외)은 여러 곳에 맞을 수 있어 받지 않는다

QA_DIR = ROOT / "data" / "v2" / "qa"
LOCAL_DIR = ROOT / "data" / "v2" / "local" / "qa"

# 묶음 × 유형별 목표 수(§2.3). 답이 있는 360개는 법령 140, 고시 110, 표준약관 70, 넘나듦 40이다.
# 단일 220과 묶음 안 여러 근거 100은 묶음 크기에 비례해 나눴다(넘나듦 40은 모두 여러 근거).
TARGETS = {
    ("법령", "single"): 96, ("법령", "multi"): 44,
    ("고시", "single"): 76, ("고시", "multi"): 34,
    ("표준약관", "single"): 48, ("표준약관", "multi"): 22,
    ("넘나듦", "multi"): 40,
    # 근거 없음 40은 답이 있는 질문 수에 비례해 나눴다
    ("법령", "none"): 17, ("고시", "none"): 14, ("표준약관", "none"): 9,
}
CALLS = {"법령": 5, "고시": 5, "표준약관": 3}  # 묶음마다 답이 있는 질문 호출 수(호출마다 중심 구역이 다르다)
CROSS = [  # 묶음이 다른 두 문서. 호출마다 여러 근거 질문 CROSS_EACH개
    ("ecom-act", "dispute-std"), ("content-act", "content-guide"), ("ecom-act", "ecom-terms"),
    ("terms-act", "game-terms"), ("content-guide", "game-terms"),
]
CROSS_EACH = 10
NONE_CANDIDATES = {"법령": 30, "고시": 25, "표준약관": 16}

DOC_RULES = """주어진 문서는 PDF에서 뽑은 글이다. 각 줄 앞의 숫자는 줄 번호이고 탭으로 나뉜다. PDF 줄바꿈이 단어 중간에 있을 수 있다. 표는 눈에 보이는 줄마다 칸을 " | "로 나눠 적었다."""

QUOTE_RULES = """- 원문은 글자 그대로 옮긴다. 줄 번호는 빼고 줄을 이어 붙인다. 중간 줄을 건너뛰거나, 생략(…)하거나, 고쳐 쓰지 않는다.
- 표는 칸을 다시 모아 쓰지 않는다. 원문에 적힌 줄 순서대로 옮기고, 한 줄에 다른 칸의 글이 섞여 있어도 그대로 둔다."""

SYSTEM_ANSWERABLE = f"""너는 소비자 상담용 근거 검색을 평가할 질문과 정답 근거를 만든다.
{DOC_RULES}

질문:
- 소비자가 상담 채팅에 실제로 쓸 말투로 쓴다. 자기 상황을 말하며 묻는 것이 자연스럽다. 근거 문장의 표현을 그대로 옮겨 묻지 않는다.
- 주어진 문서만으로 답이 정해지는 질문만 쓴다.
- 질문끼리 같은 내용을 묻지 않는다. 중심 구역의 여러 부분에 고르게 퍼지게 쓴다.

질문 유형:
- single(단일 근거): 근거 항목 하나로 답이 정해진다. 그 근거는 중심 구역 안에 있다.
- multi(여러 근거): 근거 항목이 둘 이상이고, 모든 항목이 있어야 답이 정해진다. 항목끼리 겹치지 않고, 3줄 넘게 떨어져 있거나 다른 문서·별표에 있다. 항목 하나 이상은 중심 구역 안에 있고, 나머지는 주어진 문서 어디에 있어도 된다.

근거 항목(evidence):
- 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다. doc에 문서 id를, quote에 그 부분을 옮긴다.
{QUOTE_RULES}
- 필요한 내용이 떨어진 여러 줄에 걸쳐 있으면(예: 표의 분쟁 유형 줄과 해결 기준 줄), 그 첫 줄부터 끝 줄까지 사이 줄을 모두 넣어 한 quote로 옮긴다. 아주 멀리 떨어져 있으면 근거 항목을 나눈다.
- 답에 필요한 만큼만 옮긴다. 보통 한두 문장이나 표 몇 줄이다. 너무 짧아 여러 곳에 똑같이 나올 말(예: "1개월")만 옮기지 않는다.
- same_elsewhere는 같은 내용이 다른 곳에도 적혀 있을 때만(예: 법률과 시행령이 같은 기준을 적은 경우) 그 위치를 적는 칸이다. 한 내용의 조각을 나눠 적는 칸이 아니다. 없으면 빈 목록이다.

answer에는 근거로 정해지는 답을 한두 문장으로 쓴다."""

SYSTEM_NONE = f"""너는 소비자 상담용 근거 검색을 평가할 "근거 없음" 질문을 만든다.
{DOC_RULES}

- 주어진 문서와 같은 주제(전자상거래, 소비자 보호, 콘텐츠, 약관, 분쟁 해결)로 소비자가 상담 채팅에 실제로 쓸 말투의 질문을 쓴다.
- 그럴듯하지만 주어진 문서로는 답할 수 없어야 한다. 예를 들어 문서가 정하지 않은 수치나 절차, 특정 회사의 방침, 문서에 없는 다른 법령의 내용을 묻는다.
- 주제와 상관없는 질문(날씨, 요리 같은 것)은 쓰지 않는다. 문서 일부로 답이 되는 질문도 쓰지 않는다.
- 질문끼리 같은 내용을 묻지 않는다.
- reason에는 왜 이 문서들로 답할 수 없는지 한 문장으로 쓴다."""

SYSTEM_CHECK = f"""너는 질문이 주어진 문서로 답할 수 있는지 판단한다.
{DOC_RULES}

- 질문마다 주어진 문서에 답의 근거가 있는지 본다. 일부라도 답이 되는 근거가 있으면 answerable을 true로 하고, doc과 quote에 그 원문을 옮긴다.
{QUOTE_RULES}
- 근거가 전혀 없을 때만 answerable을 false로 하고, doc과 quote는 빈 글로 둔다."""

LOCATION = {"type": "object", "properties": {"doc": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["doc", "quote"]}
SCHEMA_ANSWERABLE = {"type": "object", "properties": {"questions": {"type": "array", "items": {
    "type": "object", "properties": {
        "question": {"type": "string"}, "type": {"type": "string", "enum": ["single", "multi"]},
        "answer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "doc": {"type": "string"}, "quote": {"type": "string"},
            "same_elsewhere": {"type": "array", "items": LOCATION}}, "required": ["doc", "quote", "same_elsewhere"]}}},
    "required": ["question", "type", "answer", "evidence"]}}}, "required": ["questions"]}
SCHEMA_NONE = {"type": "object", "properties": {"questions": {"type": "array", "items": {
    "type": "object", "properties": {"question": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["question", "reason"]}}}, "required": ["questions"]}
SCHEMA_CHECK = {"type": "object", "properties": {"results": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "answerable": {"type": "boolean"},
                                     "doc": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["id", "answerable", "doc", "quote"]}}}, "required": ["results"]}


@dataclass
class Call:
    id: str
    kind: str  # answerable | cross | none_gen | none_check
    group: str  # 묶음(넘나듦 포함). none_check는 확인에 쓰는 묶음
    docs: list[str]
    focus: list[tuple[str, int, int]] = field(default_factory=list)  # (문서, 첫 줄, 끝 줄)
    n_single: int = 0
    n_multi: int = 0
    n_none: int = 0
    seed: int = 0
    avoid: list[str] = field(default_factory=list)  # 보충 호출에서 다시 묻지 않을, 이미 쓴 질문


# ---------- 프롬프트 ----------

def docs_block(docs: dict[str, Doc], ids: list[str]) -> str:
    parts = []
    for did in ids:
        d = docs[did]
        body = "\n".join(f"{line.no}\t{line.text}" for line in d.lines)
        parts.append(f'<문서 id="{did}" 이름="{d.title}" 종류="{d.kind}">\n{body}\n</문서>')
    return "\n\n".join(parts)


def focus_text(focus: list[tuple[str, int, int]]) -> str:
    return ", ".join(f"{d} {a}~{b}줄" for d, a, b in focus)


def task_text(call: Call, candidates: list[dict] | None = None) -> str:
    if call.kind == "answerable":
        text = (f"## 이번에 쓸 것\n- 중심 구역: {focus_text(call.focus)}\n"
                f"- single 질문 {call.n_single}개, multi 질문 {call.n_multi}개")
        if call.avoid:
            text += "\n\n## 이미 쓴 질문(같은 내용을 다시 묻지 않는다)\n" + "\n".join(f"- {a}" for a in call.avoid)
        return text
    if call.kind == "cross":
        a, b = call.docs
        return (f"## 이번에 쓸 것\n- multi 질문 {call.n_multi}개. 두 문서({a}, {b})는 성격이 다른 문서다.\n"
                f"- 질문마다 근거 항목이 두 문서에 하나 이상씩 있어야 한다. 중심 구역은 두 문서 전체다.")
    if call.kind == "none_gen":
        return f"## 이번에 쓸 것\n- 근거 없음 질문 {call.n_none}개"
    lines = "\n".join(f'- {c["id"]}: {c["question"]}' for c in candidates or [])
    return f"## 판단할 질문\n{lines}\n\n질문마다 results에 하나씩, 주어진 id로 답한다."


def prompt_of(call: Call, docs: dict[str, Doc], candidates: list[dict] | None = None) -> tuple[str, str, dict]:
    system, schema = {"answerable": (SYSTEM_ANSWERABLE, SCHEMA_ANSWERABLE), "cross": (SYSTEM_ANSWERABLE, SCHEMA_ANSWERABLE),
                      "none_gen": (SYSTEM_NONE, SCHEMA_NONE), "none_check": (SYSTEM_CHECK, SCHEMA_CHECK)}[call.kind]
    # 문서를 앞에 두어 같은 묶음의 호출끼리 앞부분이 같게 한다(암묵 캐시)
    return system, docs_block(docs, call.docs) + "\n\n" + task_text(call, candidates), schema


# ---------- 호출 계획 ----------

def group_docs(docs: dict[str, Doc], group: str) -> list[str]:
    return [d.id for d in docs.values() if d.group == group]


def focus_regions(docs: dict[str, Doc], ids: list[str], n: int) -> list[list[tuple[str, int, int]]]:
    """묶음의 줄을 글자 수가 비슷한 n개의 연속 구역으로 나눈다."""
    lines = [(did, line.no, len(line.text) + 1) for did in ids for line in docs[did].lines]
    total = sum(w for _, _, w in lines)
    regions: list[list[tuple[str, int, int]]] = [[] for _ in range(n)]
    acc = 0
    for did, no, w in lines:
        k = min(n - 1, int(acc * n / total))
        acc += w
        reg = regions[k]
        if reg and reg[-1][0] == did and reg[-1][2] == no - 1:
            reg[-1] = (did, reg[-1][1], no)
        else:
            reg.append((did, no, no))
    return regions


def plan(docs: dict[str, Doc]) -> list[Call]:
    calls: list[Call] = []
    for group in GROUPS:
        ids = group_docs(docs, group)
        n = CALLS[group]
        per_single = math.ceil(TARGETS[(group, "single")] * OVER / n)
        per_multi = math.ceil(TARGETS[(group, "multi")] * OVER / n)
        for k, reg in enumerate(focus_regions(docs, ids, n), 1):
            calls.append(Call(f"{group}-{k}", "answerable", group, ids, reg, per_single, per_multi,
                              seed=SEED + len(calls)))
    for k, pair in enumerate(CROSS, 1):
        calls.append(Call(f"넘나듦-{k}", "cross", "넘나듦", list(pair), n_multi=CROSS_EACH, seed=SEED + len(calls)))
    for group in GROUPS:
        calls.append(Call(f"없음-{group}", "none_gen", group, group_docs(docs, group), n_none=NONE_CANDIDATES[group],
                          seed=SEED + len(calls)))
    return calls


# 첫 생성에서 모자란 칸을 채우는 보충 호출(2026-10-16). 지시는 같고, 그 묶음에서 이미 쓴 질문을 함께 준다.
# 버린 비율(고시 단일 약 25%, 고시 여러 근거 약 50%, 법령 여러 근거 약 30%)을 보고 수를 정했다.
EXTRA = [("고시", 2, 6, 13), ("법령", 1, 0, 10)]  # (묶음, 호출 수, 호출마다 single, multi)


def extra_calls(docs: dict[str, Doc]) -> list[Call]:
    calls = []
    for group, n, n_single, n_multi in EXTRA:
        ids = group_docs(docs, group)
        written = []
        for path in sorted((LOCAL_DIR / "raw").glob(f"{group}-*.json")):
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec["kind"] == "answerable" and "추가" not in rec["call"]:
                written += [q.get("question", "") for q in (rec["data"] or {}).get("questions") or []]
        for k, reg in enumerate(focus_regions(docs, ids, n), 1):
            calls.append(Call(f"{group}-추가-{k}", "answerable", group, ids, reg, n_single, n_multi,
                              seed=SEED + 200 + len(calls), avoid=written))
    return calls


# ---------- 검증 ----------

def locate(docs: dict[str, Doc], did: str, quote: str) -> tuple[list[dict], str | None]:
    """옮긴 원문 하나를 원문 위치들로. 실패하면 (빈 목록, 이유)."""
    if did not in docs:
        return [], "unknown_doc"
    if nonspace_len(quote) < MIN_QUOTE:
        return [], "quote_too_short"
    spans = docs[did].find_quote(quote)
    if not spans:
        return [], "quote_not_found"
    return [{"doc": did, "start": a, "end": b} for a, b in spans], None


def overlaps(a: dict, b: dict) -> bool:
    return a["doc"] == b["doc"] and a["start"] < b["end"] and b["start"] < a["end"]


def far_apart(docs: dict[str, Doc], a: dict, b: dict) -> bool:
    """여러 근거의 두 항목이 겹치지 않고, 3줄 넘게 떨어져 있거나 다른 파일에 있는가(§2.3)."""
    if a["doc"] != b["doc"]:
        return True
    if overlaps(a, b):
        return False
    d = docs[a["doc"]]
    first, second = sorted((a, b), key=lambda s: s["start"])
    end_line, start_line = d.line_at(first["end"] - 1), d.line_at(second["start"])
    return end_line.file != start_line.file or start_line.no - end_line.no > 3


def in_focus(docs: dict[str, Doc], span: dict, focus: list[tuple[str, int, int]]) -> bool:
    line = docs[span["doc"]].line_at(span["start"]).no
    return any(span["doc"] == d and a <= line <= b for d, a, b in focus)


def bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def validate(docs: dict[str, Doc], call: Call, q: dict, counts: dict[str, int]) -> dict | None:
    """모델이 쓴 질문 하나를 확인해 항목으로 바꾼다. 버리면 None이고 이유를 counts에 센다."""
    evidence = []
    for item in q.get("evidence") or []:
        alts, why = locate(docs, item.get("doc", ""), item.get("quote", ""))
        if why:
            bump(counts, f"drop_evidence_{why}")
            return None
        for loc in item.get("same_elsewhere") or []:
            more, why = locate(docs, loc.get("doc", ""), loc.get("quote", ""))
            if why:
                bump(counts, f"elsewhere_{why}")  # 대체 근거는 찾지 못하면 빼고 질문은 남긴다
            alts += [s for s in more if not any(overlaps(s, t) for t in alts)]
        evidence.append({"alternatives": alts})
    qtype = q.get("type")
    if not evidence:
        bump(counts, "drop_no_evidence")
        return None
    if qtype == "single" and len(evidence) != 1:
        bump(counts, "drop_single_with_many")
        return None
    if qtype == "multi":
        if len(evidence) < 2:
            bump(counts, "drop_multi_with_one")
            return None
        primaries = [e["alternatives"][0] for e in evidence]
        if not all(far_apart(docs, a, b) for i, a in enumerate(primaries) for b in primaries[i + 1:]):
            bump(counts, "drop_multi_too_close")
            return None
        if call.kind == "cross" and len({docs[p["doc"]].group for p in primaries}) < 2:
            bump(counts, "drop_cross_single_group")
            return None
    if call.focus and not any(in_focus(docs, e["alternatives"][0], call.focus) for e in evidence):
        bump(counts, "outside_focus_kept")
    return {"group": call.group, "type": qtype, "question": q.get("question", "").strip(),
            "answer": q.get("answer", "").strip(), "evidence": evidence, "call": call.id}


def is_local(docs: dict[str, Doc], item: dict) -> bool:
    """표준약관(다시 배포하지 않는 문서)이 들어간 항목은 저장소 밖에 둔다."""
    if item["group"] == "표준약관":
        return True
    return any(not docs[a["doc"]].redistribute for e in item["evidence"] for a in e["alternatives"])


# ---------- 실행 ----------

def billed(usage: dict, price: dict) -> float:
    """응답의 토큰 수로 낸 청구 추정. 캐시에서 온 입력은 캐시 단가로 센다."""
    cached = usage.get("cached_tokens", 0)
    return ((usage["input_tokens"] - cached) * price["in"] + cached * price["cached"]
            + usage["output_tokens"] * price["out"]) / 1e6


def price_of() -> dict:
    from jikgam.decision.registry import load_config

    cfg = load_config()[MODEL]
    return {"in": cfg["price_input_per_mtok"], "out": cfg["price_output_per_mtok"],
            "cached": cfg["price_cached_input_per_mtok"]}


def prior_spent(price: dict) -> float:
    """이미 보낸 호출(시험 호출, 앞선 본 생성)의 청구 추정 합."""
    total = 0.0
    for sub in ("pilot", "raw"):
        for path in (LOCAL_DIR / sub).glob("*.json"):
            total += billed(json.loads(path.read_text(encoding="utf-8"))["usage"], price)
    return total


class Runner:
    def __init__(self, raw_dir: Path):
        from jikgam.sim.gemini import GeminiJson

        self.client = GeminiJson.from_config(MODEL, timeout_s=900.0)
        self.raw_dir = raw_dir
        self.price = price_of()
        self.spent = prior_spent(self.price)  # 이미 보낸 호출을 포함한다
        self.start_spent = self.spent
        self.reserved = 0.0
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.log: list[dict] = []

    def cached(self, call: Call) -> dict | None:
        path = self.raw_dir / f"{call.id}.json"
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec.get("data") is not None:
                return rec
        return None

    async def ask(self, call: Call, docs: dict[str, Doc], candidates: list[dict] | None = None) -> dict:
        rec = self.cached(call)
        if rec is not None:
            self.log.append({"call": rec["call"], "usage": rec["usage"], "prompt_sha256": rec["prompt_sha256"],
                             "billed_usd": round(billed(rec["usage"], self.price), 4), "reused": True})
            return rec
        system, user, schema = prompt_of(call, docs, candidates)
        async with self.sem:
            if self.spent + self.reserved + RESERVE_USD > BUDGET_USD:
                raise RuntimeError(f"예산 ${BUDGET_USD}에 닿아 {call.id}를 보내지 않았다(지금까지 ${self.spent:.2f})")
            self.reserved += RESERVE_USD
            try:
                reply = await self.client.ask(system, user, schema, seed=call.seed, max_output_tokens=MAX_OUTPUT_TOKENS)
            finally:
                self.reserved -= RESERVE_USD
        usage = reply.usage()
        self.spent += billed(usage, self.price)
        rec = {"call": call.id, "kind": call.kind, "group": call.group, "docs": call.docs, "focus": call.focus,
               "seed": call.seed, "task": task_text(call, candidates),
               "prompt_sha256": hashlib.sha256((system + "\n\n" + user).encode("utf-8")).hexdigest(),
               "usage": usage, "data": reply.data}
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / f"{call.id}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        self.log.append({"call": call.id, "usage": usage, "prompt_sha256": rec["prompt_sha256"],
                         "billed_usd": round(billed(usage, self.price), 4), "reused": False})
        return rec

    async def ask_all(self, calls: list[Call], docs: dict[str, Doc], candidates: list[dict] | None = None) -> list[dict]:
        """앞부분(지시 종류와 문서)이 같은 호출끼리 첫 호출을 먼저 보내 캐시가 생기게 한 뒤 나머지를 보낸다."""
        first: dict[tuple, Call] = {}
        for c in calls:
            first.setdefault((c.kind in ("answerable", "cross"), c.kind, tuple(c.docs)), c)
        heads = list(first.values())
        head_recs = await asyncio.gather(*(self.ask(c, docs, candidates) for c in heads))
        done = {c.id: r for c, r in zip(heads, head_recs)}
        rest = [c for c in calls if c.id not in done]
        for c, r in zip(rest, await asyncio.gather(*(self.ask(c, docs, candidates) for c in rest))):
            done[c.id] = r
        return [done[c.id] for c in calls]

    async def aclose(self) -> None:
        await self.client.aclose()


def sample(items: list[dict], group: str, qtype: str) -> list[dict]:
    pool = [i for i in items if i["group"] == group and i["type"] == qtype]
    random.Random(f"{SEED}:{group}:{qtype}").shuffle(pool)
    return pool[:TARGETS[(group, qtype)]]


async def generate(docs: dict[str, Doc], extra: bool) -> dict:
    runner = Runner(LOCAL_DIR / "raw")
    calls = plan(docs) + (extra_calls(docs) if extra else [])
    counts: dict[str, dict[str, int]] = {}
    try:
        gen = [c for c in calls if c.kind in ("answerable", "cross")]
        recs = await runner.ask_all(gen, docs)
        candidates: list[dict] = []
        for call, rec in zip(gen, recs):
            c = counts.setdefault(call.group, {})
            qs = (rec["data"] or {}).get("questions") or []
            c["written"] = c.get("written", 0) + len(qs)
            for q in qs:
                item = validate(docs, call, q, c)
                if item:
                    candidates.append(item)
                    bump(c, f"kept_{item['type']}")

        none_calls = [c for c in calls if c.kind == "none_gen"]
        none_recs = await runner.ask_all(none_calls, docs)
        none_cands = []
        for call, rec in zip(none_calls, none_recs):
            qs = (rec["data"] or {}).get("questions") or []
            counts.setdefault(call.group, {})["none_written"] = len(qs)
            for k, q in enumerate(qs, 1):
                none_cands.append({"cid": f"n-{call.group}-{k:02d}", "group": call.group, "type": "none",
                                   "question": q.get("question", "").strip(), "note": q.get("reason", "").strip(),
                                   "answer": None, "evidence": [], "call": call.id})
        listed = [{"id": c["cid"], "question": c["question"]} for c in none_cands]
        checks = [Call(f"확인-{g}", "none_check", g, group_docs(docs, g), seed=SEED + 100 + i)
                  for i, g in enumerate(GROUPS)]
        check_recs = await runner.ask_all(checks, docs, listed)
        answerable_somewhere: set[str] = set()
        for call, rec in zip(checks, check_recs):
            seen = set()
            for r in (rec["data"] or {}).get("results") or []:
                seen.add(r.get("id"))
                if r.get("answerable"):
                    answerable_somewhere.add(r.get("id"))
            missing = {c["cid"] for c in none_cands} - seen
            answerable_somewhere |= missing  # 판단이 빠진 후보는 보수적으로 버린다
            counts.setdefault(call.group, {})["check_missing"] = len(missing)
        none_kept = []
        for cand in none_cands:
            if cand["cid"] in answerable_somewhere:
                bump(counts[cand["group"]], "none_dropped_answerable")
            else:
                bump(counts[cand["group"]], "kept_none")
                none_kept.append(cand)
    finally:
        await runner.aclose()

    chosen = []
    for (group, qtype), target in TARGETS.items():
        picked = sample(none_kept if qtype == "none" else candidates, group, qtype)
        counts.setdefault(group, {})[f"chosen_{qtype}"] = len(picked)
        counts[group][f"short_{qtype}"] = max(0, target - len(picked))
        chosen += picked
    random.Random(f"{SEED}:ids").shuffle(chosen)
    for k, item in enumerate(chosen, 1):
        item.pop("cid", None)
        item["id"] = f"qa-{k:04d}"
    return {"items": chosen, "counts": counts, "log": runner.log, "spent": runner.spent,
            "spent_this_run": runner.spent - runner.start_spent}


def write(docs: dict[str, Doc], result: dict) -> None:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    public = [i for i in result["items"] if not is_local(docs, i)]
    local = [i for i in result["items"] if is_local(docs, i)]
    for path, rows in ((QA_DIR / "items.jsonl", public), (LOCAL_DIR / "items.jsonl", local)):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    price = price_of()
    pilots = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((LOCAL_DIR / "pilot").glob("*.json"))]
    meta = {
        "model": MODEL, "seed": SEED, "targets": {f"{g}/{t}": n for (g, t), n in TARGETS.items()},
        "over": OVER, "calls_per_group": CALLS, "extra_calls": EXTRA, "cross_pairs": CROSS, "cross_each": CROSS_EACH,
        "none_candidates": NONE_CANDIDATES, "min_quote_nonspace": MIN_QUOTE, "price_usd_per_mtok": price,
        "prompts_sha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest()
                           for k, v in (("answerable", SYSTEM_ANSWERABLE), ("none", SYSTEM_NONE), ("check", SYSTEM_CHECK))},
        "counts": result["counts"], "files": {"data/v2/qa/items.jsonl": len(public),
                                              "data/v2/local/qa/items.jsonl": len(local)},
        "calls": result["log"],
        "pilot_calls": [{"call": p["call"], "usage": p["usage"], "billed_usd": round(billed(p["usage"], price), 4)}
                        for p in pilots],
        "billed_usd": {"generation": round(sum(e["billed_usd"] for e in result["log"]), 4),
                       "pilots": round(sum(billed(p["usage"], price) for p in pilots), 4)},
        "uncached_cost_usd": round(sum(e["usage"]["cost_usd"] for e in result["log"]) +
                                   sum(p["usage"]["cost_usd"] for p in pilots), 4),
    }
    meta["billed_usd"]["total"] = round(meta["billed_usd"]["generation"] + meta["billed_usd"]["pilots"], 4)
    (QA_DIR / "generation.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                            newline="\n")


PILOTS = {  # 시험 호출. 결과를 보여 주고 버린다
    "1": [("법령", [("ecom-rule", 1, 151)]), ("고시", [("dispute-std", 1000, 1300)])],
    "2": [("고시", [("dispute-std", 1000, 1300)])],  # 표 인용 지시를 고친 뒤 같은 구역으로 다시
    "3": [("고시", [("dispute-std", 1000, 1300)])],  # 근거 형식(이어진 한 부분 + 다른 곳의 같은 내용)을 바꾼 뒤
}


async def pilot(docs: dict[str, Doc], tag: str) -> None:
    """시험 호출. 결과를 보여 주고 버린다(세트에 넣지 않는다)."""
    runner = Runner(LOCAL_DIR / "pilot")
    calls = [Call(f"시험{tag}-{g}", "answerable", g, group_docs(docs, g), focus, 3, 2, seed=SEED - k)
             for k, (g, focus) in enumerate(PILOTS[tag], 1)]
    try:
        recs = await asyncio.gather(*(runner.ask(c, docs) for c in calls))
    finally:
        await runner.aclose()
    for call, rec in zip(calls, recs):
        counts: dict[str, int] = {}
        print(f"== {call.id} {json.dumps(rec['usage'], ensure_ascii=False)}")
        for q in (rec["data"] or {}).get("questions") or []:
            item = validate(docs, call, q, counts)
            print(json.dumps(q, ensure_ascii=False))
            if item:
                for e in item["evidence"]:
                    print("   ->", [(a["doc"], docs[a["doc"]].line_at(a["start"]).no, docs[a["doc"]].line_at(a["end"] - 1).no)
                                    for a in e["alternatives"]])
        print("counts", counts)
    print(f"이번 시험 ${runner.spent - runner.start_spent:.3f}, 시험 합계 ${runner.spent:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", choices=list(PILOTS))
    ap.add_argument("--extra", action="store_true", help="모자란 칸을 채우는 보충 호출을 더한다")
    args = ap.parse_args()
    docs = load_docs()
    if args.pilot:
        asyncio.run(pilot(docs, args.pilot))
        return
    result = asyncio.run(generate(docs, args.extra))
    write(docs, result)
    print(json.dumps({"counts": result["counts"], "billed_usd_this_run": round(result["spent_this_run"], 3),
                      "billed_usd_total_with_pilots": round(result["spent"], 3), "items": len(result["items"])},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
