# Domain Categorization Migration Runbook

Step-by-step procedure to deploy the PeerTube domain categorization feature.

## Prerequisites

- Feature branch `feature/peertube-domain-categorization` merged to master (or checked out)
- SSH access to recon-vm (192.168.1.130) and CT 110 (192.168.1.170)
- PeerTube admin credentials (`root` / password in `.env`)

## Pre-Deploy Backups

These backups MUST be completed before any state-changing step.

### 1. Snapshot RECON database

```bash
ssh zvx@192.168.1.130
cp /opt/recon/data/recon.db "/opt/recon/data/recon.db.pre-domain-feature.$(date +%Y%m%d_%H%M%S).bak"
ls -la /opt/recon/data/recon.db.pre-domain-feature.*.bak  # Confirm
```

### 2. Snapshot PeerTube PostgreSQL

```bash
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres pg_dump peertube_prod' > "/tmp/peertube_prod.pre-domain-feature.$(date +%Y%m%d_%H%M%S).sql"
ls -la /tmp/peertube_prod.pre-domain-feature.*.sql  # Confirm non-zero
```

### 3. Verify off-site concept backup

```bash
# Check last rsync to Contabo
ssh zvx@192.168.1.130 'ls -la /opt/recon/data/concepts/ | tail -5'
ssh root@100.64.0.1 'ls -la /opt/backups/recon/concepts/ | tail -5'
# Confirm timestamps match within 6 hours
```

### 4. Confirm RECON service state

```bash
ssh zvx@192.168.1.130 'sudo systemctl status recon --no-pager'
# Note: do NOT restart until Step 3. If currently running, confirm no active
# enrichment/embedding workers before proceeding.
```

---

## Step 1: Deploy PeerTube Plugin to CT 110

```bash
# From recon-vm, copy plugin to CT 110
ssh zvx@192.168.1.130
cd /opt/recon/peertube-plugin/
scp -r peertube-plugin-recon-domains root@192.168.1.241:'pct exec 110 -- mkdir -p /var/www/peertube/storage/plugins/node_modules/peertube-plugin-recon-domains'

# Or via the Proxmox host:
ssh root@192.168.1.243  # media host
pct exec 110 -- bash -c 'mkdir -p /var/www/peertube/storage/plugins/node_modules/peertube-plugin-recon-domains'
# Copy files into the container (scp from recon-vm or use pct push)
```

Alternative: Install via PeerTube admin UI (Admin > Plugins > Install).

```bash
# Restart PeerTube to register plugin
ssh root@192.168.1.243 'pct exec 110 -- systemctl restart peertube'
```

**STOP.** Check PeerTube logs for plugin registration errors:

```bash
ssh root@192.168.1.243 'pct exec 110 -- journalctl -u peertube --since=-5min' | grep -i plugin
```

If any errors reference `peertube-plugin-recon-domains`, do NOT proceed. Diagnose
and fix the plugin before continuing. See Rollback: "Plugin install fails" below.

## Step 2: Verify Plugin

```bash
# From recon-vm
curl -s http://192.168.1.170:9000/api/v1/videos/categories -H "Host: stream.echo6.co" | python3 -m json.tool | grep -E '"1[0-1][0-9]"'
```

Should show all 18 categories (IDs 100-117). If any are missing, do NOT proceed.

Run the parity test:
```bash
cd /opt/recon && source venv/bin/activate
python3 tests/test_constants_parity.py
```

## Step 3: Apply Schema Migration

**Requires RECON restart (ask user first).**

```bash
sudo systemctl restart recon
```

The migration runs automatically on startup via `StatusDB._init_db()`. Verify:

```bash
cd /opt/recon && source venv/bin/activate
python3 -c "
from lib.status import StatusDB
db = StatusDB()
conn = db._get_conn()
cols = [r[1] for r in conn.execute('PRAGMA table_info(documents)').fetchall()]
for c in ['recon_domain', 'recon_domain_status', 'recon_domain_assigned_at', 'peertube_category_pushed_at']:
    assert c in cols, f'Missing: {c}'
    print(f'  {c}: OK')

# Verify index exists
indexes = [r[1] for r in conn.execute('PRAGMA index_list(documents)').fetchall()]
assert 'idx_documents_recon_domain_status' in indexes, 'Missing index'
print('  idx_documents_recon_domain_status: OK')

# Verify no columns were dropped
expected_existing = ['hash', 'status', 'filename', 'discovered_at']
for c in expected_existing:
    assert c in cols, f'ALERT: existing column {c} is missing!'
print('Migration verified — all columns present, no existing columns dropped')
"
```

## Step 4: Run Backfill

```bash
cd /opt/recon && source venv/bin/activate

# Dry run first
python3 recon.py assign-categories --backfill --dry-run
```

**STOP.** Verify dry-run output distribution roughly matches investigation benchmarks:
- ~94.8% `assigned` (clear winners)
- ~5.2% `tied_pass_1` (ties)
- ~19.5% `needs_reprocess` (missing/legacy concepts)

If the distribution deviates more than 5 percentage points from these benchmarks,
halt and investigate. Do not proceed until the deviation is explained.

```bash
# Execute pass 1
python3 recon.py assign-categories --backfill
```

**STOP.** Spot-check 20 random assigned documents:

```bash
python3 -c "
from lib.status import StatusDB
db = StatusDB()
rows = db._get_conn().execute(
    \"SELECT d.hash, d.recon_domain FROM documents d WHERE d.recon_domain_status = 'assigned' ORDER BY RANDOM() LIMIT 20\"
).fetchall()
for r in rows:
    print(r['hash'][:12], r['recon_domain'])
"
```

For each, visually verify against concept files: `ls data/concepts/{hash}/` and
spot-check one `window_*.json` to confirm the assigned domain is plausible.
Halt if any are wildly wrong. See Rollback: "Clear wrong backfill assignments" below.

```bash
# Run tiebreaker pass
python3 recon.py assign-categories --tiebreaker-pass
```

**STOP.** Verify tiebreaker results:

```bash
python3 -c "
from lib.status import StatusDB
db = StatusDB()
c = db.get_domain_status_counts()
print('Status breakdown:', c)
print()
print('tied_pass_2 (resolved):', c.get('tied_pass_2', 0))
print('tied_manual (needs review):', c.get('tied_manual', 0))
"
```

Spot-check 5 `tied_pass_2` items — verify the resolved domain is plausible given
the channel's other content.

```bash
# Check overall status
python3 recon.py assign-categories
```

## Step 5: Push to PeerTube

Push in stages. Do NOT push all at once.

```bash
# Dry run: confirm count
python3 recon.py assign-categories --push-pending --dry-run

# Stage 1: push 100 items
python3 recon.py assign-categories --push-pending --limit 100
```

**STOP.** Verify in PeerTube UI (stream.echo6.co admin, or via API) that 100 videos
now show RECON domain categories. Spot-check 5 videos.

```bash
# Verify via API: pick a random pushed video
python3 -c "
from lib.status import StatusDB
db = StatusDB()
row = db._get_conn().execute(
    \"SELECT d.recon_domain, c.path FROM documents d LEFT JOIN catalogue c ON d.hash = c.hash WHERE d.peertube_category_pushed_at IS NOT NULL ORDER BY RANDOM() LIMIT 1\"
).fetchone()
if row:
    uuid = row['path'].rsplit('/w/', 1)[-1] if row['path'] and '/w/' in row['path'] else '?'
    print(f'Domain: {row[\"recon_domain\"]}  UUID: {uuid}')
    print(f'Check: curl -s http://192.168.1.170:9000/api/v1/videos/{uuid} -H \"Host: stream.echo6.co\" | python3 -m json.tool | grep category')
"
```

```bash
# Stage 2: push 1000 items
python3 recon.py assign-categories --push-pending --limit 1000
```

**STOP.** Verify via PeerTube database:

```bash
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres psql -d peertube_prod -c "SELECT category, count(*) FROM video WHERE category >= 100 GROUP BY category ORDER BY count DESC"'
```

```bash
# Stage 3: push remaining
python3 recon.py assign-categories --push-pending
```

## Step 6: Verify

```bash
# Check PeerTube database directly
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres psql -d peertube_prod -c "SELECT category, count(*) FROM video WHERE category >= 100 GROUP BY category ORDER BY count DESC"'

# Check uncategorized
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres psql -d peertube_prod -c "SELECT count(*) FROM video WHERE category IS NULL"'

# Check RECON status
python3 recon.py assign-categories
```

## Step 7: Reprocess Missing Items (SEPARATE POST-DEPLOY OPERATION)

**WARNING:** This step deletes concept directories. It is the only destructive
operation in the entire feature. Run it separately from the initial deploy,
after all other steps are verified and stable.

```bash
# Dry run first — review what would be deleted
python3 recon.py assign-categories --reprocess-missing --dry-run --limit 10
```

**STOP.** Review output. Verify concept dirs listed are genuinely stale (legacy
domains only, or missing concept files). The dry-run reports file counts for
each directory that would be deleted.

```bash
# Small batch
python3 recon.py assign-categories --reprocess-missing --limit 10
```

**STOP.** Verify: check that 10 items re-entered the pipeline.

```bash
python3 recon.py status  # queued count should increase by ~10
```

Wait for pipeline to process them. Verify domain assignment on completion:

```bash
# Check these specific items got re-enriched and assigned
python3 recon.py assign-categories
```

```bash
# Scale up
python3 recon.py assign-categories --reprocess-missing --limit 100

# Then unbounded
python3 recon.py assign-categories --reprocess-missing
```

**Note on interrupts:** If `--reprocess-missing` is interrupted mid-run, re-running
it is safe. Any documents stranded at `status='catalogued'` without being re-queued
can be recovered with `recon.py queue --source stream.echo6.co`.

## Step 8: Dashboard Review

Navigate to `https://recon.echo6.co/peertube/review` to review `tied_manual` items.
Each row shows the video, channel, tied domains, and concept counts. Select the
correct domain and click Assign.

---

## Rollback Procedures

### Plugin install fails or breaks PeerTube

```bash
# Disable plugin without uninstalling
ssh root@192.168.1.243 'pct exec 110 -- bash -c "
  mv /var/www/peertube/storage/plugins/node_modules/peertube-plugin-recon-domains \
     /var/www/peertube/storage/plugins/node_modules/peertube-plugin-recon-domains.disabled
  systemctl restart peertube
"'

# Verify PeerTube is healthy
curl -s http://192.168.1.170:9000/api/v1/videos/categories -H "Host: stream.echo6.co" | python3 -m json.tool | head

# To fully remove: use PeerTube admin UI → Plugins → Uninstall
```

### Schema migration revert (drop new columns)

Only needed if the columns cause problems. The columns are nullable and have no
constraints, so they should be inert.

```bash
ssh zvx@192.168.1.130 'cd /opt/recon && source venv/bin/activate && python3 -c "
import sqlite3
conn = sqlite3.connect(\"data/recon.db\")
for col in [\"recon_domain\", \"recon_domain_status\", \"recon_domain_assigned_at\", \"peertube_category_pushed_at\"]:
    try:
        conn.execute(f\"ALTER TABLE documents DROP COLUMN {col}\")
        print(f\"Dropped: {col}\")
    except Exception as e:
        print(f\"Skip {col}: {e}\")
conn.execute(\"DROP INDEX IF EXISTS idx_documents_recon_domain_status\")
conn.commit()
print(\"Index dropped\")
"'
```

Note: SQLite ALTER TABLE DROP COLUMN requires SQLite 3.35.0+ (2021-03-12).
Ubuntu 24.04 ships 3.45.1 — this is fine.

### Clear wrong backfill assignments (selective or full)

```bash
cd /opt/recon && source venv/bin/activate

# Clear ALL domain assignments
python3 -c "
from lib.status import StatusDB
db = StatusDB()
conn = db._get_conn()
conn.execute('''UPDATE documents SET
    recon_domain = NULL, recon_domain_status = NULL,
    recon_domain_assigned_at = NULL, peertube_category_pushed_at = NULL''')
conn.commit()
print('Cleared all domain assignments')
"

# Clear only tiebreaker results (reset to tied_pass_1 for re-run)
python3 -c "
from lib.status import StatusDB
db = StatusDB()
conn = db._get_conn()
conn.execute('''UPDATE documents SET
    recon_domain = NULL, recon_domain_status = 'tied_pass_1',
    recon_domain_assigned_at = NULL
WHERE recon_domain_status IN ('tied_pass_2', 'tied_manual')''')
conn.commit()
"
```

### Clear wrong PeerTube categories

```bash
# Reset ALL RECON categories (100+) to NULL in PeerTube
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres psql -d peertube_prod \
  -c "UPDATE video SET category = NULL WHERE category >= 100"'

# Verify
ssh root@192.168.1.243 'pct exec 110 -- sudo -u postgres psql -d peertube_prod \
  -c "SELECT count(*) FROM video WHERE category >= 100"'
# Should return 0

# Also clear RECON pushed timestamps so --push-pending can retry
cd /opt/recon && source venv/bin/activate
python3 -c "
from lib.status import StatusDB
db = StatusDB()
conn = db._get_conn()
conn.execute('UPDATE documents SET peertube_category_pushed_at = NULL WHERE peertube_category_pushed_at IS NOT NULL')
conn.commit()
print('Cleared push timestamps')
"
```

### Restore concepts after failed --reprocess-missing

```bash
# Concept backups are on Contabo at /opt/backups/recon/concepts/
# Identify which hashes were deleted (check RECON logs)
ssh zvx@192.168.1.130 'grep "Deleting concept dir" /opt/recon/logs/recon.log | tail -20'

# Restore specific hash from Contabo
HASH=<hash_from_log>
ssh root@100.64.0.1 "tar -cf - -C /opt/backups/recon/concepts/ $HASH" | \
  ssh zvx@192.168.1.130 "tar -xf - -C /opt/recon/data/concepts/"

# Restore ALL concepts (nuclear option)
ssh root@100.64.0.1 'rsync -av /opt/backups/recon/concepts/ zvx@192.168.1.130:/opt/recon/data/concepts/'
```

### Fully remove feature

1. Uninstall plugin from PeerTube admin UI
2. Restart PeerTube
3. Revert RECON code changes (`git checkout master`)
4. Restart RECON
5. Drop schema columns (see above)
6. Reset PeerTube categories (see above)
