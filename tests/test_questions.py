import json
from pathlib import Path

from jikgam.eval.questions import load_bank, render

ROOT = Path(__file__).resolve().parents[1]


def test_render_with_original_bank_reproduces_built_questions():
    _, bank = load_bank(ROOT / "data/questions.json")
    for split in ("train", "val"):
        for line in (ROOT / f"data/build/{split}.jsonl").read_text(encoding="utf-8").splitlines():
            built = json.loads(json.loads(line)["questions"])
            assert render(list(built), bank) == built


def test_question_sets_cover_same_ids_and_choices():
    _, base = load_bank(ROOT / "data/questions.json")
    for path in sorted((ROOT / "data/question_sets").glob("*.json")):
        version, bank = load_bank(path)
        assert version and set(bank) == set(base), path
        for qid, q in bank.items():
            assert q["type"] == base[qid]["type"], (path, qid)
            if q["type"] == "choice":
                assert list(q["criteria"]) == list(base[qid]["criteria"]), (path, qid)
        assert "{article_id}" in bank["applies"]["instructions"], path
