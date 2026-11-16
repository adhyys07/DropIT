#!/usr/bin/env bash
# Boots a throwaway server, runs every suite, tears it down.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-5099}"
LOG="$(mktemp)"
export DROPIT_URL="http://127.0.0.1:${PORT}"

echo "=== static checks ==="
python tests/test_static.py
static_rc=$?

PORT="$PORT" python server.py > "$LOG" 2>&1 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT

printf 'waiting for %s ' "$DROPIT_URL"
for _ in $(seq 1 60); do
  if curl -sf "$DROPIT_URL/healthz" >/dev/null 2>&1; then echo "up"; break; fi
  printf '.'
  sleep 0.25
done

if ! curl -sf "$DROPIT_URL/healthz" >/dev/null 2>&1; then
  echo "server failed to start:"
  cat "$LOG"
  exit 1
fi

echo
echo "=== signalling protocol ==="
python tests/test_protocol.py
proto_rc=$?

echo
echo "=== data path ==="
node tests/test_engines.mjs
engine_rc=$?

echo
echo "======================================"
printf '  static     %s\n' "$([ $static_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  protocol   %s\n' "$([ $proto_rc  -eq 0 ] && echo PASS || echo FAIL)"
printf '  data path  %s\n' "$([ $engine_rc -eq 0 ] && echo PASS || echo FAIL)"
echo "======================================"

if [ $((static_rc + proto_rc + engine_rc)) -ne 0 ]; then
  echo
  echo "--- server log ---"
  cat "$LOG"
  exit 1
fi
echo "ALL SUITES PASSED"
