"""학습한 모델을 val로 빠르게 선별한다(실행기 비교 전 단계). 최종 비교는 실행기(run_decisions.py)로 한다.

    bash jikgam/train/run_wsl.sh screen --run q35-0.8b-a

병합 모델 폴더를 Unsloth Studio의 Decision API와 같은 방식으로 올리고(max_seq_length 16,384, 메모리가 충분하면 16비트),
같은 함수(`FastDecisionModel.predict` → `_clef_decide`)로 val 21건에 답한다. 질문은 실행기와 같게 질문 세트로
다시 만들고 키를 요청용 키로 바꾼다. 지표는 저장소 정의(jikgam.eval.metrics, label_and_prob)로 계산한다.

보정: 보정 전 정확도·ECE가 주 지표다. 그다음 `FastDecisionModel.calibrate`를 val에 맞추고,
(1) Unsloth가 돌려주는 값(val 사례를 반씩 나눠 서로의 절반으로 맞춘 온도로 계산)과
(2) val 전체로 맞춘 온도로 다시 예측해 저장소 정의로 계산한 ECE(표본 내)를 함께 적는다.
보정은 병합 모델 파일을 바꾸지 않는다(저장하지 않는다). 온도 조정이라 예측 라벨과 정확도는 바뀌지 않는다.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from jikgam.decision.types import label_and_prob, wire_id
from jikgam.eval.metrics import balanced_accuracy, ece, percentile
from jikgam.eval.questions import load_bank
from jikgam.train.data import QUESTION_SETS, ROOT, load_val

SERVE_MAX_LEN = 16384  # studio/backend/core/systemone/clef_runtime.py MAX_LENGTH
NOUL = {"condition_changed", "applies", "sufficient", "suggestion_valid"}


def wire_answer(a: dict) -> dict:
    """predict 결과를 Decision API 응답 형식으로 줄인다."""
    if a["type"] == "noul":
        return {"type": "noul", "noul": float(a["noul"])}
    return {"type": "choice", "choice": a["choice"], "probabilities": {k: float(v) for k, v in a["probabilities"].items()}}


def predict_all(model, processor, rows) -> list[dict]:
    from unsloth import FastDecisionModel

    out = []
    for r in rows:
        questions = json.loads(r["questions"])
        t0 = time.perf_counter()
        answers = FastDecisionModel.predict(model, processor, r["state"], questions)
        ms = (time.perf_counter() - t0) * 1000
        back = {wire_id(q): q for q in (k.replace("__", ":") for k in questions)}
        out.append({"case": r["id"], "tags": r["tags"], "latency_ms": round(ms, 1),
                    "answers": {back[k]: wire_answer(v) for k, v in answers.items()},
                    "gold": {back[k]: g["label"] for k, g in json.loads(r["gold"]).items()}})
    return out


def metrics(preds: list[dict]) -> dict:
    """반올림하지 않은 값을 낸다. 표에 쓸 때 한 번만 반올림한다."""
    ds = []
    for p in preds:
        for qid, gold in p["gold"].items():
            label, prob, _ = label_and_prob(p["answers"][qid])
            ds.append({"qtype": qid.split(":")[0], "gold": gold, "pred": label, "prob": prob,
                       "correct": label == gold, "tags": p["tags"]})

    def acc(sub):
        return {"correct": sum(d["correct"] for d in sub), "n": len(sub),
                "accuracy": sum(d["correct"] for d in sub) / len(sub) if sub else None}

    by_type = defaultdict(list)
    by_tag = defaultdict(list)
    for d in ds:
        by_type[d["qtype"]].append(d)
        for t in d["tags"]:
            by_tag[t].append(d)
    noul = {}
    for q in NOUL & set(by_type):
        sub = by_type[q]
        golds = {d["gold"] for d in sub}
        noul[q] = {"balanced_accuracy": balanced_accuracy([d["pred"] for d in sub], [d["gold"] for d in sub])
                   if len(golds) == 2 else None,
                   "pred_true_ratio": sum(d["pred"] == "true" for d in sub) / len(sub)}
    return {
        "overall": {**acc(ds), "ece": ece([d["prob"] for d in ds], [d["correct"] for d in ds])},
        "by_qtype": {q: {**acc(sub), "ece": ece([d["prob"] for d in sub], [d["correct"] for d in sub])}
                     for q, sub in sorted(by_type.items())},
        "noul": noul,
        "by_tag": {t: acc(sub) for t, sub in sorted(by_tag.items())},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="실행 이름(JIKGAM_RUNS 아래 폴더) 또는 병합 모델 경로")
    ap.add_argument("--questions", default="mix", choices=list(QUESTION_SETS), help="평가 질문 문구")
    ap.add_argument("--out", help="기본값 results/training/val-screening/<실행 이름>__q-<질문 세트>")
    args = ap.parse_args()

    import unsloth  # noqa: F401
    import torch
    from unsloth import FastDecisionModel

    runs = Path(os.environ.get("JIKGAM_RUNS", Path.home() / "jikgam-train/runs"))
    run_dir = Path(args.run) if Path(args.run).is_dir() else runs / args.run
    merged = run_dir / "merged" if (run_dir / "merged").is_dir() else run_dir
    name = run_dir.name if merged != run_dir else merged.parent.name
    out_dir = ROOT / (args.out or f"results/training/val-screening/{name}__q-{args.questions}")
    out_dir.mkdir(parents=True, exist_ok=True)

    model, processor = FastDecisionModel.from_pretrained(str(merged), max_seq_length=SERVE_MAX_LEN,
                                                         load_in_4bit=False, use_gradient_checkpointing=False)
    FastDecisionModel.for_inference(model)
    questions_path = QUESTION_SETS[args.questions]
    rows = load_val(questions_path)

    predict_all(model, processor, rows[:1])  # 예열(기록하지 않음)
    preds = predict_all(model, processor, rows)
    uncal = metrics(preds)

    items, report = FastDecisionModel.build_dataset(rows, processor, model)
    unsloth_uncal = FastDecisionModel.evaluate(model, processor, items)
    unsloth_cal = FastDecisionModel.calibrate(model, processor, items)  # model.decision_config의 온도를 바꾼다
    cal_preds = predict_all(model, processor, rows)
    cal = metrics(cal_preds)

    lat = [p["latency_ms"] for p in preds]
    summary = {
        "run": name, "merged": str(merged), "val_cases": len(rows),
        "questions_version": load_bank(questions_path)[0],
        "loaded_in_4bit": bool(model.decision_config.get("load_in_4bit")),
        "uncalibrated": uncal,
        "calibrated_in_sample": {"overall": cal["overall"], "by_qtype": cal["by_qtype"],
                                 "temperatures": {k: model.decision_config.get(k)
                                                  for k in ("head_temperature", "temperature")}},
        "unsloth_evaluate_uncalibrated": unsloth_uncal,
        "unsloth_calibrate_cross_fitted": unsloth_cal,
        "build_dataset_report": report,
        "in_process_predict_ms": {"p50": round(percentile(lat, 50), 1), "p95": round(percentile(lat, 95), 1),
                                  "note": "모델과 같은 프로세스에서 잰 predict 시간. 실행기 지연(HTTP 왕복)과 다르다."},
        "gpu": torch.cuda.get_device_name(0),
    }
    with (out_dir / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for p in preds:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                          newline="\n")
    o = uncal["overall"]
    print(f"{name}: val {o['correct']}/{o['n']} = {o['accuracy']:.3f}, ECE {o['ece']:.3f} "
          f"(보정 후 표본 내 ECE {cal['overall']['ece']:.3f}, Unsloth 교차 적합 ECE {unsloth_cal['ece']:.3f})")


if __name__ == "__main__":
    main()
