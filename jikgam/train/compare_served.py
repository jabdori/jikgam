"""서빙한 답(실행기 원본 로그)이 선별 때 같은 모델로 낸 예측(predictions.jsonl)과 같은지 본다.

    uv run python -m jikgam.train.compare_served results/decisions/val/<run_id> --backend q35-2b-ko-q8 \
        --screening results/training/val-screening/q35-2b-d-mix__q-mix

서빙 방식(PyTorch, llama.cpp GGUF, 양자화)을 바꿔도 가중치의 동작이 그대로인지 확인하는 용도다.
판단(사례 × 질문 × 회차)마다 예측 라벨이 같은지와 확률 차이(noul은 p, choice는 선택지별 확률의 최대 차이)를 센다.
"""

import argparse
import json
from pathlib import Path

from jikgam.decision.types import label_and_prob


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--backend", required=True)
    ap.add_argument("--screening", required=True)
    args = ap.parse_args()

    ref = {}
    for line in (Path(args.screening) / "predictions.jsonl").read_text(encoding="utf-8").splitlines():
        p = json.loads(line)
        ref[p["case"]] = p
    raw = [json.loads(line) for line in (Path(args.run_dir) / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    raw = [r for r in raw if r["backend"] == args.backend]

    n = same = served_correct = ref_correct = missing = 0
    diffs, flips = [], []
    for r in raw:
        p = ref[r["case"]]
        for qid, gold in p["gold"].items():
            n += 1
            a = r["answers"].get(qid)
            rl, _, rd = label_and_prob(p["answers"][qid])
            ref_correct += rl == gold
            if a is None:
                missing += 1
                continue
            sl, _, sd = label_and_prob(a)
            same += sl == rl
            served_correct += sl == gold
            diffs.append(max(abs(sd[k] - rd.get(k, 0.0)) for k in sd))
            if sl != rl:
                flips.append({"rep": r["rep"], "case": r["case"], "qid": qid, "gold": gold, "screening": rl,
                              "served": sl, "screening_p": round(rd[rl], 4), "served_p": round(sd[sl], 4)})
    diffs.sort()
    reps = len({r["rep"] for r in raw})
    out = {"run": Path(args.run_dir).name, "backend": args.backend, "reps": reps, "decisions": n,
           "label_agreement": f"{same}/{n - missing}", "missing": missing,
           "served_correct_per_rep": served_correct / reps, "screening_correct_per_rep": ref_correct / reps,
           "max_abs_prob_diff": round(diffs[-1], 4) if diffs else None,
           "median_abs_prob_diff": round(diffs[len(diffs) // 2], 4) if diffs else None,
           "flips": flips}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
