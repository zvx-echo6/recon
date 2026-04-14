#!/bin/bash
# RECON Pipeline — Skip scan, run extract + enrich in parallel, then embed
# Scan already completed (10,162 catalogued). 6,211 extracted, 3,603 queued.

set -euo pipefail
cd /opt/recon
source venv/bin/activate

LOGDIR="logs"
mkdir -p "$LOGDIR"
TS=$(date +%Y%m%d_%H%M%S)
MAIN_LOG="$LOGDIR/pipeline_${TS}.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$MAIN_LOG"
}

log "=== RECON Pipeline (parallel extract+enrich) ==="
log "Skipping scan (already done). Starting extract + enrich concurrently."

# Reset any stuck docs from previous kill
sqlite3 data/recon.db "UPDATE documents SET status='queued' WHERE status='extracting';"
sqlite3 data/recon.db "UPDATE documents SET status='extracted' WHERE status='enriching';"
sqlite3 data/recon.db "UPDATE documents SET status='enriched' WHERE status='embedding';"

# Status before
log "Before:"
sqlite3 data/recon.db "SELECT status, COUNT(*) FROM documents GROUP BY status;" | while read line; do log "  $line"; done

# Start extract and enrich in parallel
log "--- Starting Extract (4 workers) + Enrich (16 workers) ---"

python3 recon.py extract --workers 4 >> "$LOGDIR/extract_${TS}.log" 2>&1 &
EXTRACT_PID=$!
log "  Extract PID: $EXTRACT_PID"

sleep 3

python3 recon.py enrich --workers 16 >> "$LOGDIR/enrich_${TS}.log" 2>&1 &
ENRICH_PID=$!
log "  Enrich PID: $ENRICH_PID"

# Monitor loop — report progress every 5 minutes
while kill -0 $EXTRACT_PID 2>/dev/null || kill -0 $ENRICH_PID 2>/dev/null; do
    sleep 300
    STATS=$(sqlite3 data/recon.db "SELECT status, COUNT(*) FROM documents GROUP BY status;" | tr '\n' ' ')
    log "  Progress: $STATS"
done

log "  Extract + Enrich finished"

# Second enrich pass (catch docs extracted during first enrich)
REMAINING=$(sqlite3 data/recon.db "SELECT COUNT(*) FROM documents WHERE status='extracted';")
if [ "$REMAINING" -gt 0 ]; then
    log "--- Enrich pass 2: $REMAINING remaining ---"
    python3 recon.py enrich --workers 16 >> "$LOGDIR/enrich_${TS}.log" 2>&1
    log "  Pass 2 complete"
fi

# Embed
log "--- Embed ---"
python3 recon.py embed --workers 4 >> "$LOGDIR/embed_${TS}.log" 2>&1
log "  Embed complete"

log "=== Pipeline Complete ==="
python3 recon.py status 2>&1 | tee -a "$MAIN_LOG"
log "Finished: $(date)"
