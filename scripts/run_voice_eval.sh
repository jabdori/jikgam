#!/usr/bin/env bash
# 음성 평가 실행(docs/evaluation-voice.md §3, 설정 고정은 결정 기록 0018).
#
#   scripts/run_voice_eval.sh holdout   # §3.1: 미공개 평가 시나리오 20개 × 3회, 정책 A와 C를 두 프로세스로 동시에
#   scripts/run_voice_eval.sh barge     # §3.2: 개발용 재생 s20, s21 × 3회, 정책 A와 C를 차례로
#
# 결과: results/voice/scenarios/voice-holdout-{A,C}/, results/voice/voice-barge-{A,C}/. 실행 기록: outputs/logs/voice/.
set -u
cd "$(dirname "$0")/.."
LOGS=outputs/logs/voice
mkdir -p "$LOGS"
case "${1:-}" in
  holdout)
    for P in A C; do
      PORT=$([ "$P" = A ] && echo 8121 || echo 8122)
      bash scripts/with_bizapi.sh "$PORT" "$LOGS/bizapi-holdout-$P.log" \
        domain/org_a/customers.json domain/org_a/customers_holdout.json -- \
        uv run python -m jikgam.voice.sim --policy "$P" --scenarios all --runs 3 --run-id "voice-holdout-$P" \
          --scenario-file data/scenarios/org_a_holdout.jsonl \
          --customers domain/org_a/customers.json --customers domain/org_a/customers_holdout.json \
          --bizapi-url "http://127.0.0.1:$PORT" > "$LOGS/holdout-$P.log" 2>&1 &
    done
    wait
    ;;
  barge)
    for P in A C; do
      bash scripts/with_bizapi.sh 8123 "$LOGS/bizapi-barge-$P.log" domain/org_a/customers.json -- \
        uv run python -m jikgam.voice.replay --replays s20 s21 --policy "$P" --reps 3 --run-id "voice-barge-$P" \
          --bizapi-url http://127.0.0.1:8123 > "$LOGS/barge-$P.log" 2>&1
    done
    ;;
  *)
    echo "사용: $0 holdout|barge"; exit 2 ;;
esac
