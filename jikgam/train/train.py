"""Unsloth decision model 학습. 기반 LLM에 Clef 형식 판별 헤드와 LoRA를 붙여 학습하고, 어댑터와 병합 모델을 저장한다.

    bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-0.8B --composition a

하이퍼파라미터는 Unsloth 문서의 권장값을 따른다(2026-10-14 확인,
https://unsloth.ai/docs/basics/train-your-own-decision-model-with-unsloth "Recommended settings"):
4비트 LoRA r=16, alpha=16, 학습률 2e-4, 헤드 학습률 1e-4(기본값), 배치 2 × 누적 4, typed-decisions는 2 에폭,
데이터가 적으면 3~4 에폭. 같은 문서의 코드 예시는 학습률 2e-5, max_steps 240이라 권장값과 다르다. 권장값 절을 따랐다.

학습에는 train 분할(과 typed-decisions train)만 쓴다. 평가·선택은 screen.py가 val로 한다.
모델 파일은 저장소 밖(JIKGAM_RUNS, 기본 ~/jikgam-train/runs)에 두고, 기록(train_meta.json)만 저장소에 복사한다.
"""

import argparse
import json
import math
import os
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

from jikgam.train.data import BASE_MODELS, QUESTION_SETS, ROOT, compose, write_jsonl

KST = timezone(timedelta(hours=9))
SHORT = {"unsloth/Qwen3.5-0.8B": "q35-0.8b", "unsloth/Qwen3.5-2B": "q35-2b"}
# (a)·(e)는 한국어 64건·168건뿐이라 문서의 "데이터가 적으면 3~4"에서 4
DEFAULT_EPOCHS = {"a": 4, "b": 2, "c": 2, "d": 2, "e": 4}
# results/training/tokenizer.json: 학습 입력 최대 3,324 토큰(train, mix 문구; q-2026.10.12 문구는 3,112),
# typed-decisions 최대 1,222. 4,096이면 잘리는 입력이 없고, Unsloth Clef 기본값과 같다.
MAX_SEQ = 4096
WARMUP_RATIO = 0.04  # 문서 코드 예시의 warmup 10 / 240 단계와 같은 비율


def versions() -> dict:
    out = {"python": platform.python_version()}
    for p in ("unsloth", "unsloth_zoo", "torch", "transformers", "peft", "trl", "datasets", "bitsandbytes",
              "accelerate"):
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            out[p] = None
    return out


def git_commit() -> str:
    """WSL의 git은 Windows 쪽 worktree를 읽지 못할 수 있어, 실행하는 쪽이 JIKGAM_GIT_COMMIT으로 넘긴다."""
    return os.environ.get("JIKGAM_GIT_COMMIT") or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, choices=list(BASE_MODELS))
    ap.add_argument("--composition", required=True, choices=list(DEFAULT_EPOCHS))
    ap.add_argument("--epochs", type=float)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--head-lr", type=float, default=None, help="기본값은 Unsloth의 1e-4")
    ap.add_argument("--r", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--seed", type=int, default=3407)
    ap.add_argument("--questions", default="mix", choices=list(QUESTION_SETS), help="학습 질문 문구(평가와 같게)")
    ap.add_argument("--name", help="실행 이름. 기본값은 <모델>-<구성>-<질문 세트>")
    args = ap.parse_args()

    import unsloth  # noqa: F401  (transformers보다 먼저 import해야 패치가 적용된다)
    import torch
    from transformers import TrainingArguments
    from unsloth import DecisionTrainer, FastDecisionModel, is_bfloat16_supported

    name = args.name or f"{SHORT[args.base]}-{args.composition}-{args.questions}"
    run_dir = Path(os.environ.get("JIKGAM_RUNS", Path.home() / "jikgam-train/runs")) / name
    run_dir.mkdir(parents=True, exist_ok=True)
    epochs = args.epochs or DEFAULT_EPOCHS[args.composition]

    rows, data_summary = compose(args.composition, seed=args.seed, questions_path=QUESTION_SETS[args.questions])
    data_summary["composed_sha256_16"] = write_jsonl(rows, run_dir / "train_rows.jsonl")

    model, processor = FastDecisionModel.from_pretrained(
        args.base, revision=BASE_MODELS[args.base], max_seq_length=MAX_SEQ, load_in_4bit=True,
    )
    model = FastDecisionModel.get_peft_model(
        model, r=args.r, lora_alpha=args.alpha, lora_dropout=0, use_gradient_checkpointing="unsloth",
        random_state=args.seed,
    )
    items, report = FastDecisionModel.build_dataset(rows, processor, model)
    if report["skipped"] or report["truncated"]:
        raise SystemExit(f"build_dataset이 판단을 버리거나 잘랐다: {report}")

    steps_per_epoch = math.ceil(math.ceil(len(items) / args.batch) / args.grad_accum)
    total_steps = math.ceil(steps_per_epoch * epochs)
    warmup = max(1, round(WARMUP_RATIO * total_steps))
    targs = TrainingArguments(
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=epochs,
        warmup_steps=warmup,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        weight_decay=0.01,
        bf16=is_bfloat16_supported(),
        fp16=not is_bfloat16_supported(),
        eval_strategy="no",
        save_strategy="no",
        logging_steps=1,
        output_dir=str(run_dir / "trainer"),
        report_to="none",
        seed=args.seed,
    )
    trainer = DecisionTrainer(model=model, processing_class=processor, train_dataset=items, args=targs,
                              head_learning_rate=args.head_lr)
    torch.cuda.reset_peak_memory_stats()
    started = datetime.now(KST)
    t0 = time.perf_counter()
    stats = trainer.train()
    train_s = time.perf_counter() - t0
    peak = torch.cuda.max_memory_reserved()

    model.save_pretrained(str(run_dir / "adapter"))
    t1 = time.perf_counter()
    model.save_pretrained_merged(str(run_dir / "merged"))
    merge_s = time.perf_counter() - t1

    meta = {
        "name": name, "started_kst": started.isoformat(timespec="seconds"), "git_commit": git_commit(),
        "base_model": args.base, "base_revision": BASE_MODELS[args.base],
        "data": data_summary, "build_dataset_report": report, "items": len(items),
        "hyperparameters": {
            "load_in_4bit": True, "max_seq_length": MAX_SEQ, "lora_r": args.r, "lora_alpha": args.alpha,
            "lora_dropout": 0, "target_modules": "Unsloth 기본값(언어 모델 선형층, 비전 탑 제외)",
            "decision_head": "clef(Unsloth 기본, width 512)", "learning_rate": args.lr,
            "head_learning_rate": args.head_lr or 1e-4, "epochs": epochs, "per_device_batch": args.batch,
            "grad_accum": args.grad_accum, "steps": stats.global_step, "warmup_steps": warmup,
            "lr_scheduler": "cosine", "weight_decay": 0.01, "precision": "bf16" if is_bfloat16_supported() else "fp16",
            "optimizer": targs.optim if isinstance(targs.optim, str) else str(targs.optim), "seed": args.seed,
        },
        "train_seconds": round(train_s, 1), "merge_save_seconds": round(merge_s, 1),
        "peak_gpu_memory_reserved_gb": round(peak / 2**30, 2),
        "gpu": torch.cuda.get_device_name(0),
        "final_train_loss": stats.training_loss,
        "loss_history": [{"step": h["step"], "loss": h["loss"]} for h in trainer.state.log_history if "loss" in h],
        "versions": versions(),
        "outputs": {"adapter": str(run_dir / "adapter"), "merged": str(run_dir / "merged")},
    }
    (run_dir / "train_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record = ROOT / "results/training/runs" / f"{name}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"학습 {train_s / 60:.1f}분, 최대 GPU 메모리 {peak / 2**30:.1f} GB, 손실 {stats.training_loss:.4f}")
    print(f"병합 모델: {run_dir / 'merged'}")


if __name__ == "__main__":
    main()
