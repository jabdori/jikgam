"""2차 세트를 고친다(docs/evaluation-v2.md §2.3 보완 "2차 검토 뒤 고치기", 사용자 결정 2026-10-16).

2차 사람 검토에서 검색 평가 세트의 문제 비율이 18.7%로 기준(10%)을 넘었다. 사용자는 다시 만들지 않고 고친 뒤
새 표본 40개로 다시 재기로 했다. 고치는 것은 두 가지다.

1. 대체 근거 찾기로 더한 근거를 뺀다. 검토 표본에서 이런 근거가 붙은 질문 12개 중 11개가 문제였고, 메모는 주로
   적용 범위가 다른 조항(예: 게임 서비스에만 적용되는 조항)을 일반 거래의 대체 근거로 붙인 것이었다.
   더한 근거는 근거 항목의 대체 근거 목록 끝에 붙어 있고 added_by에 하나씩 기록되어 있어, 끝에서 그 수만큼 뺀다.
2. 검토에서 문제로 표시된 질문을 뺀다(검색 평가 세트 14개, 점검용 2개).

점검용 질문도 같은 방식으로 고친다(점검용은 문제 비율 10.0%로 기준을 넘지 않았지만, 대체 근거 찾기를 같은 이유로 되돌린다).
고치기 전 파일은 data/v2/local/qa/round2_before_repair/와 data/v2/local/check/round2_before_repair/에 둔다.
수만 출력한다.

    uv run python -m jikgam.v2.repair
"""

import hashlib
import json
import shutil

from jikgam.ingest.corpus import ROOT, load_docs
from jikgam.v2.qa_gen import is_local

RESULT = ROOT / "data" / "v2" / "local" / "review" / "round2" / "qa_review_result_r2.json"
SETS = {
    "qa": (ROOT / "data" / "v2" / "qa", ROOT / "data" / "v2" / "local" / "qa"),
    "check": (ROOT / "data" / "v2" / "check", ROOT / "data" / "v2" / "local" / "check"),
}


def sha16(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def strip_added(item: dict) -> int:
    removed = 0
    for ev in item["evidence"]:
        n = len(ev.pop("added_by", []))
        if n:
            ev["alternatives"] = ev["alternatives"][:-n]
            removed += n
    return removed


def remove_reviewed() -> None:
    """확인 검토(2b)에서 문제로 표시된 질문을 검색 평가 세트에서 뺀다. 검토를 통과한 질문의 id를 남긴다(내용 없음)."""
    docs = load_docs()
    review_dir = ROOT / "data" / "v2" / "local" / "review"
    r2 = json.loads((review_dir / "round2" / "qa_review_result_r2.json").read_text(encoding="utf-8"))["items"]
    r2b = json.loads((review_dir / "qa_review_result_r2b.json").read_text(encoding="utf-8"))["items"]
    problems = sorted(k for k, v in r2b.items() if v["failed"])
    pub_dir, loc_dir = SETS["qa"]
    pub, loc = pub_dir / "items.jsonl", loc_dir / "items.jsonl"
    backup = loc_dir / "round2b_before_removal"
    if backup.exists():
        raise SystemExit(f"{backup}가 이미 있다. 한 번만 뺀다")
    backup.mkdir(parents=True)
    shutil.copy2(pub, backup / "public_items.jsonl")
    shutil.copy2(loc, backup / "items.jsonl")
    before = {"public": sha16(pub), "local": sha16(loc)}
    items = [json.loads(line) for p in (pub, loc) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = sorted((i for i in items if i["id"] not in problems), key=lambda i: i["id"])
    for path, rows in ((pub, [i for i in kept if not is_local(docs, i)]), (loc, [i for i in kept if is_local(docs, i)])):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    ids = {i["id"] for i in kept}
    passed = sorted(k for k, v in {**r2, **r2b}.items() if not v["failed"] and k in ids)
    record = json.loads((pub_dir / "repair.json").read_text(encoding="utf-8"))
    record["review_2b"] = {"reason": "확인 검토(2b)의 문제 항목을 뺀다(사용자 결정 2026-10-16: 기준을 넘어도 더 만들지 않음)",
                           "reviewed": len(r2b), "problems": len(problems), "removed_items": problems,
                           "kept": len(kept), "sha256_16_before": before,
                           "sha256_16_after": {"public": sha16(pub), "local": sha16(loc)}}
    (pub_dir / "repair.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                         newline="\n")
    # 사람 검토를 통과한 질문(2차 표본과 확인 표본). 결과 보고에서 참고 값을 낼 때 쓴다
    (pub_dir / "reviewed_passed.json").write_text(json.dumps({"source": ["round2", "2b"], "ids": passed}, indent=1) + "\n",
                                                  encoding="utf-8", newline="\n")
    print(json.dumps({"removed": len(problems), "kept": len(kept), "reviewed_passed": len(passed)}, ensure_ascii=False))


def main() -> None:
    import sys

    if sys.argv[1:] == ["--remove-2b"]:
        remove_reviewed()
        return
    docs = load_docs()
    review = json.loads(RESULT.read_text(encoding="utf-8"))["items"]
    problems = sorted(k for k, v in review.items() if v["failed"])
    for name, (pub_dir, loc_dir) in SETS.items():
        pub, loc = pub_dir / "items.jsonl", loc_dir / "items.jsonl"
        backup = loc_dir / "round2_before_repair"
        if backup.exists():
            raise SystemExit(f"{backup}가 이미 있다. 한 번만 고친다")
        backup.mkdir(parents=True)
        shutil.copy2(pub, backup / "public_items.jsonl")
        shutil.copy2(loc, backup / "items.jsonl")
        before = {"public": sha16(pub), "local": sha16(loc)}
        items = [json.loads(line) for p in (pub, loc) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        items.sort(key=lambda i: i["id"])
        removed_items = [i["id"] for i in items if i["id"] in problems]
        kept = [i for i in items if i["id"] not in problems]
        removed_alts = sum(strip_added(i) for i in kept)
        public = [i for i in kept if not is_local(docs, i)]
        local = [i for i in kept if is_local(docs, i)]
        for path, rows in ((pub, public), (loc, local)):
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
        counts: dict[str, int] = {}
        for i in kept:
            k = f"{i['group']}/{i['type']}"
            counts[k] = counts.get(k, 0) + 1
        record = {"reason": "2차 사람 검토 뒤 고치기(사용자 결정 2026-10-16)", "removed_items": removed_items,
                  "removed_added_alternatives": removed_alts, "kept": len(kept), "kept_by_group_type": counts,
                  "files": {"public": len(public), "local": len(local)},
                  "sha256_16_before": before, "sha256_16_after": {"public": sha16(pub), "local": sha16(loc)}}
        (pub_dir / "repair.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                             newline="\n")
        print(name, json.dumps({k: v for k, v in record.items() if k != "removed_items"} | {"removed_items": len(removed_items)},
                               ensure_ascii=False))


if __name__ == "__main__":
    main()
