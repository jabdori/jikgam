#!/usr/bin/env bash
# ADR 0025의 고정 커밋에서 한 번 실행한다. 완료 파일이 있는 단계는 다시 부르지 않는다.
# WSL2 준비물: ~/jikgam-train/runs/q35-2b-d-mix/merged (고정 가중치),
# ~/jikgam-train/secrets/studio_api_key (키 파일), Unsloth 실행 환경.
# 서버 실행: bash jikgam/train/run_wsl.sh serve_direct --run q35-2b-d-mix --port 8893
# Windows의 인증 값은 동일한 키를 UNSLOTH_API_KEY로 공급한다. 키 내용은 로그에 쓰지 않는다.
# 고정 전: uv run --group embed python -m jikgam.v2.freeze prepare --adr 0025
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0
if [[ ${1:-} == --dry-run ]]; then
  DRY=1
elif [[ $# -gt 0 ]]; then
  printf '사용법: bash scripts/run_v2_test.sh [--dry-run]\n' >&2
  exit 2
fi
LOGDIR=outputs/logs/v2-test
B=b-W4000-C800-raw
CJ=c-jev-W4000-C800-raw-t0.65
CQ=c-qwen-W4000-C800-raw-t0.55

command_line() { printf '%q ' "$@"; printf '\n'; }
stage() {
  local result=$1 log=$2
  shift 2
  command_line "$@"
  if [[ $DRY == 0 ]]; then
    uv run python -m jikgam.v2.stage --name "$log" --result "$result" -- "$@"
  fi
}

if [[ $DRY == 1 ]]; then
  printf 'git status --porcelain\ngit rev-parse HEAD\n'
  command_line uv run python -m jikgam.v2.freeze verify --adr 0025
else
  HEAD_NOW=$(git rev-parse HEAD)
  STATUS=$(git status --porcelain)
  if [[ -f "$LOGDIR/HEAD" ]]; then
    [[ $(cat "$LOGDIR/HEAD") == "$HEAD_NOW" ]] || { printf '최초 실행과 HEAD가 다릅니다.\n' >&2; exit 1; }
    while IFS= read -r line; do
      [[ -z "$line" || "$line" == '?? results/v2/test/'* || "$line" == '?? results/v2/check/'* ]] || {
        printf '재개 결과 외의 변경이 있습니다.\n' >&2; exit 1;
      }
    done <<< "$STATUS"
  else
    [[ -z "$STATUS" ]] || { printf '작업 공간이 깨끗하지 않습니다.\n' >&2; exit 1; }
  fi
  mkdir -p "$LOGDIR"
  printf '%s\n' "$HEAD_NOW" > "$LOGDIR/HEAD"
  uv run python -m jikgam.v2.freeze verify --adr 0025
fi

search() {
  local split=$1 name=$2
  shift 2
  local uv_args=(uv run)
  # 임베딩이 있는 구성만 큰 의존성 묶음을 사용한다.
  for arg in "$@"; do
    if [[ "$arg" == --embed ]]; then uv_args+=(--group embed); break; fi
  done
  stage "results/v2/$split/$name.json" "$split-$name" "${uv_args[@]}" python -m jikgam.v2.run \
    "$@" --split "$split" --frozen 0025
}

# 1. 판정 구성과 보고용 변형.
search test R0-article256 --chunks article256 --r0
search test R0-article0 --chunks article0 --r0
search test fixed256--none--bm25 --chunks fixed256
search test "$B--full--bm25" --chunks "$B" --meta full
search test "$B--full--snow+rrf" --chunks "$B" --meta full --embed snow --fusion rrf
search test "$B--full--snow+rrf+jev8" --chunks "$B" --meta full --embed snow --fusion rrf --rerank 8
search test "$CJ--full--snow+rrf+jev8" --chunks "$CJ" --meta full --embed snow --fusion rrf --rerank 8
search test "$CQ--full--snow+rrf+jev8" --chunks "$CQ" --meta full --embed snow --fusion rrf --rerank 8
search test "$B--full--gemini+w0.3+rel+jev8" --chunks "$B" --meta full --embed gemini --fusion w0.3 --related --rerank 8
search test "$B--full--kure1+rrf+jev8" --chunks "$B" --meta full --embed kure1 --fusion rrf --rerank 8
search test "$B--full--kure2+rrf+jev8" --chunks "$B" --meta full --embed kure2 --fusion rrf --rerank 8
search test "$B--full--gemini+rrf+jev8" --chunks "$B" --meta full --embed gemini --fusion rrf --rerank 8
search test fixed256--full--snow+rrf+jev8 --chunks fixed256 --meta full --embed snow --fusion rrf --rerank 8
search test "$B--none--snow+rrf+jev8" --chunks "$B" --embed snow --fusion rrf --rerank 8
search test "$B--noexpr--snow+rrf+jev8" --chunks "$B" --meta noexpr --embed snow --fusion rrf --rerank 8

# 2. 다른 계열 모델의 점검용 질문.
search check R0-article256 --chunks article256 --r0
search check fixed256--none--bm25 --chunks fixed256
search check "$B--full--snow+rrf+jev8" --chunks "$B" --meta full --embed snow --fusion rrf --rerank 8
search check "$CJ--full--snow+rrf+jev8" --chunks "$CJ" --meta full --embed snow --fusion rrf --rerank 8
search check "$CQ--full--snow+rrf+jev8" --chunks "$CQ" --meta full --embed snow --fusion rrf --rerank 8

# 3. 답변 생성·판정은 한 번씩만 한다.
for name in R0-article256 "$B--full--snow+rrf+jev8"; do
  stage "results/v2/test/answers-$name.json" "answers-$name" uv run python -m jikgam.v2.answer \
    --run "$name" --split test --reps 1 --types single,none --frozen 0025
done

# 4. 덩어리 두 번 재판단. 새 Qwen 실행은 boundary_runs 안에서 인증·해시·판별을 먼저 확인한다.
for method in b c-jev c-qwen; do
  stage "results/v2/test/boundary-reruns-$method.json" "boundary-$method" uv run python -m jikgam.v2.boundary_runs \
    --method "$method" --frozen 0025
done

# 5. 사람 확인은 화면을 만든 뒤 사용자가 한다. 결과 적용은 검토 뒤 별도로 돌린다.
stage data/v2/local/review/claims_test.html claim-check-build uv run python -m jikgam.v2.claim_check build --split test

# 6. 판정 수치는 보고기에서만 재계산한다.
command_line uv run python -m jikgam.v2.report --split test
if [[ $DRY == 0 ]]; then
  uv run python -m jikgam.v2.stage --name report --result results/v2/test/report.json --always -- \
    uv run python -m jikgam.v2.report --split test
fi
