#!/usr/bin/env bash
# Boots a throwaway server, runs every suite, tears it down.
set -uo pipefail
cd "$(dirname "$0")/.."

# The suites exercise the local path and must never touch a real storage
# backend, even when .env points at one. load_dotenv does not override
# variables that are already set, so these win.
export STORAGE_BACKEND=local
# Small parts so the resumable suite exercises real multi-part
# behaviour instead of a single part that can never be resumed.
export UPLOAD_PART_SIZE=65536
unset CDN_TOKEN

PORT="${PORT:-5099}"
LOG="$(mktemp)"
export DROPIT_URL="http://127.0.0.1:${PORT}"

echo "=== static checks ==="
python tests/test_static.py
static_rc=$?

echo
echo "=== storage backends ==="
python tests/test_backends.py
backend_rc=$?

echo "=== hack club cdn ==="
python tests/test_cdn.py
cdn_rc=$?

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
echo "=== hosted files ==="
python tests/test_hosting.py
hosting_rc=$?

echo "=== compression ==="
python tests/test_compression.py
compress_rc=$?

echo "=== resumable uploads ==="
python tests/test_resumable.py
resume_rc=$?

echo
echo "======================================"
printf '  static     %s\n' "$([ $static_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  protocol   %s\n' "$([ $proto_rc  -eq 0 ] && echo PASS || echo FAIL)"
printf '  data path  %s\n' "$([ $engine_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  hosting    %s\n' "$([ $hosting_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  backends   %s\n' "$([ $backend_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  compress   %s\n' "$([ $compress_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  cdn        %s\n' "$([ $cdn_rc -eq 0 ] && echo PASS || echo FAIL)"
printf '  resumable  %s\n' "$([ $resume_rc -eq 0 ] && echo PASS || echo FAIL)"
echo "======================================"

if [ $((static_rc + proto_rc + engine_rc + hosting_rc + backend_rc + compress_rc + cdn_rc + resume_rc)) -ne 0 ]; then
  echo
  echo "--- server log ---"
  cat "$LOG"
  exit 1
fi
echo "ALL SUITES PASSED"
