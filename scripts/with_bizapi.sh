#!/usr/bin/env bash
# 모의 업무 API를 한 포트에 띄운 채 명령 하나를 실행하고, 끝나면 그 서버만 내린다.
#
#   scripts/with_bizapi.sh <포트> <로그 파일> <고객 데이터 파일>... -- <명령>...
#   scripts/with_bizapi.sh 8111 outputs/logs/bizapi-8111.log domain/org_a/customers.json -- \
#       uv run python -m jikgam.voice.sim --policy C --scenarios s20 --bizapi-url http://127.0.0.1:8111
#
# - 서버 실행 파일은 JIKGAM_BIZAPI_EXE(기본 outputs/bin/jikgam-voice-api.exe)다. 없으면 bizapi/에서 빌드한다.
# - 다른 작업이 서버를 내려도 감시 루프가 다시 띄우고, 다시 띄운 시각을 로그에 남긴다.
# - 끝낼 때는 이 스크립트가 띄운 프로세스만 PID로 내린다. 같은 이름의 다른 서버(동시 실행)는 건드리지 않는다.
set -u
PORT=$1; LOG=$2; shift 2
DATA=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do DATA+=(-data "$1"); shift; done
[ "${1:-}" = "--" ] && shift
EXE=${JIKGAM_BIZAPI_EXE:-outputs/bin/jikgam-voice-api.exe}
if [ ! -x "$EXE" ]; then (cd bizapi && go build -o "../$EXE" ./cmd/bizapi) || exit 1; fi
mkdir -p "$(dirname "$LOG")"
STOP="$LOG.stop"; PIDF="$LOG.pid"
rm -f "$STOP"
: > "$LOG"
(
  while true; do
    "$EXE" -addr "127.0.0.1:$PORT" "${DATA[@]}" >> "$LOG" 2>&1 &
    echo $! > "$PIDF"
    wait $!
    [ -f "$STOP" ] && break
    echo "{\"watchdog\": \"server exited, restarting\", \"time\": \"$(date -Iseconds)\"}" >> "$LOG"
    sleep 1
  done
) &
WD=$!
UP=0
for _ in $(seq 1 40); do curl -s -o /dev/null -X POST "localhost:$PORT/_admin/reset" && { UP=1; break; }; sleep 0.5; done
if [ "$UP" != 1 ]; then echo "모의 업무 API가 $PORT에 뜨지 않아 중단"; touch "$STOP"; kill "$(cat "$PIDF")" 2>/dev/null; exit 1; fi
"$@"
RC=$?
touch "$STOP"
kill "$(cat "$PIDF")" 2>/dev/null
wait $WD 2>/dev/null
echo "명령 종료 코드 $RC, 서버 재시작 $(grep -c watchdog "$LOG")회"
exit $RC
