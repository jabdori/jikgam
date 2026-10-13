#!/usr/bin/env bash
# WSL2에서 Unsloth Studio venv로 jikgam.train 모듈을 실행한다.
#   bash jikgam/train/run_wsl.sh tokens
#   bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-0.8B --composition a
# 저장소는 Windows 쪽 작업 폴더(/mnt/c/...)를 그대로 쓰고, 모델·체크포인트는 JIKGAM_RUNS(기본 ~/jikgam-train/runs)에 둔다.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "${UNSLOTH_VENV:-$HOME/.unsloth/studio/unsloth_studio}/bin/activate"
export PYTHONPATH="$REPO"
export JIKGAM_RUNS="${JIKGAM_RUNS:-$HOME/jikgam-train/runs}"
# Unsloth는 현재 폴더에 unsloth_compiled_cache/를 만든다. 저장소가 아니라 실행 폴더에서 돌린다.
mkdir -p "$JIKGAM_RUNS"
cd "$JIKGAM_RUNS"
module="$1"
shift
exec python -m "jikgam.train.$module" "$@"
