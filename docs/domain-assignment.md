# Domain Assignment — Algorithm & Operations Guide

## Overview

RECON's domain assignment feature maps each PeerTube video to one of 18 knowledge domains by analyzing the concept vectors stored in Qdrant. Assignments are pushed to PeerTube as category metadata via a custom plugin.

## Data Source

Domain counts are read from the `domain` payload field on concept vectors in Qdrant (`recon_knowledge_hybrid` collection on cortex:6333). Each concept vector has a `domain` string in its payload, set during enrichment and validated at embed time. This provides 100% coverage for all embedded documents with zero legacy domain residue.

Previously, domain counts were read from on-disk concept JSON files (`data/concepts/{hash}/window_*.json`). This was replaced with Qdrant queries on 2026-04-28 because ~10,000 items had missing or legacy-only concept files on disk while Qdrant had the correct data.

## Algorithm

### Pass 1: Concept Domain Count (inline, per-document)

Runs automatically via post-embed hook when a video completes the pipeline, or in bulk via `--backfill`.

1. Query Qdrant for all points with `doc_hash` matching the document
2. Count `domain` payload occurrences, filtering to `VALID_DOMAINS` only
3. If zero concept vectors → `no_concepts` (terminal)
4. If single top domain → `assigned`
5. If tied → `tied_pass_1` (deferred to tiebreaker)

### Pass 2: Channel Tiebreaker (batch)

Runs via `assign-categories --tiebreaker-pass`.

For each `tied_pass_1` document:

1. Identify the tied domains from Qdrant
2. Look up the document's channel (`catalogue.category`)
3. **Mega-channel rule:** If channel has >500 videos, skip tiebreaking → `tied_manual`
4. Query Qdrant for domain counts across all other videos in the same channel (single batch query with `MatchAny` filter)
5. Among the tied domains only, pick the one with the highest channel-wide concept count
6. If resolved → `tied_pass_2`
7. If still tied → `tied_manual` (alphabetical fallback assigned, flagged for review)

### Mega-Channel Rule

Channels with >500 videos (like the "Transcript" catch-all with ~9,200 videos) are not topically coherent. Scanning their concepts produces meaningless aggregate data. These go straight to `tied_manual` for dashboard review.

## Status Values

| Status | Meaning | Terminal? | Next Action |
|--------|---------|-----------|-------------|
| `assigned` | Clear winner from pass 1 | No | Push to PeerTube |
| `tied_pass_1` | Concept tie, awaiting tiebreaker | No | Run `--tiebreaker-pass` |
| `tied_pass_2` | Resolved by channel tiebreaker | No | Push to PeerTube |
| `tied_manual` | Needs human review | No | Review at `/peertube/review` |
| `no_concepts` | Zero concept vectors in Qdrant | **Yes** | None — typically non-topical content (vlogs, giveaways, announcements) |
| `needs_reprocess` | Transient failure (Qdrant error) | No | Run `--reprocess-missing` |
| `manual_assigned` | Human override from dashboard | No | Already pushed |

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

# Re-queue items with transient failures for full re-processing
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

Items with `no_concepts` or `needs_reprocess` status do NOT appear in the review UI.

## Pipeline Integration

New videos ingested via the PeerTube collector are automatically assigned a domain when they complete the embed stage. The post-embed hook in `embedder.py`:

1. Runs `compute_assignment()` (pass 1 only), reusing the embedder's existing Qdrant client
2. If clear winner: pushes category to PeerTube immediately
3. If tied: marks as `tied_pass_1` for the next tiebreaker batch run
4. If no concepts: marks as `no_concepts` (terminal)
5. On Qdrant error: logs warning and continues — does not block the pipeline

## Source Files

| File | Purpose |
|------|---------|
| `lib/recon_domains.py` | Domain↔Category ID mapping, VALID_DOMAINS |
| `lib/domain_assigner.py` | `compute_assignment()` + `run_tiebreaker_pass()` + Qdrant helpers |
| `lib/peertube_writer.py` | OAuth2 client, `push_category()`, `push_pending()` |
| `lib/embedder.py` | Post-embed hook (passes qdrant client) |
| `lib/status.py` | DB columns + helper methods |
| `lib/api.py` | Dashboard review routes |
| `recon.py` | CLI `assign-categories` command |
