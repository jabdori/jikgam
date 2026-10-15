"""검색 평가 세트를 train/val/test로 나눈다(docs/evaluation-v2.md §2.3 "분할").

- 정답 근거(대체 근거 포함)가 한 글자라도 겹치는 질문은 한 묶음으로 같은 분할에 넣는다.
- 묶음을 대표 질문(가장 작은 id)의 묶음(법령·고시·표준약관·넘나듦) × 유형으로 층을 나누고,
  층마다 고정한 seed로 섞은 뒤 질문 수 기준 비율(train 30%, val 20%, test 50%)에 가장 모자란 분할에 차례로 넣는다.
- 배정한 뒤 test는 바꾸지 않는다(CLAUDE.md 고정 규칙). 이 스크립트는 이미 배정 파일이 있으면 멈춘다.

결과는 id → 분할 파일(data/v2/qa/splits.json)이고 내용은 없다. 수만 출력한다.

    uv run python -m jikgam.v2.splits
"""

import hashlib
import json
import random
import sys

from jikgam.ingest.corpus import ROOT
from jikgam.v2.review import QA_FILES, read

SEED = 20261016
RATIO = {"train": 0.3, "val": 0.2, "test": 0.5}
OUT = ROOT / "data" / "v2" / "qa" / "splits.json"


def clusters(items: list[dict]) -> list[list[dict]]:
    parent = {i["id"]: i["id"] for i in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    spans = [(a["doc"], a["start"], a["end"], i["id"]) for i in items for e in i["evidence"] for a in e["alternatives"]]
    spans.sort()
    for k in range(len(spans)):
        doc, start, end, qid = spans[k]
        j = k + 1
        while j < len(spans) and spans[j][0] == doc and spans[j][1] < end:
            parent[find(spans[j][3])] = find(qid)
            j += 1
    groups: dict[str, list[dict]] = {}
    for i in items:
        groups.setdefault(find(i["id"]), []).append(i)
    return sorted((sorted(g, key=lambda i: i["id"]) for g in groups.values()), key=lambda g: g[0]["id"])


def assign(items: list[dict]) -> dict[str, str]:
    strata: dict[tuple[str, str], list[list[dict]]] = {}
    for c in clusters(items):
        strata.setdefault((c[0]["group"], c[0]["type"]), []).append(c)
    split_of: dict[str, str] = {}
    for key in sorted(strata):
        cs = strata[key]
        random.Random(f"{SEED}:split:{key[0]}:{key[1]}").shuffle(cs)
        total = sum(len(c) for c in cs)
        have = {s: 0 for s in RATIO}
        for c in cs:
            # 지금까지 넣은 뒤 목표에 가장 모자란 분할(같으면 test, val, train 순)
            s = max(RATIO, key=lambda s: (RATIO[s] * total - have[s], ["train", "val", "test"].index(s)))
            have[s] += len(c)
            for i in c:
                split_of[i["id"]] = s
    return split_of


def main() -> None:
    if OUT.exists():
        sys.exit(f"{OUT}가 이미 있다. 배정한 뒤에는 바꾸지 않는다")
    items = read(QA_FILES)
    split_of = assign(items)
    counts: dict[str, dict[str, int]] = {}
    for i in items:
        k = f"{i['group']}/{i['type']}"
        counts.setdefault(k, {s: 0 for s in RATIO})[split_of[i["id"]]] += 1
    files = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in QA_FILES if p.exists()}
    payload = {"seed": SEED, "ratio": RATIO, "items_sha256": files, "clusters": len(clusters(items)),
               "counts": counts, "splits": dict(sorted(split_of.items()))}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"clusters": payload["clusters"], "counts": counts,
                      "totals": {s: sum(v == s for v in split_of.values()) for s in RATIO}}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
