"""서빙 방식별 지연·정확도 표(docs/training.md §10). 실행기 원본 로그와 선별 예측으로만 만든다.

    uv run python -m jikgam.train.latency_table <run_dir>=<backend> ... --screening results/training/val-screening/q35-2b-d-mix__q-mix
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from jikgam.eval.metrics import percentile


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="<실행 폴더>=<백엔드>")
    ap.add_argument("--screening", required=True)
    args = ap.parse_args()
    print("| 실행 | 백엔드 | p50 ms | p95 ms | 최대 ms | 정답(회차 평균)/152 | 선별 예측과 라벨 일치 | 확률 차 중앙값 / 최대 |")
    print("|---|---|---|---|---|---|---|---|")
    for pair in args.pairs:
        run_dir, backend = pair.split("=")
        raw = [json.loads(line) for line in (Path(run_dir) / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
        lat = [r["latency_ms"] for r in raw if r["backend"] == backend and not r["error"]]
        cmp = json.loads(subprocess.run(
            [sys.executable, "-m", "jikgam.train.compare_served", run_dir, "--backend", backend,
             "--screening", args.screening], capture_output=True, text=True, encoding="utf-8").stdout)
        print(f"| {Path(run_dir).name} | {backend} | {percentile(lat, 50):.0f} | {percentile(lat, 95):.0f} | "
              f"{max(lat):.0f} | {cmp['served_correct_per_rep']:.1f} | {cmp['label_agreement']} | "
              f"{cmp['median_abs_prob_diff']} / {cmp['max_abs_prob_diff']} |")


if __name__ == "__main__":
    main()
