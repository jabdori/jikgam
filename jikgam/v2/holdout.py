"""v2 미공개 시나리오의 법령·고시 정답 근거를 원문 위치로 바꾼다(docs/evaluation-v2.md §2.5).

미공개 시나리오(data/scenarios/org_a_v2_holdout.jsonl)는 엔진 개발에 참여하지 않는 별도 작성자가 쓴다. 작성자는
expected.law_quotes에 원문을 그대로 옮겨 적고, 이 도구로 원문 위치(expected.law_evidence)를 채운다. 확인 규칙은
검색 평가 세트와 같다(공백·"|" 무시, 25줄 안의 건너뛴 줄, 같은 내용이 다른 곳에 있으면 same_elsewhere).

열람 규칙: 엔진 개발자(코디네이터 포함)는 이 파일을 엔진 설정 고정 전에 열지 않는다. 그래서 이 도구는 내용을 출력하지
않고, 시나리오 id와 수만 출력한다.

    uv run python -m jikgam.v2.holdout map [--file data/scenarios/org_a_v2b_holdout.jsonl]

--file을 생략하면 첫 v2 미공개 파일이다. 두 번째 묶음(data/scenarios/HOLDOUT_V2B.md)은 --file로 고른다.
"""

import json
import sys

from jikgam.ingest.corpus import ROOT, load_docs
from jikgam.v2.qa_gen import Call, validate

PATH = ROOT / "data" / "scenarios" / "org_a_v2_holdout.jsonl"
FILES = (PATH, ROOT / "data" / "scenarios" / "org_a_v2b_holdout.jsonl")


def map_quotes(path=PATH) -> int:
    docs = load_docs()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    bad = 0
    for r in rows:
        quotes = r["expected"].get("law_quotes") or []
        q = {"question": "", "type": "multi" if len(quotes) > 1 else "single", "answer": "",
             "evidence": [{"doc": x["doc"], "quote": x["quote"], "same_elsewhere": x.get("same_elsewhere") or []}
                          for x in quotes]}
        counts: dict[str, int] = {}
        item = validate(docs, Call("holdout", "answerable", "", list(docs)), q, counts) if quotes else None
        if item is None:
            bad += 1
            print(json.dumps({"id": r["id"], "law_quotes": len(quotes), "problem": counts or "law_quotes 없음"},
                             ensure_ascii=False))
            continue
        r["expected"]["law_evidence"] = item["evidence"]
        print(json.dumps({"id": r["id"], "law_items": len(item["evidence"]),
                          "alternatives": sum(len(e["alternatives"]) for e in item["evidence"]),
                          "notes": counts}, ensure_ascii=False))
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    print(json.dumps({"scenarios": len(rows), "failed": bad,
                      "handoff": sum(r["expected"].get("action") == "handoff" for r in rows),
                      "condition_change": sum(any(e.get("condition_change") for e in r.get("mid_call_events", []))
                                              for r in rows)}, ensure_ascii=False))
    return bad


def main() -> None:
    args = sys.argv[1:]
    usage = "사용법: uv run python -m jikgam.v2.holdout map [--file <미공개 시나리오 파일>]"
    if args[:1] != ["map"] or len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--file"):
        raise SystemExit(usage)
    path = (ROOT / args[2]).resolve() if len(args) == 3 else PATH
    if path not in {f.resolve() for f in FILES}:
        raise SystemExit("미공개 시나리오 파일은 정해진 경로만 받는다: " + ", ".join(f.relative_to(ROOT).as_posix() for f in FILES))
    sys.exit(1 if map_quotes(path) else 0)


if __name__ == "__main__":
    main()
