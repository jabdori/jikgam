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
- 대체 근거 찾기(2차부터): 고른 질문마다, 세 묶음 각각의 글 전체에서 정답 근거 항목과 같은 내용을 적은 곳을 같은 모델이
  찾는다. 찾은 원문은 같은 방식으로 위치를 확인해 그 항목의 대체 근거에 더한다. 묶음마다 따로 만들면 다른 묶음 문서의
  같은 내용(예: 법률과 표준약관이 같은 환급 기한을 적은 곳)을 놓치기 때문이다.

2차(2026-10-16): 첫 세트의 사람 검토에서 문제 비율이 25%로 기준(10%)을 넘었다. 검토 메모를 보고 지시를 고쳤다
(하나의 상담 상황, 적용 조건과 단서까지 담은 근거, 조건과 예외를 밝힌 답, 일부라도 답이 되면 근거 없음이 아님).
첫 세트의 기록은 data/v2/local/qa/round1/에 있다.

비용: 같은 묶음의 호출은 앞부분(지시와 문서)이 같아 암묵 캐시가 붙는다. 그래서 묶음마다 첫 호출을 먼저 보내고
나머지를 보낸다. 예산은 응답의 캐시 토큰 수를 반영한 청구 추정으로 세고, 시험 호출까지 합쳐 BUDGET_USD를 넘지 않는다.

열람 규칙(§2.6): 만든 질문은 분할 배정 전이라 test가 될 수 있다. 그래서 이 스크립트는 수와 비용만 출력하고
내용은 파일로만 쓴다. --pilot만 예외다(시험 호출이고, 그 결과는 세트에 넣지 않는다).

결과:
- 법령·고시만 담은 항목: data/v2/qa/items.jsonl
- 표준약관이 들어간 항목: data/v2/local/qa/items.jsonl(저장소 밖, ADR 0023 보완)
- 호출 기록(응답 원문 포함): data/v2/local/qa/raw/, 시험 호출은 data/v2/local/qa/pilot/
- 수와 비용: data/v2/qa/generation.json

    uv run python -m jikgam.v2.qa_gen --pilot 4
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
# 사용자 승인 범위. 1차는 $13(§9 데이터 만들기 7~13)에 $10.41을 썼다. 2차는 사용자가 추가 약 $14~16을 승인했고
# 상한을 $17로 둔다. 지금 폴더(raw/, pilot/)의 호출만 센다(1차 기록은 round1/로 옮겼다).
ROUND = 2
BUDGET_USD = 17.0
CONCURRENCY = 4
RESERVE_USD = 0.8  # 보내는 중인 호출 하나의 비용 상한으로 미리 잡는 값(예산을 넘지 않게)
MAX_OUTPUT_TOKENS = 65536
OVER = 1.15  # 목표 수보다 넉넉히 만든다(버리는 질문이 있으므로)
# 1차에서 버린 비율이 높았던 칸은 더 넉넉히 만든다(고시 단일 약 25%, 고시 여러 근거 약 50%, 법령 여러 근거 약 30%)
OVER_BY = {("고시", "single"): 1.35, ("고시", "multi"): 1.8, ("법령", "multi"): 1.45}
ALT_BATCH_CHARS = 70000  # 대체 근거 찾기에서 한 번에 넣는 질문 목록의 글자 수 상한
GAP_LINES = 25  # 중간 줄을 건너뛴 원문은 첫 조각이 든 줄부터 이 줄 수 안에서만 받는다
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

QUOTE_RULES = """- 원문은 글자 그대로 옮긴다. 줄 번호는 빼고 줄을 이어 붙인다. 줄 안의 글을 줄이거나 고쳐 쓰지 않는다.
- 표는 원문에 적힌 줄 순서대로 옮긴다. 한 줄에 다른 칸의 글이 섞여 있어도 그대로 둔다.
- 필요한 줄 사이에 상관없는 줄이 끼어 있으면 그 줄들을 건너뛰고, 건너뛴 자리에 "…"를 넣는다. 건너뛴 줄도 근거 범위에 들어가므로 25줄 안에서만 건너뛴다."""

SYSTEM_ANSWERABLE = f"""너는 소비자 상담용 근거 검색을 평가할 질문과 정답 근거를 만든다.
{DOC_RULES}

질문:
- 소비자가 상담 채팅에 실제로 쓸 말투로 쓴다. 자기 상황을 말하며 묻는 것이 자연스럽다. 근거 문장의 표현을 그대로 옮겨 묻지 않는다.
- 하나의 상담 상황에서 나오는 질문 하나를 쓴다. 별개의 두 문의를 이어 붙이지 않는다.
- 주어진 문서만으로 답이 정해지는 질문만 쓴다. 문서만으로 단정할 수 없는 사실 판단이 필요한 질문(예: 한 번 써 본 것이 "가치가 현저히 감소한" 경우인지)은 쓰지 않는다.
- 질문이 여러 가지를 묻거나 목록 전체를 물으면, 답과 근거가 그 전부를 담아야 한다.
- 질문끼리 같은 내용을 묻지 않는다. 중심 구역의 여러 부분에 고르게 퍼지게 쓴다.

질문 유형:
- single(단일 근거): 근거 항목 하나로 답이 정해진다. 그 근거는 중심 구역 안에 있다.
- multi(여러 근거): 물음 하나에 답하는 데 근거 항목 둘 이상이 함께 필요하다. 항목끼리 겹치지 않고, 3줄 넘게 떨어져 있거나 다른 문서·별표에 있다. 항목 하나 이상은 중심 구역 안에 있고, 나머지는 주어진 문서 어디에 있어도 된다.
  - 좋은 예: 법률이 "대통령령으로 정하는 이율"이라고만 정하고 시행령이 그 이율을 정한 경우의 지연이자 질문, 분쟁해결기준 별표의 보상 기준과 다른 별표의 품목별 품질보증기간이 함께 있어야 답이 나오는 질문.
  - 나쁜 예: "환불은 얼마나 되나요? 그리고 다치면 보상되나요?"처럼 각각 따로 답할 수 있는 두 물음을 "그리고", "또", "혹시"로 이은 것. 이런 질문은 쓰지 않는다.

근거 항목(evidence):
- 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다. doc에 문서 id를, quote에 그 부분을 옮긴다.
{QUOTE_RULES}
- 그 내용이 적용되는 조건을 함께 담는다. 표에서는 그 줄이 속한 상위 줄(분쟁 유형, "품질보증기간 이내" 같은 조건, 품목 이름)부터 옮긴다. 상위 줄이 25줄 넘게 떨어져 있으면 별도 근거 항목으로 둔다.
- 문장 중간에서 끊지 않는다. 답에 영향을 주는 단서("다만, …")와 예외도 넣는다.
- 답에 필요한 만큼만 옮긴다. 너무 짧아 여러 곳에 똑같이 나올 말(예: "1개월")만 옮기지 않는다.
- same_elsewhere는 같은 내용이 주어진 문서의 다른 곳에도 적혀 있을 때만(예: 법률과 시행령이 같은 기준을 적은 경우) 그 위치를 적는 칸이다. 한 내용의 조각을 나눠 적는 칸이 아니다. 없으면 빈 목록이다. 다른 묶음의 문서는 다음 단계에서 따로 찾는다.

answer:
- 근거로 정해지는 답을 쓴다. 답이 조건이나 예외에 따라 달라지면 그 조건과 예외를 밝힌다.
- 근거에 없는 경우까지 일반화하지 않는다."""

SYSTEM_NONE = f"""너는 소비자 상담용 근거 검색을 평가할 "근거 없음" 질문을 만든다.
{DOC_RULES}

- 주어진 문서와 같은 주제(전자상거래, 소비자 보호, 콘텐츠, 약관, 분쟁 해결)로 소비자가 상담 채팅에 실제로 쓸 말투의 질문을 쓴다.
- 그럴듯하지만 주어진 문서로는 답할 수 없어야 한다. 예를 들어 문서가 정하지 않은 수치나 절차, 특정 회사의 방침, 문서에 없는 다른 법령의 내용을 묻는다.
- 주제와 상관없는 질문(날씨, 요리 같은 것)은 쓰지 않는다.
- 질문의 일부(일반 원칙, 기간, 기준, 품목별 보증기간 같은 것)라도 문서로 답할 수 있으면 근거 없음 질문이 아니다. 그런 질문은 쓰지 않는다.
- 질문끼리 같은 내용을 묻지 않는다.
- reason에는 왜 이 문서들로 답할 수 없는지 한 문장으로 쓴다."""

SYSTEM_CHECK = f"""너는 질문이 주어진 문서로 답할 수 있는지 판단한다.
{DOC_RULES}

- 질문마다 주어진 문서에 답의 근거가 있는지 본다. 질문의 일부(일반 원칙, 기간, 기준, 품목별 보증기간 같은 것)라도 답이 되는 근거가 있으면 answerable을 true로 하고, doc과 quote에 그 원문을 옮긴다.
{QUOTE_RULES}
- 근거가 전혀 없을 때만 answerable을 false로 하고, doc과 quote는 빈 글로 둔다."""

SYSTEM_ALT = f"""너는 검색 평가 세트의 정답 근거에 빠진 대체 근거를 찾는다.
{DOC_RULES}

- 질문마다 질문, 답, 정답 근거 항목(근거 id와 원문)이 주어진다. 근거 항목들은 다른 문서에서 왔을 수 있다.
- 근거 항목마다, 주어진 문서에서 그 항목과 같은 내용을 적어 그 항목 대신 답의 근거가 될 수 있는 곳을 모두 찾는다. 예: 법률과 표준약관이 같은 환급 기한을 적은 곳, 시행령이나 고시가 법률의 기준을 다시 적은 곳.
- 주어진 근거 원문의 위치 자체는 적지 않는다. 관련은 있지만 그 항목의 내용을 담지 않은 곳(다른 조건, 일부만 같은 내용)도 적지 않는다.
- 찾은 곳은 doc과 quote로 적는다. 그 내용이 적용되는 조건까지 담아 옮긴다.
{QUOTE_RULES}
- 없으면 locations를 빈 목록으로 둔다. 근거 id마다 results에 하나씩 답한다."""

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
SCHEMA_ALT = {"type": "object", "properties": {"results": {"type": "array", "items": {
    "type": "object", "properties": {"evidence_id": {"type": "string"},
                                     "locations": {"type": "array", "items": LOCATION}},
    "required": ["evidence_id", "locations"]}}}, "required": ["results"]}


@dataclass
class Call:
    id: str
    kind: str  # answerable | cross | none_gen | none_check | alt
    group: str  # 묶음(넘나듦 포함). none_check와 alt는 찾아보는 묶음
    docs: list[str]
    focus: list[tuple[str, int, int]] = field(default_factory=list)  # (문서, 첫 줄, 끝 줄)
    n_single: int = 0
    n_multi: int = 0
    n_none: int = 0
    seed: int = 0
    avoid: list[str] = field(default_factory=list)  # 보충 호출에서 다시 묻지 않을, 이미 쓴 질문
    listing: str = ""  # 대체 근거 찾기에 넣는 질문과 근거 목록


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
    if call.kind == "alt":
        return f"## 질문과 정답 근거\n{call.listing}\n\n근거 id마다 results에 하나씩 답한다."
    lines = "\n".join(f'- {c["id"]}: {c["question"]}' for c in candidates or [])
    return f"## 판단할 질문\n{lines}\n\n질문마다 results에 하나씩, 주어진 id로 답한다."


def prompt_of(call: Call, docs: dict[str, Doc], candidates: list[dict] | None = None) -> tuple[str, str, dict]:
    system, schema = {"answerable": (SYSTEM_ANSWERABLE, SCHEMA_ANSWERABLE), "cross": (SYSTEM_ANSWERABLE, SCHEMA_ANSWERABLE),
                      "none_gen": (SYSTEM_NONE, SCHEMA_NONE), "none_check": (SYSTEM_CHECK, SCHEMA_CHECK),
                      "alt": (SYSTEM_ALT, SCHEMA_ALT)}[call.kind]
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


def call_seed(k: int) -> int:
    return SEED + 1000 * ROUND + k


def plan(docs: dict[str, Doc]) -> list[Call]:
    calls: list[Call] = []
    for group in GROUPS:
        ids = group_docs(docs, group)
        n = CALLS[group]
        per_single = math.ceil(TARGETS[(group, "single")] * OVER_BY.get((group, "single"), OVER) / n)
        per_multi = math.ceil(TARGETS[(group, "multi")] * OVER_BY.get((group, "multi"), OVER) / n)
        for k, reg in enumerate(focus_regions(docs, ids, n), 1):
            calls.append(Call(f"{group}-{k}", "answerable", group, ids, reg, per_single, per_multi,
                              seed=call_seed(len(calls))))
    for k, pair in enumerate(CROSS, 1):
        calls.append(Call(f"넘나듦-{k}", "cross", "넘나듦", list(pair), n_multi=CROSS_EACH, seed=call_seed(len(calls))))
    for group in GROUPS:
        calls.append(Call(f"없음-{group}", "none_gen", group, group_docs(docs, group), n_none=NONE_CANDIDATES[group],
                          seed=call_seed(len(calls))))
    return calls


# 모자란 칸을 채우는 보충 호출. 지시는 같고, 그 묶음에서 이미 쓴 질문을 함께 준다. (묶음, 호출 수, 호출마다 single, multi)
# 1차에는 [("고시", 2, 6, 13), ("법령", 1, 0, 10)]을 썼다. 2차는 OVER_BY로 처음부터 넉넉히 만들고, 모자라면 여기에 더한다.
EXTRA: list[tuple[str, int, int, int]] = []


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
                              seed=call_seed(200 + len(calls)), avoid=written))
    return calls


# ---------- 검증 ----------

def locate(docs: dict[str, Doc], did: str, quote: str, counts: dict[str, int] | None = None) -> tuple[list[dict], str | None]:
    """옮긴 원문 하나를 원문 위치들로. 실패하면 (빈 목록, 이유).

    그대로 이어진 원문이면 나오는 곳 모두를 돌려준다. 아니면 중간 줄을 건너뛴 원문으로 찾는다(§2.3 보완, 2차부터).
    """
    if did not in docs:
        return [], "unknown_doc"
    if nonspace_len(quote) < MIN_QUOTE:
        return [], "quote_too_short"
    spans = docs[did].find_quote(quote)
    if spans:
        return [{"doc": did, "start": a, "end": b} for a, b in spans], None
    if ROUND < 2:
        return [], "quote_not_found"
    gapped = docs[did].find_gapped(quote, max_lines=GAP_LINES)
    if gapped:
        if counts is not None:
            bump(counts, "quote_gapped")
        return [{"doc": did, "start": gapped[0], "end": gapped[1]}], None
    # 모델이 표 제목이나 분류 이름(25줄 넘게 위에 있거나 원문에 없는 말)을 맨 앞 줄에 붙인 경우: 맨 앞 1~2줄을 빼고
    # 나머지가 원문과 맞으면 나머지만 근거로 둔다. 뺀 줄은 근거 범위에 넣지 않는다.
    lines = [x for x in quote.split("\n") if x.strip()]
    for k in (1, 2):
        rest = "\n".join(lines[k:])
        if len(lines) <= k or nonspace_len(rest) < MIN_QUOTE:
            break
        spans = docs[did].find_quote(rest)
        found = [(a, b) for a, b in spans[:1]] or ([g] if (g := docs[did].find_gapped(rest, max_lines=GAP_LINES)) else [])
        if found:
            if counts is not None:
                bump(counts, f"quote_lead_dropped_{k}")
            return [{"doc": did, "start": found[0][0], "end": found[0][1]}], None
    return [], "quote_not_found"


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


def merge_close(docs: dict[str, Doc], evidence: list[dict]) -> list[dict] | None:
    """서로 3줄 안에 붙은(또는 겹친) 근거 항목을 이어진 범위 하나로 합친다(2차부터, §2.3 보완).

    붙은 항목은 사실 이어진 근거 하나다. 합친 범위가 GAP_LINES줄을 넘으면 None이다. 합친 항목의 대체 근거는
    조각에 대한 것이라 버리고, 뒤의 대체 근거 찾기에서 다시 찾는다.
    """
    n = len(evidence)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            x = parent[x]
        return x

    prim = [e["alternatives"][0] for e in evidence]
    for i in range(n):
        for j in range(i + 1, n):
            if not far_apart(docs, prim[i], prim[j]):
                parent[find(j)] = find(i)
    out = []
    for root in sorted({find(i) for i in range(n)}):
        members = [i for i in range(n) if find(i) == root]
        if len(members) == 1:
            out.append(evidence[members[0]])
            continue
        spans = [prim[i] for i in members]
        if len({s["doc"] for s in spans}) > 1:
            return None
        d = docs[spans[0]["doc"]]
        start, end = min(s["start"] for s in spans), max(s["end"] for s in spans)
        if d.line_at(end - 1).no - d.line_at(start).no + 1 > GAP_LINES:
            return None
        out.append({"alternatives": [{"doc": spans[0]["doc"], "start": start, "end": end}]})
    return out


def in_focus(docs: dict[str, Doc], span: dict, focus: list[tuple[str, int, int]]) -> bool:
    line = docs[span["doc"]].line_at(span["start"]).no
    return any(span["doc"] == d and a <= line <= b for d, a, b in focus)


def bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def validate(docs: dict[str, Doc], call: Call, q: dict, counts: dict[str, int]) -> dict | None:
    """모델이 쓴 질문 하나를 확인해 항목으로 바꾼다. 버리면 None이고 이유를 counts에 센다."""
    evidence = []
    for item in q.get("evidence") or []:
        alts, why = locate(docs, item.get("doc", ""), item.get("quote", ""), counts)
        if why:
            bump(counts, f"drop_evidence_{why}")
            return None
        for loc in item.get("same_elsewhere") or []:
            more, why = locate(docs, loc.get("doc", ""), loc.get("quote", ""), counts)
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
            merged = merge_close(docs, evidence) if ROUND >= 2 and call.kind != "cross" else None
            if merged is None:
                bump(counts, "drop_multi_too_close")
                return None
            evidence = merged
            if len(evidence) == 1:
                qtype = "single"
                bump(counts, "multi_merged_to_single")
            else:
                bump(counts, "multi_merged_some")
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
        self.skipped: list[dict] = []

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
        head_recs = await asyncio.gather(*(self.ask(c, docs, candidates) for c in heads), return_exceptions=True)
        done = {c.id: r for c, r in zip(heads, head_recs)}
        rest = [c for c in calls if c.id not in done]
        rest_recs = await asyncio.gather(*(self.ask(c, docs, candidates) for c in rest), return_exceptions=True)
        for c, r in zip(rest, rest_recs):
            done[c.id] = r
        out = []
        for c in calls:
            r = done[c.id]
            if isinstance(r, Exception):  # 예산에 닿아 보내지 않은 호출. 결과 없이 넘어가고 기록에 남긴다
                self.skipped.append({"call": c.id, "reason": str(r)})
                r = {"call": c.id, "data": None}
            out.append(r)
        return out

    async def aclose(self) -> None:
        await self.client.aclose()


def sample(items: list[dict], group: str, qtype: str) -> list[dict]:
    pool = [i for i in items if i["group"] == group and i["type"] == qtype]
    random.Random(f"{SEED}:{ROUND}:{group}:{qtype}").shuffle(pool)
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
        checks = [Call(f"확인-{g}", "none_check", g, group_docs(docs, g), seed=call_seed(100 + i))
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

        chosen = []
        for (group, qtype), target in TARGETS.items():
            picked = sample(none_kept if qtype == "none" else candidates, group, qtype)
            counts.setdefault(group, {})[f"chosen_{qtype}"] = len(picked)
            counts[group][f"short_{qtype}"] = max(0, target - len(picked))
            chosen += picked
        random.Random(f"{SEED}:{ROUND}:ids").shuffle(chosen)
        for k, item in enumerate(chosen, 1):
            item.pop("cid", None)
            item["id"] = f"qa-{k:04d}"

        alt_counts = await find_alternatives(runner, docs, [i for i in chosen if i["type"] != "none"], "대체")
    finally:
        await runner.aclose()
    counts["대체 근거"] = alt_counts
    return {"items": chosen, "counts": counts, "log": runner.log, "spent": runner.spent, "skipped": runner.skipped,
            "spent_this_run": runner.spent - runner.start_spent}


def evidence_text(docs: dict[str, Doc], span: dict) -> str:
    return docs[span["doc"]].text[span["start"]:span["end"]]


def alt_listings(docs: dict[str, Doc], items: list[dict]) -> list[str]:
    """대체 근거 찾기에 넣을 질문 목록을 글자 수 상한에 맞춰 나눈다. 근거 id는 "질문 id#항목 번호"다."""
    blocks = []
    for item in sorted(items, key=lambda i: i["id"]):
        lines = [f"### {item['id']}", f"질문: {item['question']}", f"답: {item['answer']}"]
        for k, ev in enumerate(item["evidence"], 1):
            first = ev["alternatives"][0]
            lines.append(f"- 근거 {item['id']}#{k} ({first['doc']}):\n{evidence_text(docs, first)}")
        blocks.append("\n".join(lines))
    batches, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b) > ALT_BATCH_CHARS:
            batches.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(b)
        size += len(b)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


async def find_alternatives(runner: "Runner", docs: dict[str, Doc], items: list[dict], prefix: str,
                            groups: tuple[str, ...] = GROUPS) -> dict[str, int]:
    """묶음마다 근거 항목과 같은 내용을 적은 곳을 찾아 대체 근거에 더한다(제자리에서 고친다)."""
    counts: dict[str, int] = {}
    by_id = {i["id"]: i for i in items}
    batches = alt_listings(docs, items)
    calls = [Call(f"{prefix}-{g}-{k}", "alt", g, group_docs(docs, g), seed=call_seed(300 + 10 * gi + k), listing=b)
             for gi, g in enumerate(groups) for k, b in enumerate(batches, 1)]
    counts["calls"] = len(calls)
    recs = await runner.ask_all(calls, docs)
    for call, rec in zip(calls, recs):
        apply_alternatives(docs, by_id, set(call.docs), (rec["data"] or {}).get("results") or [], counts, call.id)
    counts["evidence_items"] = sum(len(i["evidence"]) for i in items)
    counts["items_with_added"] = sum(any(e.get("added_by") for e in i["evidence"]) for i in items)
    return counts


def apply_alternatives(docs: dict[str, Doc], by_id: dict[str, dict], group_ids: set[str], results: list[dict],
                       counts: dict[str, int], source: str) -> None:
    """대체 근거 찾기의 답(근거 id마다 찾은 원문)을 확인해 그 근거 항목의 대체 근거에 더한다."""
    for r in results:
        qid, _, num = str(r.get("evidence_id", "")).partition("#")
        item = by_id.get(qid)
        if item is None or not num.isdigit() or not 1 <= int(num) <= len(item["evidence"]):
            bump(counts, "unknown_evidence_id")
            continue
        ev = item["evidence"][int(num) - 1]
        others = [a for j, e in enumerate(item["evidence"]) if j != int(num) - 1 for a in e["alternatives"]]
        for loc in r.get("locations") or []:
            if loc.get("doc") not in group_ids:
                bump(counts, "outside_group")
                continue
            spans, why = locate(docs, loc.get("doc", ""), loc.get("quote", ""), counts)
            if why:
                bump(counts, f"location_{why}")
                continue
            for s in spans:
                if any(overlaps(s, t) for t in ev["alternatives"]):
                    bump(counts, "already_there")
                elif any(overlaps(s, t) for t in others):
                    bump(counts, "overlaps_other_evidence")  # 다른 근거 항목과 겹치면 더하지 않는다
                else:
                    ev["alternatives"].append(s)
                    ev.setdefault("added_by", []).append(source)
                    bump(counts, "added")


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
        "round": ROUND, "budget_usd": BUDGET_USD,
        "model": MODEL, "seed": SEED, "targets": {f"{g}/{t}": n for (g, t), n in TARGETS.items()},
        "over": OVER, "over_by": {f"{g}/{t}": v for (g, t), v in OVER_BY.items()}, "calls_per_group": CALLS,
        "extra_calls": EXTRA, "cross_pairs": CROSS, "cross_each": CROSS_EACH, "alt_batch_chars": ALT_BATCH_CHARS,
        "none_candidates": NONE_CANDIDATES, "min_quote_nonspace": MIN_QUOTE, "price_usd_per_mtok": price,
        "prompts_sha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest()
                           for k, v in (("answerable", SYSTEM_ANSWERABLE), ("none", SYSTEM_NONE), ("check", SYSTEM_CHECK),
                                        ("alt", SYSTEM_ALT))},
        "counts": result["counts"], "skipped_calls": result["skipped"], "gap_lines": GAP_LINES, "files": {"data/v2/qa/items.jsonl": len(public),
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
    # 2차: 검토 메모를 보고 고친 지시. 대체 근거 찾기는 표준약관 묶음에서만 시험한다(글이 짧아 싸다)
    "4": [("법령", [("ecom-act", 180, 300)]), ("고시", [("dispute-std", 3600, 3900)])],
    "5": [("고시", [("dispute-std", 3600, 3900)])],  # 이어 붙인 여러 근거 질문을 막는 예시를 넣은 뒤(여러 근거만 4개)
}
PILOT_COUNTS = {"5": (1, 4)}  # 시험의 (single, multi). 없으면 3, 2
PILOT_ALT_GROUPS = {"4": ("표준약관",)}


def where(docs: dict[str, Doc], a: dict) -> tuple[str, int, int]:
    d = docs[a["doc"]]
    return a["doc"], d.line_at(a["start"]).no, d.line_at(a["end"] - 1).no


async def pilot(docs: dict[str, Doc], tag: str) -> None:
    """시험 호출. 결과를 보여 주고 버린다(세트에 넣지 않는다)."""
    runner = Runner(LOCAL_DIR / "pilot")
    n_single, n_multi = PILOT_COUNTS.get(tag, (3, 2))
    calls = [Call(f"시험{tag}-{g}", "answerable", g, group_docs(docs, g), focus, n_single, n_multi, seed=SEED - k)
             for k, (g, focus) in enumerate(PILOTS[tag], 1)]
    items = []
    try:
        recs = await asyncio.gather(*(runner.ask(c, docs) for c in calls))
        for call, rec in zip(calls, recs):
            counts: dict[str, int] = {}
            print(f"== {call.id} {json.dumps(rec['usage'], ensure_ascii=False)}")
            for q in (rec["data"] or {}).get("questions") or []:
                item = validate(docs, call, q, counts)
                print(json.dumps(q, ensure_ascii=False))
                if item:
                    item["id"] = f"p{tag}-{len(items) + 1}"
                    items.append(item)
                    for e in item["evidence"]:
                        print("   ->", [where(docs, a) for a in e["alternatives"]])
            print("counts", counts)
        if tag in PILOT_ALT_GROUPS and items:
            alt = await find_alternatives(runner, docs, items, f"시험{tag}-대체", PILOT_ALT_GROUPS[tag])
            print("== 대체 근거 찾기", alt)
            for item in items:
                for k, e in enumerate(item["evidence"], 1):
                    if e.get("added_by"):
                        print(f"   {item['id']}#{k} 더함:", [where(docs, a) for a in e["alternatives"][1:]])
    finally:
        await runner.aclose()
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
    print(json.dumps({"counts": result["counts"], "skipped_calls": len(result["skipped"]), "billed_usd_this_run": round(result["spent_this_run"], 3),
                      "billed_usd_total_with_pilots": round(result["spent"], 3), "items": len(result["items"])},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
