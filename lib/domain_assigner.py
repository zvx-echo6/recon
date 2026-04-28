"""
RECON Domain Assigner

Computes per-video domain assignments from Qdrant vector payloads.
Two functions, two execution modes:

  compute_assignment() — pass 1, inline from post-embed hook
  run_tiebreaker_pass() — batch, resolves ties via channel concept scan

Data source: Qdrant `domain` payload field on concept vectors.
Previously read on-disk concept JSON files; migrated to Qdrant as
single source of truth (2026-04-28).

Status values written to documents.recon_domain_status:
  assigned       — clear winner from pass 1 concept count
  tied_pass_1    — concept tie, awaiting channel tiebreaker
  tied_pass_2    — resolved by channel tiebreaker
  tied_manual    — needs human review (dashboard)
  no_concepts    — terminal, zero concept vectors in Qdrant
  needs_reprocess — transient failure (Qdrant error, etc.)
  manual_assigned — human override from dashboard
"""
from collections import Counter

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

from .recon_domains import VALID_DOMAINS, DOMAIN_CATEGORY_MAP, MEGA_CHANNEL_SKIP_LIST
from .utils import setup_logging

logger = setup_logging('recon.domain_assigner')



def _get_qdrant_client(config):
    """Create a QdrantClient from RECON config.

    Callers should create one client and pass it through rather than
    calling this repeatedly.
    """
    logger.debug("Creating new QdrantClient (caller did not pass one)")
    return QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=60
    )


def _count_domains_from_qdrant(qdrant, collection, doc_hash):
    """Count valid domain occurrences for a single document from Qdrant.

    Scrolls all points matching doc_hash and counts domain values.

    Args:
        qdrant: QdrantClient instance
        collection: Qdrant collection name
        doc_hash: Document hash to query

    Returns:
        Counter of {domain_name: count} for valid domains.
        Empty Counter if no points found (never None).
    """
    domain_counter = Counter()
    offset = None

    while True:
        results, next_offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="doc_hash", match=MatchValue(value=doc_hash))
            ]),
            with_payload=["domain"],
            with_vectors=False,
            limit=200,
            offset=offset,
        )

        for point in results:
            dom = point.payload.get('domain')
            if isinstance(dom, str) and dom in VALID_DOMAINS:
                domain_counter[dom] += 1
            elif isinstance(dom, list):
                for d in dom:
                    if isinstance(d, str) and d in VALID_DOMAINS:
                        domain_counter[d] += 1

        if next_offset is None:
            break
        offset = next_offset

    return domain_counter


def _count_domains_from_qdrant_batch(qdrant, collection, doc_hashes):
    """Count valid domain occurrences across multiple documents from Qdrant.

    Single scroll with MatchAny filter, with offset pagination for large
    result sets.

    Args:
        qdrant: QdrantClient instance
        collection: Qdrant collection name
        doc_hashes: List of document hashes to query

    Returns:
        Counter of {domain_name: count} aggregated across all matching points.
    """
    if not doc_hashes:
        return Counter()

    domain_counter = Counter()
    offset = None

    while True:
        results, next_offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="doc_hash", match=MatchAny(any=doc_hashes))
            ]),
            with_payload=["domain"],
            with_vectors=False,
            limit=10000,
            offset=offset,
        )

        for point in results:
            dom = point.payload.get('domain')
            if isinstance(dom, str) and dom in VALID_DOMAINS:
                domain_counter[dom] += 1
            elif isinstance(dom, list):
                for d in dom:
                    if isinstance(d, str) and d in VALID_DOMAINS:
                        domain_counter[d] += 1

        if next_offset is None:
            break
        offset = next_offset

    return domain_counter


def compute_assignment(file_hash, db, config, qdrant=None):
    """Compute domain assignment for a single document (pass 1).

    Counts domain occurrences across all concept vectors in Qdrant.
    If a single domain wins, assigns it. If tied, defers to batch
    tiebreaker.

    Args:
        file_hash: Document hash
        db: StatusDB instance
        config: RECON config dict
        qdrant: Optional QdrantClient (created if not provided)

    Returns:
        (domain, status) tuple where domain is a string or None,
        and status is one of: 'assigned', 'tied_pass_1', 'no_concepts',
        'needs_reprocess'
    """
    owns_client = False
    if qdrant is None:
        qdrant = _get_qdrant_client(config)
        owns_client = True

    collection = config['vector_db']['collection']

    try:
        domain_counter = _count_domains_from_qdrant(qdrant, collection, file_hash)
    except Exception as e:
        logger.warning(f"Qdrant query failed for {file_hash[:12]}: {e}")
        return (None, 'needs_reprocess')

    if len(domain_counter) == 0:
        return (None, 'no_concepts')

    top = domain_counter.most_common(2)
    top_domain = top[0][0]
    top_count = top[0][1]

    if len(top) == 1 or top[1][1] < top_count:
        return (top_domain, 'assigned')

    # Tie — defer to tiebreaker pass
    return (None, 'tied_pass_1')


def _get_tied_domains(qdrant, collection, file_hash):
    """Get the set of domains tied for first place in a document's concepts."""
    domain_counter = _count_domains_from_qdrant(qdrant, collection, file_hash)
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




def run_tiebreaker_pass(db, config, qdrant=None):
    """Resolve tied domain assignments using channel-level Qdrant analysis.

    Processes all documents where recon_domain_status = 'tied_pass_1'.

    For each tied document, queries Qdrant for domain counts from all
    other videos in the same channel and picks the tied domain with the
    highest channel-wide count.

    Channels in MEGA_CHANNEL_SKIP_LIST (known non-topical catch-alls) skip
    tiebreaking and go straight to 'tied_manual' for dashboard review.

    Args:
        db: StatusDB instance
        config: RECON config dict
        qdrant: Optional QdrantClient (created if not provided)

    Returns:
        Dict with counts: resolved, manual, skipped, errors
    """
    owns_client = False
    if qdrant is None:
        qdrant = _get_qdrant_client(config)
        owns_client = True

    collection = config['vector_db']['collection']
    tied_items = db.get_items_by_domain_status('tied_pass_1')

    stats = {'resolved': 0, 'manual': 0, 'skipped': 0, 'errors': 0, 'total': len(tied_items)}
    logger.info(f"Tiebreaker pass: {len(tied_items)} items to resolve")

    for item in tied_items:
        file_hash = item['hash']
        channel = item.get('category', '')

        try:
            tied_domains = _get_tied_domains(qdrant, collection, file_hash)
            if not tied_domains:
                db.set_domain_assignment(file_hash, None, 'no_concepts')
                stats['skipped'] += 1
                continue

            if len(tied_domains) == 1:
                # No longer tied (possibly re-enriched since pass 1)
                db.set_domain_assignment(file_hash, tied_domains[0], 'assigned')
                stats['resolved'] += 1
                continue

            # Skip-list check: known non-topical catch-all channels
            if channel in MEGA_CHANNEL_SKIP_LIST:
                fallback = sorted(tied_domains)[0]
                db.set_domain_assignment(file_hash, fallback, 'tied_manual')
                stats['manual'] += 1
                logger.debug(f"  {file_hash[:12]}: skip-list channel '{channel}' → tied_manual")
                continue

            # Channel tiebreaker: count domains across all other videos in channel
            other_hashes = _channel_video_hashes(db, channel, exclude_hash=file_hash)
            channel_domain_counts = _count_domains_from_qdrant_batch(
                qdrant, collection, other_hashes
            )

            # Among tied domains only, pick highest channel-wide count
            best_domain = None
            best_count = -1
            for dom in tied_domains:
                c = channel_domain_counts.get(dom, 0)
                if c > best_count:
                    best_count = c
                    best_domain = dom

            # Check if channel tiebreaker resolved it
            tied_at_channel = [d for d in tied_domains
                               if channel_domain_counts.get(d, 0) == best_count]

            if len(tied_at_channel) == 1:
                db.set_domain_assignment(file_hash, best_domain, 'tied_pass_2')
                stats['resolved'] += 1
                logger.debug(f"  {file_hash[:12]}: resolved → {best_domain} (channel tiebreaker)")
                continue

            # Still tied after channel scan — mark for manual review
            fallback = sorted(tied_domains)[0]
            db.set_domain_assignment(file_hash, fallback, 'tied_manual')
            stats['manual'] += 1
            logger.debug(f"  {file_hash[:12]}: still tied after channel scan, → tied_manual")

        except Exception as e:
            logger.warning(f"  Tiebreaker error for {file_hash[:12]}: {e}")
            stats['errors'] += 1

    logger.info(f"Tiebreaker complete: {stats['resolved']} resolved, "
                f"{stats['manual']} manual, {stats['skipped']} skipped, "
                f"{stats['errors']} errors")
    return stats
