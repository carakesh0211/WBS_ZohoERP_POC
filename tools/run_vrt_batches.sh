#!/usr/bin/env bash
# Bounded, sequential VRT batches — one spec, one viewport, one at a time.
#
# WHY THIS EXISTS. A single `node tools/run_vrt.mjs` over the whole suite ran
# for five hours against a 1.1-hour baseline with nothing observable. It was
# not frozen — it was still spawning browsers — but a run you cannot watch is
# a run you cannot diagnose, and the previous invocation piped through
# `tail -12`, which buffers every line until the process exits. So:
#
#   * ONE SPEC, ONE VIEWPORT PER BATCH. A hang is attributable to a batch
#     rather than to "the suite".
#   * UNBUFFERED. Each batch writes straight to its own log, no pipe. Progress
#     is visible while it runs.
#   * TWO TIMEOUTS. `--timeout` bounds a single test; `timeout` bounds the
#     whole batch, because a runner wedged outside a test is exactly what the
#     per-test timeout cannot catch.
#   * REAL EXIT CODES. Recorded per batch. A batch that times out is reported
#     as TIMEOUT, never folded in with a pass.
#   * ONE SERVER FOR ALL BATCHES. `run_vrt.mjs` otherwise starts its own,
#     which means `migrate --fresh --seed` per batch — minutes of repeated
#     work and a fresh database under every batch. CAPEX_VRT_REUSE=1 attaches
#     instead. The server is started here, once, deliberately.
#
# Usage:  bash tools/run_vrt_batches.sh [spec ...]
# With no arguments it runs every spec in tests/vrt.

set -u

PORT="${CAPEX_VRT_PORT:-8896}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${VRT_BATCH_OUT:-$ROOT/.vrt-batches}"
PER_TEST_MS="${VRT_PER_TEST_MS:-60000}"
PER_BATCH_S="${VRT_PER_BATCH_S:-900}"
PROJECTS="${VRT_PROJECTS:-desktop-1440 laptop-1024 tablet-800}"

mkdir -p "$OUT"
SUMMARY="$OUT/summary.txt"
: > "$SUMMARY"

cd "$ROOT"

if [ "$#" -gt 0 ]; then
  SPECS="$*"
else
  SPECS="$(cd tests/vrt && ls *.spec.js | tr '\n' ' ')"
fi

echo "port=$PORT per_test=${PER_TEST_MS}ms per_batch=${PER_BATCH_S}s" | tee -a "$SUMMARY"
echo "specs: $SPECS" | tee -a "$SUMMARY"
echo "projects: $PROJECTS" | tee -a "$SUMMARY"
echo "" | tee -a "$SUMMARY"

# ---- one server, started here, torn down at the end ----------------------
export CAPEX_PROFILE=local-demo
export CAPEX_DB_PATH="app/data/capex_vrt-$PORT.db"
export PORT
export X_ZOHO_CATALYST_LISTEN_PORT="$PORT"

echo "migrating $CAPEX_DB_PATH ..." | tee -a "$SUMMARY"
python -m app.backend.migrate --fresh --seed > "$OUT/migrate.log" 2>&1
MIG=$?
if [ "$MIG" -ne 0 ]; then
  echo "MIGRATE FAILED rc=$MIG — see $OUT/migrate.log" | tee -a "$SUMMARY"
  exit 1
fi

python app/run.py > "$OUT/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/health"; then break; fi
  sleep 1
done
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
  echo "SERVER DID NOT COME UP — see $OUT/server.log" | tee -a "$SUMMARY"
  exit 1
fi
echo "server up on $PORT (pid $SERVER_PID)" | tee -a "$SUMMARY"
echo "" | tee -a "$SUMMARY"

FAILED=0
for spec in $SPECS; do
  for project in $PROJECTS; do
    label="${spec%.spec.js}::$project"
    log="$OUT/${spec%.spec.js}.$project.log"
    started=$(date '+%H:%M:%S')
    printf '%-46s %s ' "$label" "$started"

    CAPEX_VRT_REUSE=1 CAPEX_VRT_PORT="$PORT" \
      timeout --signal=KILL "$PER_BATCH_S" \
      node tools/run_vrt.mjs "tests/vrt/$spec" \
        --project="$project" --timeout="$PER_TEST_MS" --reporter=line \
        > "$log" 2>&1
    rc=$?

    tail=$(grep -aE "^[[:space:]]*[0-9]+ (passed|failed|skipped)" "$log" | tail -1)
    [ -z "$tail" ] && tail=$(tail -2 "$log" | tr '\n' ' ')
    if [ "$rc" -eq 137 ] || [ "$rc" -eq 124 ]; then
      verdict="TIMEOUT after ${PER_BATCH_S}s"
      FAILED=$((FAILED + 1))
    elif [ "$rc" -eq 0 ]; then
      verdict="ok"
    else
      verdict="FAIL rc=$rc"
      FAILED=$((FAILED + 1))
    fi
    printf '%s  %s\n' "$verdict" "$tail"
    printf '%-46s %s  %s  %s\n' "$label" "$started" "$verdict" "$tail" >> "$SUMMARY"
  done
done

echo "" | tee -a "$SUMMARY"
echo "batches with a non-zero result: $FAILED" | tee -a "$SUMMARY"
exit "$FAILED"
