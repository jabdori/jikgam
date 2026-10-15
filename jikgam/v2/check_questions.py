"""점검용 질문 50개를 다른 계열 모델(Codex)로 만든다(docs/evaluation-v2.md §2.4, §5 보완).

같은 계열 모델(Gemini)이 질문도 쓰고 구간 정보의 "고객이 쓸 만한 표현"도 쓰면 새 방식이 유리해질 수 있다.
이를 보려고 Codex가 묶음마다 추출 원문만 보고 질문을 따로 쓴다. 구간과 구간 정보는 보지 않는다.
근거 원문 확인과 원문 위치 바꾸기는 검색 평가 세트(qa_gen)와 같은 코드로 한다. 50개 모두 test에만 쓴다.

2차(2026-10-16): 1차(`gpt-6.1-sol`)의 사람 검토에서 문제 비율이 20%로 기준(10%)을 넘었다. §5 보완대로
`gpt-6-astra`로 다시 만들고, 검색 평가 세트와 같은 지시 보강과 대체 근거 찾기를 더한다. 대체 근거 찾기도 Codex가 한다
(Gemini가 끼지 않게). 1차 기록은 data/v2/local/check/round1/, data/v2/local/codex/check-round1/에 있다.

열람 규칙(§2.6): 코디네이터는 이 질문을 열지 않는다. 이 모듈은 수만 출력하고 내용은 파일로만 쓴다.
작업 폴더(지시문에 원문이 들어간다)는 data/v2/local/codex/check/ 아래에 둔다.

    uv run python -m jikgam.v2.check_questions prepare
    uv run python -m jikgam.v2.check_questions run          # Orca 터미널에서 돌린다
    uv run python -m jikgam.v2.check_questions collect      # 고르고, 대체 근거 찾기 작업을 만든다
    uv run python -m jikgam.v2.check_questions run-alt      # Orca 터미널에서 돌린다
    uv run python -m jikgam.v2.check_questions finish
"""

import argparse
import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor

from jikgam.ingest.corpus import GROUPS, ROOT, load_docs
from jikgam.v2 import codex
from jikgam.v2.qa_gen import (QUOTE_RULES, Call, alt_listings, apply_alternatives, bump, docs_block, group_docs,
                              is_local, validate)

SEED = 20261016
ROUND = 2
MODEL = "gpt-6-astra"
EFFORT = "high"
WORK = ROOT / "data" / "v2" / "local" / "codex" / "check"
OUT = ROOT / "data" / "v2" / "check"
LOCAL_OUT = ROOT / "data" / "v2" / "local" / "check"
CHOSEN = LOCAL_OUT / "chosen.jsonl"  # 고른 50개(대체 근거 찾기 전)
# 묶음 × 유형별 목표 수(§2.4: 법령 20, 고시 15, 표준약관 15, 단일 30, 여러 근거 20). 넉넉히 만들어 고른다.
TARGETS = {("법령", "single"): 12, ("법령", "multi"): 8, ("고시", "single"): 9, ("고시", "multi"): 6,
           ("표준약관", "single"): 9, ("표준약관", "multi"): 6}
OVER = 1.3

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
ALT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"], "properties": {
    "results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["evidence_id", "locations"],
        "properties": {"evidence_id": {"type": "string"}, "locations": {"type": "array", "items": LOCATION}}}}}}

HEAD = """필요한 글은 모두 이 지시의 끝에 있다. 도구, 파일, 인터넷을 쓰지 않는다. 명령을 실행하지 않는다.

아래 문서들은 PDF에서 뽑은 글이다({group} 묶음). 각 줄 앞의 숫자는 줄 번호이고 탭으로 나뉜다. PDF 줄바꿈이 단어 중간에 있을 수 있다. 표는 눈에 보이는 줄마다 칸을 " | "로 나눠 적었다."""


def prompt(group: str, n_single: int, n_multi: int, block: str) -> str:
    return f"""소비자 상담용 근거 검색을 평가할 질문과 정답 근거를 만드는 작업이다. {HEAD.format(group=group)}

## 쓸 것
- single 질문 {n_single}개, multi 질문 {n_multi}개. 주어진 문서 전체에 고르게 퍼지게 쓴다.

## 질문
- 소비자가 상담 채팅에 실제로 쓸 말투로 쓴다. 근거 문장의 표현을 그대로 옮겨 묻지 않는다.
- 하나의 상담 상황에서 나오는 질문 하나를 쓴다. 별개의 두 문의를 "그리고", "또", "혹시"로 이어 붙이지 않는다.
- 주어진 문서만으로 답이 정해지는 질문만 쓴다. 문서만으로 단정할 수 없는 사실 판단이 필요한 질문(예: 한 번 써 본 것이 "가치가 현저히 감소한" 경우인지)은 쓰지 않는다.
- 질문이 여러 가지를 묻거나 목록 전체를 물으면, 답과 근거가 그 전부를 담아야 한다. 질문끼리 같은 내용을 묻지 않는다.
- single: 근거 항목 하나로 답이 정해진다.
- multi: 물음 하나에 답하는 데 근거 항목 둘 이상이 함께 필요하다. 항목끼리 겹치지 않고, 3줄 넘게 떨어져 있거나 다른 문서·별표에 있다. 예: 법률이 "대통령령으로 정하는 이율"이라고만 정하고 시행령이 그 이율을 정한 경우.

## 근거 항목(evidence)
- 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다. doc에 문서 id를, quote에 그 부분을 옮긴다.
{QUOTE_RULES}
- 그 내용이 적용되는 조건을 함께 담는다. 표에서는 그 줄이 속한 상위 줄(분쟁 유형, 조건, 품목 이름)부터 옮긴다. 상위 줄이 25줄 넘게 떨어져 있으면 별도 근거 항목으로 둔다.
- 문장 중간에서 끊지 않는다. 답에 영향을 주는 단서("다만, …")와 예외도 넣는다. 너무 짧아 여러 곳에 똑같이 나올 말만 옮기지 않는다.
- same_elsewhere는 같은 내용이 주어진 문서의 다른 곳에도 적혀 있을 때만 그 위치를 적는 칸이다. 없으면 빈 목록이다.
- answer에는 근거로 정해지는 답을 쓴다. 답이 조건이나 예외에 따라 달라지면 그 조건과 예외를 밝힌다. 근거에 없는 경우까지 일반화하지 않는다.

마지막 답은 주어진 JSON 스키마로만 쓴다.

{block}
"""


def alt_prompt(group: str, listing: str, block: str) -> str:
    return f"""검색 평가 세트의 정답 근거에 빠진 대체 근거를 찾는 작업이다. {HEAD.format(group=group)}

- 아래 "질문과 정답 근거"에는 질문마다 질문, 답, 정답 근거 항목(근거 id와 원문)이 있다. 근거 항목들은 다른 문서에서 왔을 수 있다.
- 근거 항목마다, 주어진 문서에서 그 항목과 같은 내용을 적어 그 항목 대신 답의 근거가 될 수 있는 곳을 모두 찾는다. 예: 법률과 표준약관이 같은 환급 기한을 적은 곳, 시행령이나 고시가 법률의 기준을 다시 적은 곳.
- 주어진 근거 원문의 위치 자체는 적지 않는다. 관련은 있지만 그 항목의 내용을 담지 않은 곳(다른 조건, 일부만 같은 내용)도 적지 않는다.
- 찾은 곳은 doc과 quote로 적는다. 그 내용이 적용되는 조건까지 담아 옮긴다.
{QUOTE_RULES}
- 없으면 locations를 빈 목록으로 둔다. 근거 id마다 results에 하나씩 답한다.

마지막 답은 주어진 JSON 스키마로만 쓴다.

## 질문과 정답 근거
{listing}

{block}
"""


def counts_for(group: str) -> tuple[int, int]:
    return (round(TARGETS[(group, "single")] * OVER + 0.49), round(TARGETS[(group, "multi")] * OVER + 0.49))


def task(t, text: str, schema: dict) -> None:
    (t / "work").mkdir(parents=True, exist_ok=True)
    (t / "prompt.md").write_text(text, encoding="utf-8", newline="\n")
    (t / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (t / "task.json").write_text(json.dumps({"model": MODEL, "effort": EFFORT, "sandbox": "read-only", "workdir": "work",
                                             "schema": "schema.json", "timeout_s": 3600, "isolated": True}, indent=2),
                                 encoding="utf-8")


def prepare() -> None:
    docs = load_docs()
    for group in GROUPS:
        n_single, n_multi = counts_for(group)
        task(WORK / group, prompt(group, n_single, n_multi, docs_block(docs, group_docs(docs, group))), SCHEMA)
        print(f"{group}: single {n_single}, multi {n_multi}, 지시문 {len((WORK / group / 'prompt.md').read_text(encoding='utf-8')):,}자")


def run(prefix: str = "") -> None:
    def one(group: str) -> str:
        st = codex.run(WORK / f"{prefix}{group}")
        return f"{prefix}{group} exit={st['exit']} {st['seconds']}s usage={json.dumps(st['usage'])}"

    with ThreadPoolExecutor(len(GROUPS)) as pool:
        for line in pool.map(one, GROUPS):
            print(line, flush=True)


def status_of(t) -> dict:
    st = json.loads((t / "status.json").read_text(encoding="utf-8"))
    return {"seconds": st["seconds"], **st["usage"], "exit": st["exit"], "codex_version": st.get("codex_version")}


def collect() -> None:
    """질문을 확인해 고르고(대체 근거 전), 대체 근거 찾기 작업을 만든다."""
    docs = load_docs()
    kept, counts = [], {}
    for group in GROUPS:
        c = counts.setdefault(group, {})
        try:
            qs = json.loads((WORK / group / "last.json").read_text(encoding="utf-8"))["questions"]
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
        random.Random(f"{SEED}:{ROUND}:check:{group}:{qtype}").shuffle(pool)
        counts[group][f"chosen_{qtype}"] = len(pool[:target])
        counts[group][f"short_{qtype}"] = max(0, target - len(pool))
        chosen += pool[:target]
    random.Random(f"{SEED}:{ROUND}:check:ids").shuffle(chosen)
    for k, item in enumerate(chosen, 1):
        item["id"] = f"ck-{k:03d}"
        item["split"] = "test"
    LOCAL_OUT.mkdir(parents=True, exist_ok=True)
    CHOSEN.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in chosen), encoding="utf-8", newline="\n")
    (LOCAL_OUT / "collect_counts.json").write_text(json.dumps(counts, ensure_ascii=False, indent=1), encoding="utf-8")
    listing = "\n\n".join(alt_listings(docs, chosen))
    for group in GROUPS:
        task(WORK / f"대체-{group}", alt_prompt(group, listing, docs_block(docs, group_docs(docs, group))), ALT_SCHEMA)
    print(json.dumps({"counts": counts, "chosen": len(chosen)}, ensure_ascii=False, indent=1))


def finish() -> None:
    docs = load_docs()
    chosen = [json.loads(line) for line in CHOSEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = json.loads((LOCAL_OUT / "collect_counts.json").read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in chosen}
    alt_counts: dict[str, int] = {}
    for group in GROUPS:
        t = WORK / f"대체-{group}"
        try:
            results = json.loads((t / "last.json").read_text(encoding="utf-8"))["results"]
        except (OSError, ValueError, KeyError):
            bump(alt_counts, f"unreadable_{group}")
            continue
        apply_alternatives(docs, by_id, set(group_docs(docs, group)), results, alt_counts, f"codex-대체-{group}")
    alt_counts["evidence_items"] = sum(len(i["evidence"]) for i in chosen)
    alt_counts["items_with_added"] = sum(any(e.get("added_by") for e in i["evidence"]) for i in chosen)
    counts["대체 근거"] = alt_counts
    OUT.mkdir(parents=True, exist_ok=True)
    public = [i for i in chosen if not is_local(docs, i)]
    local = [i for i in chosen if is_local(docs, i)]
    for path, rows in ((OUT / "items.jsonl", public), (LOCAL_OUT / "items.jsonl", local)):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    names = list(GROUPS) + [f"대체-{g}" for g in GROUPS]
    meta = {"round": ROUND, "model": MODEL, "effort": EFFORT, "seed": SEED,
            "targets": {f"{g}/{t}": n for (g, t), n in TARGETS.items()}, "over": OVER,
            "prompt_sha256": {n: hashlib.sha256((WORK / n / "prompt.md").read_bytes()).hexdigest() for n in names},
            "counts": counts, "usage": {n: status_of(WORK / n) for n in names},
            "files": {"data/v2/check/items.jsonl": len(public), "data/v2/local/check/items.jsonl": len(local)}}
    (OUT / "generation.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                         newline="\n")
    print(json.dumps({"counts": counts, "chosen": len(chosen), "files": meta["files"]}, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["prepare", "run", "collect", "run-alt", "finish"])
    args = ap.parse_args()
    if args.step == "run-alt":
        run("대체-")
    else:
        {"prepare": prepare, "run": run, "collect": collect, "finish": finish}[args.step]()


if __name__ == "__main__":
    main()
