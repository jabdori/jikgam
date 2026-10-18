"""사람 확인의 입력 해시를 실제 합성 파일에 묶는 공통 자료."""

import json

from jikgam.v2 import claim_check


def bind_review_inputs(root, sample, result=None):
    rows = {}
    for n, chosen in enumerate(sample["items"]):
        name = chosen.get("run", claim_check.RUNS[0])
        claim = f"합성 주장 {n}"
        chosen.update({"run": name, "id": chosen.get("id", f"q{n}"), "rep": 0,
                       "row": len(rows.get(name, [])), "claim_index": 0, "claim_sha256": claim_check.text_hash(claim)})
        rows.setdefault(name, []).append({"id": chosen["id"], "type": "single", "group": "법령", "rep": 0,
                                         "claims": [{"claim": claim, "supported": chosen["supported"],
                                                     "correctness": chosen["correctness"]}]})
    for name, values in rows.items():
        for kind, records in (("answers", values), ("runs", [{"id": r["id"], "budget4000": []} for r in values])):
            path = root / f"data/v2/local/{kind}" / sample["split"] / f"{name}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    sample["source_hashes"] = claim_check.input_hashes(root, sample["split"])
    sample["sample_digest"] = claim_check.sample_digest(sample["items"], sample["source_hashes"])
    if result is not None:
        result["sample_digest"] = sample["sample_digest"]
