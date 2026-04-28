# Domain Assignment — Algorithm & Operations Guide

## Overview

RECON's domain assignment feature maps each PeerTube video to one of 18 knowledge domains by analyzing the concepts extracted from its transcript. Assignments are pushed to PeerTube as category metadata via a custom plugin.

## Algorithm

### Pass 1: Concept Domain Count (inline, per-document)

Runs automatically via post-embed hook when a video completes the pipeline, or in bulk via `--backfill`.

1. Read all `data/concepts/{hash}/window_*.json` files
2. Count domain occurrences across all concepts, filtering to `VALID_DOMAINS` only (skips legacy domains)
3. If no valid concepts → `needs_reprocess`
4. If single top domain → `assigned`
5. If tied → `tied_pass_1` (deferred to tiebreaker)

### Pass 2: Channel Tiebreaker (batch)

Runs via `assign-categories --tiebreaker-pass`.

For each `tied_pass_1` document:

1. Identify the tied domains
2. Look up the document's channel (`catalogue.category`)
3. **Mega-channel rule:** If channel has >500 videos, skip tiebreaking → `tied_manual`
4. Read concept files for all other videos in the same channel
5. Among the tied domains only, pick the one with the highest channel-wide concept count
6. If resolved → `tied_pass_2`
7. If still tied → proceed to pass 3

### Pass 3: Defensive Re-Run

If pass 2 does not resolve the tie, re-read the same channel concept files and re-run identical counting logic. This catches concept-file changes that occurred mid-run (e.g. concurrent enrichment writing new windows during the batch). In steady state, pass 3 produces the same result as pass 2, but under concurrent writes it can resolve a tie that pass 2 missed.

- If resolved → `tied_pass_2` (same status — the column tracks "channel scan resolved it")
- If still tied → `tied_manual` (alphabetical fallback assigned, flagged for review)

### Mega-Channel Rule

Channels with >500 videos (like the "Transcript" catch-all with ~9,200 videos) are not topically coherent. Scanning their concepts produces meaningless aggregate data. These go straight to `tied_manual` for dashboard review.

## Status Values

| Status | Meaning | Next Action |
|--------|---------|-------------|
| `assigned` | Clear winner from pass 1 | Push to PeerTube |
| `tied_pass_1` | Concept tie, awaiting tiebreaker | Run `--tiebreaker-pass` |
| `tied_pass_2` | Resolved by channel tiebreaker | Push to PeerTube |
| `tied_manual` | Needs human review | Review at `/peertube/review` |
| `needs_reprocess` | Missing concepts or only legacy domains | Run `--reprocess-missing` |
| `manual_assigned` | Human override from dashboard | Already pushed |

**"Categorized" filter** = `{'assigned', 'tied_pass_2', 'manual_assigned'}`

## CLI Commands

```bash
cd /opt/recon && source venv/bin/activate

# Show current assignment status
python3 recon.py assign-categories

# Pass 1: backfill all unassigned complete stream documents
python3 recon.py assign-categories --backfill --dry-run
python3 recon.py assign-categories --backfill

# Pass 2: resolve ties via channel analysis
python3 recon.py assign-categories --tiebreaker-pass

# Push all assigned-but-unpushed categories to PeerTube API
python3 recon.py assign-categories --push-pending

# Re-queue items with missing/legacy concepts
python3 recon.py assign-categories --reprocess-missing

# Limit processing count
python3 recon.py assign-categories --backfill --limit 100
```

## Dashboard Review

The review UI at `recon.echo6.co/peertube/review` shows only `tied_manual` items. Each row displays:
- Video title and channel
- Top concept domains with counts
- Dropdown to select the correct domain
- Assign button (pushes to PeerTube immediately)

Items with `needs_reprocess` status do NOT appear in the review UI — they are handled exclusively via the CLI `--reprocess-missing` command.

## Pipeline Integration

New videos ingested via the PeerTube collector are automatically assigned a domain when they complete the embed stage. The post-embed hook in `embedder.py`:

1. Runs `compute_assignment()` (pass 1 only)
2. If clear winner: pushes category to PeerTube immediately
3. If tied: marks as `tied_pass_1` for the next tiebreaker batch run
4. On error: logs warning and continues — does not block the pipeline

## Source Files

| File | Purpose |
|------|---------|
| `lib/recon_domains.py` | Domain↔Category ID mapping, VALID_DOMAINS |
| `lib/domain_assigner.py` | `compute_assignment()` + `run_tiebreaker_pass()` |
| `lib/peertube_writer.py` | OAuth2 client, `push_category()`, `push_pending()` |
| `lib/embedder.py` | Post-embed hook |
| `lib/status.py` | DB columns + helper methods |
| `lib/api.py` | Dashboard review routes |
| `recon.py` | CLI `assign-categories` command |
