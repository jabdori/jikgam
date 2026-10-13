"""val 선별 결과(results/training/val-screening/*/summary.json)를 한 표로 모은다. 손으로 고치지 않는다.

    uv run python -m jikgam.train.screening_table

val을 몇 번 썼는지 드러내기 위해 선별한 모든 구성을 빠짐없이 적는다.
"""

import json
from pathlib import Path

from jikgam.train.data import ROOT
from jikgam.train.screen import metrics

DIR = ROOT / "results/training/val-screening"
RUNS = ROOT / "results/training/runs"
QTYPES = ["intent", "condition_changed", "applies", "sufficient", "next_info", "suggestion_valid", "action"]


def main() -> None:
    rows = []
    for d in sorted(p for p in DIR.iterdir() if (p / "summary.json").is_file()):
        s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        run = s["run"]
        meta_path = RUNS / f"{run}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        # 보정 전 지표는 반올림해 저장한 summary 대신 저장된 예측(predictions.jsonl)으로 다시 계산한다.
        # 반올림한 값을 다시 반올림하면 표의 마지막 자리가 틀린다(2026-10-14 독립 검토).
        preds = [json.loads(line) for line in (d / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
        s["uncalibrated"] = metrics(preds)
        rows.append((d.name, s, meta))

    L = ["# val 선별 결과", "",
         "`jikgam/train/screening_table.py`가 `*/summary.json`으로 만든다. 손으로 고치지 않는다.", "",
         f"val을 쓴 선별 구성: {len(rows)}개(보정 변형은 아래 열로 함께 적고 따로 세지 않는다). "
         "모델은 서빙과 같은 방식(병합 모델, 16비트, 최대 16,384 토큰)으로 올려 `FastDecisionModel.predict`로 답했다. "
         "정확도·ECE는 저장소 정의(`jikgam.eval.metrics`)다. 보정 후 ECE는 val 전체로 맞춘 온도로 다시 예측한 표본 내 값이고, "
         "Unsloth 교차 적합 ECE는 `FastDecisionModel.calibrate`가 val 사례를 반씩 나눠 계산한 값(Unsloth 정의)이다. "
         "보정 전 지표는 저장된 예측으로 다시 계산하고, 보정 후·Unsloth ECE는 예측을 저장하지 않아 summary.json의 값(소수 넷째 자리 반올림)을 옮긴다.", "",
         "| 선별 | 기반 모델 | 학습 구성 | 학습 문구 | 평가 문구 | 정확도 | ECE | 보정 후 ECE(표본 내) | Unsloth 교차 적합 ECE | 학습 분 | 최대 GPU GB |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s, meta in rows:
        o = s["uncalibrated"]["overall"]
        data = meta.get("data", {})
        L.append(f"| {name} | {meta.get('base_model', '-')} | {data.get('composition', '-')} | "
                 f"{data.get('questions_version', '-') if data.get('korean_cases') else '(한국어 없음)'} | "
                 f"{s['questions_version']} | {o['accuracy'] * 100:.1f}% ({o['correct']}/{o['n']}) | {o['ece']:.3f} | "
                 f"{s['calibrated_in_sample']['overall']['ece']:.3f} | {s['unsloth_calibrate_cross_fitted']['ece']:.3f} | "
                 f"{meta.get('train_seconds', 0) / 60:.1f} | {meta.get('peak_gpu_memory_reserved_gb', '-')} |")
    L += ["", "## 질문 유형별 정확도(보정 전)", "", "| 선별 | " + " | ".join(QTYPES) + " |", "|---|" + "---|" * len(QTYPES)]
    for name, s, _ in rows:
        bt = s["uncalibrated"]["by_qtype"]
        L.append(f"| {name} | " + " | ".join(f"{bt[q]['correct']}/{bt[q]['n']}" if q in bt else "-" for q in QTYPES) + " |")
    L += ["", "## noul 질문: 균형 정확도 / 예측 true 비율", "",
          "| 선별 | condition_changed | applies | sufficient |", "|---|---|---|---|"]
    for name, s, _ in rows:
        nl = s["uncalibrated"]["noul"]

        def cell(q):
            if q not in nl:
                return "-"
            b = nl[q]["balanced_accuracy"]
            return f"{'-' if b is None else f'{b * 100:.1f}%'} / {nl[q]['pred_true_ratio'] * 100:.1f}%"
        L.append(f"| {name} | {cell('condition_changed')} | {cell('applies')} | {cell('sufficient')} |")
    tags = sorted({t for _, s, _ in rows for t in s["uncalibrated"]["by_tag"]})
    L += ["", "## 실패 유형 태그별 정확도(보정 전)", "", "| 선별 | " + " | ".join(tags) + " |", "|---|" + "---|" * len(tags)]
    for name, s, _ in rows:
        bt = s["uncalibrated"]["by_tag"]
        L.append(f"| {name} | " + " | ".join(f"{bt[t]['correct']}/{bt[t]['n']}" if t in bt else "-" for t in tags) + " |")
    text = "\n".join(L) + "\n"
    (DIR / "README.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
