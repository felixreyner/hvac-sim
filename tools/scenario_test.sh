#!/usr/bin/env bash
# End-to-end scenario: steady control, setpoint change, tower failure trip,
# auto-reset, lockout after 3 trips, HMI reset, pump failure, comm loss.
# Run from the repo root with the stack up:  ./tools/scenario_test.sh
set -uo pipefail
hmi() { docker compose run --rm --no-deps -T hmi python tools/hmi.py "$@" 2>/dev/null; }
status() { hmi status; }
wait_for() {  # wait_for <regex> <timeout_s>
  local deadline=$((SECONDS + $2))
  while (( SECONDS < deadline )); do
    s=$(status) && grep -Eq "$1" <<<"$s" && { echo "  ok: $s"; return 0; }
    sleep 5
  done
  echo "  FAIL waiting for /$1/: $s"; exit 1
}

echo "1. plant running under PLC control"
wait_for "state RUNNING .*comp ON" 120

echo "2. setpoint change via S7 DB1 (reset off, SP 8.0)"
hmi spreset off >/dev/null; hmi setpoint 8.0 >/dev/null
wait_for "SP  8.0" 15

echo "3. cooling tower failure -> high condenser pressure trip"
hmi fault tower on
wait_for "alarm 3 " 400
echo "   lockout after 3 trips (auto-reset every 30 s while fault persists)"
wait_for "state FAULT .*alarm 103" 300

echo "4. clear fault, HMI reset -> plant restarts"
hmi fault tower off
sleep 60   # let condenser water cool down
hmi reset >/dev/null
wait_for "state RUNNING .*comp ON" 240

echo "5. CHW pump failure -> flow loss, PLC pump fault"
hmi fault pump on
wait_for "state FAULT .*alarm 101" 60
hmi fault pump off; hmi reset >/dev/null
wait_for "state RUNNING .*comp ON" 240

echo "6. Modbus comm loss (stop chiller container) -> PLC comm fault"
docker compose pause chiller >/dev/null
wait_for "alarm 102" 30
docker compose unpause chiller >/dev/null
wait_for "state RUNNING .*alarm 0 " 30

echo "ALL SCENARIOS PASSED"
