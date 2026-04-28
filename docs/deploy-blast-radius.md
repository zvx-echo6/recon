# Deploy Blast Radius Reference

Quick-reference for operators during deployment of domain categorization.

| Step | What Changes | Worst Case (Partial Failure) | Detection Signal | Rollback | Est. Rollback Time |
|------|-------------|------------------------------|-----------------|----------|-------------------|
| **Plugin install** | PeerTube plugin dir on CT 110 | PeerTube fails to start | `systemctl status peertube` shows failed | Move plugin dir to `.disabled`, restart PeerTube | 2 min |
| **PeerTube restart** | PeerTube service state | PeerTube crash loop | `journalctl -u peertube` shows repeated failures | Disable plugin, restart | 2 min |
| **Schema migration** (RECON restart) | 4 new nullable columns + 1 index in recon.db | Migration SQL error leaves partial columns | Python PRAGMA check fails | DROP COLUMN for each added column | 5 min |
| **--backfill** | `recon_domain` + `recon_domain_status` on ~22K rows | Wrong domain assignments | Spot-check 20 random docs | `UPDATE documents SET recon_domain = NULL, recon_domain_status = NULL ...` | 1 min |
| **--tiebreaker-pass** | ~1,100 rows: `tied_pass_1` to `tied_pass_2`/`tied_manual` | Wrong tiebreaker resolution | Spot-check 5 resolved items | Reset `tied_pass_2`/`tied_manual` back to `tied_pass_1` | 1 min |
| **--push-pending** | PeerTube `video.category` column on ~22K rows | Wrong categories visible to all PeerTube users | PeerTube UI shows wrong labels | `UPDATE video SET category = NULL WHERE category >= 100` + clear push timestamps | 2 min |
| **--reprocess-missing** | **DELETES** concept directories (irreversible locally) | Concepts deleted, re-enrichment fails (Gemini API down, quota hit) | `recon.py status` shows stuck `queued` items, concept dirs missing | Restore from Contabo backup (`rsync`) | 10-60 min depending on count |

## Risk Tiers

- **Low risk (read-only):** `--dry-run` on any command, status display
- **Medium risk (DB-only, reversible):** `--backfill`, `--tiebreaker-pass`, schema migration
- **High risk (external writes):** `--push-pending` (writes to PeerTube, visible to users)
- **Critical risk (destructive):** `--reprocess-missing` (deletes concept files, $130+ Gemini work at risk)
