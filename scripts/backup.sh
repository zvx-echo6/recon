#!/bin/bash
# RECON Backup Script
# Backs up the precious data: concept JSONs, text extracts, SQLite DB
# Qdrant is NOT backed up — rebuilt from JSONs via `recon rebuild`
# Destination: Contabo VPS (100.64.0.1) via rsync+SSH

set -euo pipefail

RECON_DIR="/opt/recon"
DATA_DIR="$RECON_DIR/data"
LOG_FILE="$RECON_DIR/logs/backup.log"
DATE=$(date +%Y%m%d_%H%M%S)

BACKUP_HOST="root@100.64.0.1"
BACKUP_BASE="/opt/backups/recon"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

mkdir -p "$RECON_DIR/logs"

log "=== RECON Backup Starting ==="

# ── 1. SQLite DB (small, fast, critical) ──
log "Backing up recon.db..."
LOCAL_DB_BACKUP="/tmp/recon_${DATE}.db"
sqlite3 "$DATA_DIR/recon.db" ".backup '$LOCAL_DB_BACKUP'"
rsync -az "$LOCAL_DB_BACKUP" "$BACKUP_HOST:$BACKUP_BASE/recon_${DATE}.db"
rm -f "$LOCAL_DB_BACKUP"
# Keep last 7 daily DB backups on remote
ssh "$BACKUP_HOST" "ls -t $BACKUP_BASE/recon_*.db 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null || true"
log "  recon.db backed up"

# ── 2. Concept JSONs (THE PRECIOUS DATA — $130+ of Gemini work) ──
log "Syncing concept JSONs..."
rsync -az --delete "$DATA_DIR/concepts/" "$BACKUP_HOST:$BACKUP_BASE/concepts/"
CONCEPT_COUNT=$(find "$DATA_DIR/concepts/" -name "*.json" 2>/dev/null | wc -l)
log "  concepts synced ($CONCEPT_COUNT JSON files)"

# ── 3. Text extracts (regenerable but expensive in time) ──
log "Syncing text extracts..."
rsync -az --delete "$DATA_DIR/text/" "$BACKUP_HOST:$BACKUP_BASE/text/"
TEXT_COUNT=$(find "$DATA_DIR/text/" -maxdepth 1 -type d 2>/dev/null | wc -l)
log "  text synced ($((TEXT_COUNT - 1)) document dirs)"

# ── 4. Intel feeds ──
if [ -d "$DATA_DIR/intel" ]; then
    log "Syncing intel feeds..."
    rsync -az --delete "$DATA_DIR/intel/" "$BACKUP_HOST:$BACKUP_BASE/intel/"
    log "  intel synced"
fi

# ── 5. Config files ──
log "Backing up config..."
rsync -az "$RECON_DIR/config.yaml" "$BACKUP_HOST:$BACKUP_BASE/config_${DATE}.yaml"
rsync -az "$RECON_DIR/.env" "$BACKUP_HOST:$BACKUP_BASE/env_${DATE}" 2>/dev/null || true
ssh "$BACKUP_HOST" "ls -t $BACKUP_BASE/config_*.yaml 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true"
ssh "$BACKUP_HOST" "ls -t $BACKUP_BASE/env_* 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true"
log "  config backed up"

# ── Summary ──
BACKUP_SIZE=$(ssh "$BACKUP_HOST" "du -sh $BACKUP_BASE" | cut -f1)
log "=== Backup Complete: $BACKUP_SIZE on Contabo ==="
