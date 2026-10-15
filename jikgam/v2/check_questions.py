"""점검용 질문 50개를 다른 계열 모델(Codex)로 만든다(docs/evaluation-v2.md §2.4, §5 보완).

같은 계열 모델(Gemini)이 질문도 쓰고 구간 정보의 "고객이 쓸 만한 표현"도 쓰면 새 방식이 유리해질 수 있다.
이를 보려고 `gpt-6.1-sol`이 묶음마다 추출 원문만 보고 질문을 따로 쓴다. 구간과 구간 정보는 보지 않는다.
근거 원문 확인과 원문 위치 바꾸기는 검색 평가 세트(qa_gen)와 같은 코드로 한다. 50개 모두 test에만 쓴다.

열람 규칙(§2.6): 코디네이터는 이 질문을 열지 않는다. 이 모듈은 수만 출력하고 내용은 파일로만 쓴다.
작업 폴더(지시문에 원문이 들어간다)는 data/v2/local/codex/check/ 아래에 둔다.

    uv run python -m jikgam.v2.check_questions prepare
    uv run python -m jikgam.v2.check_questions run          # Orca 터미널에서 돌린다
    uv run python -m jikgam.v2.check_questions collect
"""

import argparse
import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor

from jikgam.ingest.corpus import GROUPS, ROOT, load_docs
from jikgam.v2 import codex
from jikgam.v2.qa_gen import Call, QUOTE_RULES, bump, docs_block, group_docs, is_local, validate

SEED = 20261016
MODEL = "gpt-6.1-sol"
EFFORT = "high"
WORK = ROOT / "data" / "v2" / "local" / "codex" / "check"
OUT = ROOT / "data" / "v2" / "check"
LOCAL_OUT = ROOT / "data" / "v2" / "local" / "check"
# 묶음 × 유형별 목표 수(§2.4: 법령 20, 고시 15, 표준약관 15, 단일 30, 여러 근거 20). 1.2배를 만들어 고른다.
TARGETS = {("법령", "single"): 12, ("법령", "multi"): 8, ("고시", "single"): 9, ("고시", "multi"): 6,
           ("표준약관", "single"): 9, ("표준약관", "multi"): 6}
OVER = 1.2

LOCATION = {"type": "object", "additionalProperties": False, "required": ["doc", "quote"],
            "properties": {"doc": {"type": "string"}, "quote": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["questions"], "properties": {
    "questions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["question", "type", "answer", "evidence"],
        "properties": {
            "question": {"type": "string"}, "type": {"type": "string", "enum": ["single", "multi"]},
            "answer": {"type": "string"},
            "evidence": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["doc", "quote", "same_elsewhere"],
                "properties": {"doc": {"type": "string"}, "quote": {"type": "string"},
                               "same_elsewhere": {"type": "array", "items": LOCATION}}}}}}}}}


def prompt(group: str, n_single: int, n_multi: int, block: str) -> str:
    return f"""소비자 상담용 근거 검색을 평가할 질문과 정답 근거를 만드는 작업이다. 필요한 글은 모두 이 지시의 끝에 있다. 도구, 파일, 인터넷을 쓰지 않는다. 명령을 실행하지 않는다.

아래 문서들은 PDF에서 뽑은 글이다({group} 묶음). 각 줄 앞의 숫자는 줄 번호이고 탭으로 나뉜다. PDF 줄바꿈이 단어 중간에 있을 수 있다. 표는 눈에 보이는 줄마다 칸을 " | "로 나눠 적었다.

## 쓸 것
- single 질문 {n_single}개, multi 질문 {n_multi}개. 주어진 문서 전체에 고르게 퍼지게 쓴다.

## 질문
- 소비자가 상담 채팅에 실제로 쓸 말투로 쓴다. 근거 문장의 표현을 그대로 옮겨 묻지 않는다.
- 주어진 문서만으로 답이 정해지는 질문만 쓴다. 질문끼리 같은 내용을 묻지 않는다.
- single: 근거 항목 하나로 답이 정해진다.
- multi: 근거 항목이 둘 이상이고, 모든 항목이 있어야 답이 정해진다. 항목끼리 겹치지 않고, 3줄 넘게 떨어져 있거나 다른 문서·별표에 있다.

## 근거 항목(evidence)
- 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다. doc에 문서 id를, quote에 그 부분을 옮긴다.
{QUOTE_RULES}
- 필요한 내용이 떨어진 여러 줄에 걸쳐 있으면 그 첫 줄부터 끝 줄까지 사이 줄을 모두 넣어 한 quote로 옮긴다. 아주 멀리 떨어져 있으면 근거 항목을 나눈다.
- 답에 필요한 만큼만 옮긴다. 너무 짧아 여러 곳에 똑같이 나올 말만 옮기지 않는다.
- same_elsewhere는 같은 내용이 다른 곳에도 적혀 있을 때만 그 위치를 적는 칸이다. 없으면 빈 목록이다.
- answer에는 근거로 정해지는 답을 한두 문장으로 쓴다.

마지막 답은 주어진 JSON 스키마로만 쓴다.

{block}
"""


def counts_for(group: str) -> tuple[int, int]:
    return (round(TARGETS[(group, "single")] * OVER + 0.49), round(TARGETS[(group, "multi")] * OVER + 0.49))


def prepare() -> None:
    docs = load_docs()
    for group in GROUPS:
        n_single, n_multi = counts_for(group)
        t = WORK / group
        (t / "work").mkdir(parents=True, exist_ok=True)
        (t / "prompt.md").write_text(prompt(group, n_single, n_multi, docs_block(docs, group_docs(docs, group))),
                                     encoding="utf-8", newline="\n")
        (t / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        (t / "task.json").write_text(json.dumps({"model": MODEL, "effort": EFFORT, "sandbox": "read-only",
                                                 "workdir": "work", "schema": "schema.json", "timeout_s": 3600,
                                                 "isolated": True}, indent=2), encoding="utf-8")
        print(f"{group}: single {n_single}, multi {n_multi}, 지시문 {len((t / 'prompt.md').read_text(encoding='utf-8')):,}자")


def run() -> None:
    def one(group: str) -> str:
        st = codex.run(WORK / group)
        return f"{group} exit={st['exit']} {st['seconds']}s usage={json.dumps(st['usage'])}"

    with ThreadPoolExecutor(len(GROUPS)) as pool:
        for line in pool.map(one, GROUPS):
            print(line, flush=True)


def collect() -> None:
    docs = load_docs()
    kept, counts, usage = [], {}, {}
    for group in GROUPS:
        t = WORK / group
        st = json.loads((t / "status.json").read_text(encoding="utf-8"))
        usage[group] = {"seconds": st["seconds"], **st["usage"], "exit": st["exit"]}
        c = counts.setdefault(group, {})
        try:
            qs = json.loads((t / "last.json").read_text(encoding="utf-8"))["questions"]
        except (OSError, ValueError, KeyError):
            c["unreadable"] = 1
            continue
        c["written"] = len(qs)
        call = Call(f"점검-{group}", "answerable", group, group_docs(docs, group))
        for q in qs:
            item = validate(docs, call, q, c)
            if item:
                kept.append(item)
                bump(c, f"kept_{item['type']}")
    chosen = []
    for (group, qtype), target in TARGETS.items():
        pool = [i for i in kept if i["group"] == group and i["type"] == qtype]
        random.Random(f"{SEED}:check:{group}:{qtype}").shuffle(pool)
        counts[group][f"chosen_{qtype}"] = len(pool[:target])
        counts[group][f"short_{qtype}"] = max(0, target - len(pool))
        chosen += pool[:target]
    random.Random(f"{SEED}:check:ids").shuffle(chosen)
    for k, item in enumerate(chosen, 1):
        item["id"] = f"ck-{k:03d}"
        item["split"] = "test"
    OUT.mkdir(parents=True, exist_ok=True)
    LOCAL_OUT.mkdir(parents=True, exist_ok=True)
    public = [i for i in chosen if not is_local(docs, i)]
    local = [i for i in chosen if is_local(docs, i)]
    for path, rows in ((OUT / "items.jsonl", public), (LOCAL_OUT / "items.jsonl", local)):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    meta = {"model": MODEL, "effort": EFFORT, "codex_version": st.get("codex_version"), "seed": SEED,
            "targets": {f"{g}/{t}": n for (g, t), n in TARGETS.items()}, "over": OVER,
            "prompt_sha256": {g: hashlib.sha256((WORK / g / "prompt.md").read_bytes()).hexdigest() for g in GROUPS},
            "counts": counts, "usage": usage,
            "files": {"data/v2/check/items.jsonl": len(public), "data/v2/local/check/items.jsonl": len(local)}}
    (OUT / "generation.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                         newline="\n")
    print(json.dumps({"counts": counts, "chosen": len(chosen)}, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["prepare", "run", "collect"])
    args = ap.parse_args()
    {"prepare": prepare, "run": run, "collect": collect}[args.step]()


if __name__ == "__main__":
    main()
