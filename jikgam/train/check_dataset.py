"""`FastDecisionModel.build_dataset`이 빌드 파일을 그대로 받는지, 변환한 행과 무엇이 다른지 확인한다.

    bash jikgam/train/run_wsl.sh check_dataset --out results/training/dataset_check.json

모델 가중치는 프롬프트 확인에 필요 없지만 build_dataset이 모델 설정(max_len)을 읽으므로 0.8B를 4비트로 올린다.
"""

import argparse
import json

from jikgam.train.data import BASE_MODELS, ROOT, TRAIN, load_korean

MAX_SEQ = 4096


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/training/dataset_check.json")
    args = ap.parse_args()

    import unsloth  # noqa: F401  (transformers보다 먼저)
    from unsloth import FastDecisionModel

    base = "unsloth/Qwen3.5-0.8B"
    model, processor = FastDecisionModel.from_pretrained(base, revision=BASE_MODELS[base], max_seq_length=MAX_SEQ,
                                                         load_in_4bit=True)
    tok = getattr(processor, "tokenizer", processor)

    raw = [json.loads(line) for line in TRAIN.read_text(encoding="utf-8").splitlines()]
    converted = load_korean(TRAIN)
    out = {}
    for name, rows in (("raw_build_file", raw), ("converted", converted)):
        items, report = FastDecisionModel.build_dataset(rows, processor, model)
        first = tok.decode(items[0]["input_ids"])
        ids = [line for line in first.splitlines() if line.startswith("ID: ")]
        out[name] = {"report": report, "items": len(items),
                     "decisions": sum(len(i["targets"]) for i in items),
                     "max_input_tokens": max(len(i["input_ids"]) for i in items),
                     "question_id_lines_first_row": ids,
                     "prompt_head_first_row": first[:400]}
    same_ids = [a["input_ids"] == b["input_ids"] for a, b in zip(*(FastDecisionModel.build_dataset(r, processor, model)[0]
                                                                     for r in (raw, converted)))]
    out["identical_input_ids_rows"] = sum(same_ids)
    path = ROOT / args.out
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
