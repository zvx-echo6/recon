#!/usr/bin/env bash
# sweep_gated.sh — Qdrant-gated sweep wrapper for Stream B.2 Phase 4
# Runs recon.py pipeline sweep in bounded chunks with Qdrant health checks
# between each invocation. Aborts cleanly if Qdrant becomes unreachable.

set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://192.168.1.150:6333/collections/recon_knowledge_hybrid}"
BATCH_SIZE="${BATCH_SIZE:-500}"
MAX_ENTRIES="${MAX_ENTRIES:-500}"
PLAN_FILE="${PLAN_FILE:-/opt/recon/data/sweep/sweep_plan.json}"
RECON_DIR="/opt/recon"
# Checkpoint co-locates with plan file: plan.json -> plan_checkpoint.json
CHECKPOINT_FILE="${PLAN_FILE%.json}_checkpoint.json"

log() { echo "[$(date +%Y-%m-%dT%H:%M:%S)] $*"; }

probe_qdrant() {
    local resp
    resp=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$QDRANT_URL" 2>/dev/null) || true
    if [ "$resp" = "200" ]; then
        return 0
    else
        return 1
    fi
}

report_progress() {
    if [ -f "$CHECKPOINT_FILE" ]; then
        python3 -c "
import json
cp = json.load(open('$CHECKPOINT_FILE'))
s = cp['stats']
idx = cp['last_completed_index']
print(f'  last_completed_index={idx}')
print(f'  relocated={s[\"relocated\"]} rescued={s[\"rescued\"]} unclassified={s[\"unclassified_moved\"]}')
print(f'  noop={s[\"no_op_marked\"]} dup={s[\"duplicates\"]} skip={s[\"skipped\"]} fail={s[\"failed\"]}')
print(f'  qdrant_updated={s[\"qdrant_updated\"]}')
" 2>/dev/null || log "  (could not read checkpoint)"
    else
        log "  no checkpoint file at $CHECKPOINT_FILE"
    fi
}

parse_processed() {
    # Parse the sweep output to count total entries processed this iteration
    python3 -c "
import sys, re
lines = sys.stdin.read()
total = 0
for key in ['Relocated', 'Rescued', 'Unclassified moved', 'No-op .marked.', 'Duplicates', 'Skipped', 'Failed']:
    m = re.search(key + r':\s+(\d+)', lines)
    if m:
        total += int(m.group(1))
print(total)
" 2>/dev/null || echo "-1"
}

log "Plan file: $PLAN_FILE"
log "Batch size: $BATCH_SIZE, Max entries per chunk: $MAX_ENTRIES"

iteration=0

while true; do
    iteration=$((iteration + 1))
    log "=== Iteration $iteration ==="

    # Pre-flight Qdrant probe
    log "Probing Qdrant at $QDRANT_URL ..."
    if ! probe_qdrant; then
        log "ABORT: Qdrant unreachable before iteration $iteration"
        report_progress
        exit 1
    fi
    log "Qdrant OK"

    # Run sweep chunk
    log "Running: recon.py pipeline sweep --execute --resume --batch-size $BATCH_SIZE --max-entries $MAX_ENTRIES --plan-file $PLAN_FILE"
    set +e
    output=$(cd "$RECON_DIR" && python3 recon.py pipeline sweep --execute --resume \
        --batch-size "$BATCH_SIZE" --max-entries "$MAX_ENTRIES" --plan-file "$PLAN_FILE" 2>&1)
    rc=$?
    set -e

    echo "$output"

    if [ $rc -ne 0 ]; then
        log "ABORT: recon.py exited with code $rc"
        report_progress
        exit 2
    fi

    # Check if sweep is done (all counters zero = nothing left to process)
    processed=$(echo "$output" | parse_processed)

    if [ "$processed" = "0" ]; then
        log "Sweep complete — nothing left to process"
        report_progress
        exit 0
    fi

    log "Chunk processed $processed entries"

    # Post-flight Qdrant probe
    log "Post-flight Qdrant probe..."
    if ! probe_qdrant; then
        log "ABORT: Qdrant unreachable after iteration $iteration"
        log "Last chunk may have filesystem/Qdrant drift — verify with: recon.py pipeline sweep --verify"
        report_progress
        exit 3
    fi
    log "Qdrant still healthy, continuing..."
    report_progress
    echo
done
