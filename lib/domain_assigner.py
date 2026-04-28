"""
RECON Domain Assigner

Computes per-video domain assignments from concept extraction results.
Two functions, two execution modes:

  compute_assignment() — pass 1, inline from post-embed hook
  run_tiebreaker_pass() — batch, resolves ties via channel concept scan

Status values written to documents.recon_domain_status:
  assigned       — clear winner from pass 1 concept count
  tied_pass_1    — concept tie, awaiting channel tiebreaker
  tied_pass_2    — resolved by channel tiebreaker
  tied_manual    — needs human review (dashboard)
  needs_reprocess — missing concepts or only legacy domains
  manual_assigned — human override from dashboard
"""
import json
import os
from collections import Counter

from .recon_domains import VALID_DOMAINS, DOMAIN_CATEGORY_MAP
from .utils import setup_logging

logger = setup_logging('recon.domain_assigner')

# Channels with more than this many videos skip channel tiebreaking entirely
MEGA_CHANNEL_THRESHOLD = 500


def _count_concept_domains(concepts_dir, file_hash):
    """Read concept files and count valid domain occurrences.

    Args:
        concepts_dir: Base concepts directory (e.g. /opt/recon/data/concepts)
        file_hash: Document hash

    Returns:
        Counter of {domain_name: count} for valid domains only,
        or None if no concept directory exists.
    """
    doc_concepts_dir = os.path.join(concepts_dir, file_hash)
    if not os.path.isdir(doc_concepts_dir):
        return None

    domain_counter = Counter()

    for fname in os.listdir(doc_concepts_dir):
        if not fname.startswith('window_') or not fname.endswith('.json'):
            continue
        fpath = os.path.join(doc_concepts_dir, fname)
        try:
            with open(fpath, 'r') as f:
                concepts = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(concepts, list):
            continue

        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            dom = concept.get('domain')
            if isinstance(dom, str) and dom in VALID_DOMAINS:
                domain_counter[dom] += 1
            elif isinstance(dom, list):
                for d in dom:
                    if isinstance(d, str) and d in VALID_DOMAINS:
                        domain_counter[d] += 1

    return domain_counter


def compute_assignment(file_hash, db, config):
    """Compute domain assignment for a single document (pass 1).

    Counts domain occurrences across all concepts. If a single domain
    wins, assigns it. If tied, defers to batch tiebreaker.

    Args:
        file_hash: Document hash
        db: StatusDB instance
        config: RECON config dict

    Returns:
        (domain, status) tuple where domain is a string or None,
        and status is one of: 'assigned', 'tied_pass_1', 'needs_reprocess'
    """
    concepts_dir = config['paths']['concepts']
    domain_counter = _count_concept_domains(concepts_dir, file_hash)

    if domain_counter is None or len(domain_counter) == 0:
        return (None, 'needs_reprocess')

    top = domain_counter.most_common(2)
    top_domain = top[0][0]
    top_count = top[0][1]

    if len(top) == 1 or top[1][1] < top_count:
        return (top_domain, 'assigned')

    # Tie — defer to tiebreaker pass
    return (None, 'tied_pass_1')


def _get_tied_domains(concepts_dir, file_hash):
    """Get the set of domains tied for first place in a document's concepts."""
    domain_counter = _count_concept_domains(concepts_dir, file_hash)
    if not domain_counter:
        return []

    top = domain_counter.most_common()
    if not top:
        return []

    max_count = top[0][1]
    return [dom for dom, cnt in top if cnt == max_count]


def _channel_video_hashes(db, channel_name, exclude_hash=None):
    """Get all document hashes belonging to a PeerTube channel.

    Args:
        db: StatusDB instance
        channel_name: catalogue.category (channel actor name)
        exclude_hash: Hash to exclude (the document being resolved)

    Returns:
        List of document hashes
    """
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT hash FROM catalogue WHERE category = ? AND source = 'stream.echo6.co'",
        (channel_name,)
    ).fetchall()
    hashes = [r['hash'] for r in rows]
    if exclude_hash:
        hashes = [h for h in hashes if h != exclude_hash]
    return hashes


def _channel_video_count(db, channel_name):
    """Count total videos in a channel."""
    conn = db._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM catalogue WHERE category = ? AND source = 'stream.echo6.co'",
        (channel_name,)
    ).fetchone()
    return row['cnt'] if row else 0


def run_tiebreaker_pass(db, config):
    """Resolve tied domain assignments using channel-level concept analysis.

    Processes all documents where recon_domain_status = 'tied_pass_1'.

    Pass 2: For each tied document, reads concept files from all other
    videos in the same channel and picks the tied domain with the highest
    channel-wide count.

    Pass 3 (defensive re-run): Re-reads the same channel concept files a
    second time with identical logic. This catches concept-file changes
    that occurred mid-run (e.g. concurrent enrichment writing new windows).
    In steady state pass 3 produces the same result as pass 2, but under
    concurrent writes it can resolve a tie that pass 2 missed.

    Mega-channels (>500 videos) skip both passes and go straight to
    'tied_manual' for dashboard review.

    Args:
        db: StatusDB instance
        config: RECON config dict

    Returns:
        Dict with counts: resolved, manual, skipped, errors
    """
    concepts_dir = config['paths']['concepts']
    tied_items = db.get_items_by_domain_status('tied_pass_1')

    stats = {'resolved': 0, 'manual': 0, 'skipped': 0, 'errors': 0, 'total': len(tied_items)}
    logger.info(f"Tiebreaker pass: {len(tied_items)} items to resolve")

    # Cache channel sizes to avoid repeated queries
    channel_size_cache = {}

    for item in tied_items:
        file_hash = item['hash']
        channel = item.get('category', '')

        try:
            tied_domains = _get_tied_domains(concepts_dir, file_hash)
            if not tied_domains:
                db.set_domain_assignment(file_hash, None, 'needs_reprocess')
                stats['skipped'] += 1
                continue

            if len(tied_domains) == 1:
                # No longer tied (possibly re-enriched since pass 1)
                db.set_domain_assignment(file_hash, tied_domains[0], 'assigned')
                stats['resolved'] += 1
                continue

            # Check mega-channel rule
            if channel not in channel_size_cache:
                channel_size_cache[channel] = _channel_video_count(db, channel)

            if channel_size_cache[channel] > MEGA_CHANNEL_THRESHOLD:
                fallback = sorted(tied_domains)[0]
                db.set_domain_assignment(file_hash, fallback, 'tied_manual')
                stats['manual'] += 1
                logger.debug(f"  {file_hash[:12]}: mega-channel '{channel}' "
                             f"({channel_size_cache[channel]} videos), → tied_manual")
                continue

            # Channel tiebreaker: count domains across all other videos in channel
            other_hashes = _channel_video_hashes(db, channel, exclude_hash=file_hash)
            channel_domain_counts = Counter()

            for other_hash in other_hashes:
                other_counts = _count_concept_domains(concepts_dir, other_hash)
                if other_counts:
                    channel_domain_counts.update(other_counts)

            # Among tied domains only, pick highest channel-wide count
            best_domain = None
            best_count = -1
            for dom in tied_domains:
                c = channel_domain_counts.get(dom, 0)
                if c > best_count:
                    best_count = c
                    best_domain = dom

            # Pass 2: check if channel tiebreaker resolved it
            tied_at_channel = [d for d in tied_domains
                               if channel_domain_counts.get(d, 0) == best_count]

            if len(tied_at_channel) == 1:
                db.set_domain_assignment(file_hash, best_domain, 'tied_pass_2')
                stats['resolved'] += 1
                logger.debug(f"  {file_hash[:12]}: resolved → {best_domain} (pass 2 channel tiebreaker)")
                continue

            # Pass 3: defensive re-run — re-count channel concepts to catch
            # concept-file changes that occurred mid-run. Identical logic to
            # pass 2; resolves races where files were written between the
            # two reads.
            channel_domain_counts_p3 = Counter()
            for other_hash in other_hashes:
                other_counts = _count_concept_domains(concepts_dir, other_hash)
                if other_counts:
                    channel_domain_counts_p3.update(other_counts)

            best_domain_p3 = None
            best_count_p3 = -1
            for dom in tied_domains:
                c = channel_domain_counts_p3.get(dom, 0)
                if c > best_count_p3:
                    best_count_p3 = c
                    best_domain_p3 = dom

            tied_at_p3 = [d for d in tied_domains
                          if channel_domain_counts_p3.get(d, 0) == best_count_p3]

            if len(tied_at_p3) == 1:
                db.set_domain_assignment(file_hash, best_domain_p3, 'tied_pass_2')
                stats['resolved'] += 1
                logger.debug(f"  {file_hash[:12]}: resolved → {best_domain_p3} (pass 3 defensive re-run)")
                continue

            # Still tied after pass 3 — mark for manual review
            fallback = sorted(tied_domains)[0]
            db.set_domain_assignment(file_hash, fallback, 'tied_manual')
            stats['manual'] += 1
            logger.debug(f"  {file_hash[:12]}: still tied after pass 3, → tied_manual")

        except Exception as e:
            logger.warning(f"  Tiebreaker error for {file_hash[:12]}: {e}")
            stats['errors'] += 1

    logger.info(f"Tiebreaker complete: {stats['resolved']} resolved, "
                f"{stats['manual']} manual, {stats['skipped']} skipped, "
                f"{stats['errors']} errors")
    return stats
