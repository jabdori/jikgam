"""학습 경로 점검: 병합 모델을 typed-decisions의 공개 test 분할(영어, 400건)로 평가한다.

    bash jikgam/train/run_wsl.sh td_eval --run q35-0.8b-b

이 저장소의 test 분할과 무관한 공개 벤치마크이고, 선택·튜닝에 쓰지 않는다. Unsloth 문서가 보고한
typed-decisions 정확도(Qwen3.5-0.8B 73%, 2B 78%)와 비교해 학습 경로가 제대로 도는지만 본다.
정확도는 Unsloth `evaluate` 정의(정답 분포의 최빈 라벨 기준)다.
"""

import argparse
import json
import os
from pathlib import Path

from jikgam.train.data import ROOT, load_typed_decisions

SERVE_MAX_LEN = 16384


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()

    import unsloth  # noqa: F401
    from unsloth import FastDecisionModel

    runs = Path(os.environ.get("JIKGAM_RUNS", Path.home() / "jikgam-train/runs"))
    merged = runs / args.run / "merged"
    model, processor = FastDecisionModel.from_pretrained(str(merged), max_seq_length=SERVE_MAX_LEN,
                                                         load_in_4bit=False, use_gradient_checkpointing=False)
    FastDecisionModel.for_inference(model)
    items, report = FastDecisionModel.build_dataset(load_typed_decisions("test"), processor, model)
    result = {"run": args.run, "dataset": "LocalLLaMA/typed-decisions all/test", "report": report,
              **FastDecisionModel.evaluate(model, processor, items)}
    out = ROOT / "results/training/typed-decisions-test" / f"{args.run}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
