#!/usr/bin/env python3
"""
RECON CLI — Main entry point.

Subcommands: scan, queue, extract, enrich, embed, run, search, upload,
ingest-url, ingest-peertube, organize, status, catalogue, failures, validate, rebuild, serve, ingest.

Usage: cd /opt/recon && source venv/bin/activate && python3 recon.py <command>
"""

import argparse
import json
import os
import signal
import shutil
import sys
import threading
import time

from lib.utils import get_config, content_hash, derive_source_and_category, generate_download_url, setup_logging
from lib.status import StatusDB

logger = setup_logging('recon.cli')


# ── Standalone functions (used by both CLI and service) ──────────────────

def scan_library(path=None):
    """Scan library tree and catalogue new PDFs. Returns count of PDFs catalogued."""
    config = get_config()
    db = StatusDB()
    library_root = config['library_root']
    scan_root = path or library_root

    if not os.path.exists(scan_root):
        logger.warning(f"Scan path not found: {scan_root}")
        return 0

    count = 0
    for root, dirs, files in os.walk(scan_root):
        dirs[:] = [d for d in dirs if not d.startswith('_')]  # Skip staging dirs (_acquired, _ingest)
        for fname in files:
            if not fname.lower().endswith('.pdf'):
                continue
            filepath = os.path.join(root, fname)
            try:
                fhash = content_hash(filepath)
                size = os.path.getsize(filepath)
                source, category = derive_source_and_category(filepath, library_root)
                db.add_to_catalogue(fhash, fname, filepath, size, source, category)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to catalogue {filepath}: {e}")

    return count


def queue_all():
    """Queue all unprocessed catalogue items. Returns count queued."""
    db = StatusDB()
    items = db.get_catalogued()
    queued = 0
    for item in items:
        if db.queue_document(item['hash']):
            queued += 1
    return queued




def sync_qdrant_paths():
    """Sync updated file paths to Qdrant vector payloads.

    After scan_library() upserts catalogue paths, any path changes are flagged
    with path_updated_at. This function propagates those path changes to the
    Qdrant download_url payload so citations stay valid.

    Returns count of Qdrant points updated.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, MatchValue, Filter

    config = get_config()
    db = StatusDB()

    updates = db.get_path_updates()
    if not updates:
        return 0

    library_root = config['library_root']
    qdrant = QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=60
    )
    collection = config['vector_db']['collection']

    synced = 0
    for row in updates:
        doc_hash = row['hash']
        new_path = row['path']
        new_filename = row['filename']

        # Update documents table path
        db.sync_document_path(doc_hash, new_path, new_filename)

        # Build new download URL
        new_url = generate_download_url(new_path, library_root)

        # Find all Qdrant points for this document
        try:
            hits = qdrant.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="doc_hash", match=MatchValue(value=doc_hash))
                ]),
                limit=1000,
                with_payload=False,
            )
            point_ids = [p.id for p in hits[0]]

            if point_ids:
                qdrant.set_payload(
                    collection_name=collection,
                    payload={"download_url": new_url, "filename": new_filename},
                    points=point_ids,
                )
                logger.info(f"  Synced {len(point_ids)} vectors for {new_filename} -> {new_url}")

            db.clear_path_update(doc_hash)
            synced += 1
        except Exception as e:
            logger.warning(f"  Failed to sync Qdrant paths for {doc_hash}: {e}")

    if synced:
        logger.info(f"[scanner] Synced {synced} document paths to Qdrant")
    return synced


# ── CLI command wrappers ─────────────────────────────────────────────────

def cmd_scan(args):
    count = scan_library(path=args.path)
    synced = sync_qdrant_paths()
    print(f"Scanned {count} PDFs into catalogue, {synced} paths synced to Qdrant")
    return 0


def cmd_queue(args):
    db = StatusDB()

    if args.hash:
        if db.queue_document(args.hash):
            print(f"Queued: {args.hash}")
        else:
            print(f"Not found in catalogue: {args.hash}")
        return 0

    items = db.get_catalogued(
        source=args.source,
        category=args.category,
        limit=args.limit
    )

    if not items:
        print("No catalogued items match criteria")
        return 0

    queued = 0
    for item in items:
        if db.queue_document(item['hash']):
            queued += 1

    print(f"Queued {queued} documents for processing")
    return 0


def cmd_extract(args):
    from lib.extractor import run_extraction
    success = run_extraction(workers=args.workers)
    print(f"Extraction complete: {success} documents processed")
    return 0


def cmd_enrich(args):
    from lib.enricher import run_enrichment
    success = run_enrichment(workers=args.workers, limit=args.limit)
    print(f"Enrichment complete: {success} documents processed")
    return 0


def cmd_embed(args):
    from lib.embedder import run_embedding
    success = run_embedding(workers=args.workers, limit=args.limit)
    print(f"Embedding complete: {success} documents processed")
    return 0


def cmd_run(args):
    """Run all pipeline stages concurrently.

    Each stage runs in its own thread, polling for work independently:
      - Extract: queued -> extracted
      - Enrich:  extracted -> enriched
      - Embed:   enriched -> complete

    Documents flow through continuously without waiting for a stage to finish.
    """
    from lib.extractor import run_extraction
    from lib.enricher import run_enrichment
    from lib.embedder import run_embedding

    config = get_config()
    extract_workers = args.workers
    enrich_workers = args.enrich_workers or config.get('processing', {}).get('enrich_workers', 16)
    embed_workers = args.workers
    poll_interval = 30

    stop_event = threading.Event()
    totals = {'extract': 0, 'enrich': 0, 'embed': 0}

    def _doc_counts():
        db = StatusDB()
        raw = db.get_status_counts()
        return raw.get('documents', {})

    def _upstream_done(stage, counts):
        """Check if all upstream stages are finished feeding this stage."""
        if stage == 'extract':
            return counts.get('queued', 0) == 0
        elif stage == 'enrich':
            return (counts.get('queued', 0) == 0
                    and counts.get('extracting', 0) == 0
                    and counts.get('extracted', 0) == 0)
        elif stage == 'embed':
            return (counts.get('queued', 0) == 0
                    and counts.get('extracting', 0) == 0
                    and counts.get('extracted', 0) == 0
                    and counts.get('enriching', 0) == 0
                    and counts.get('enriched', 0) == 0)
        return False

    def stage_loop(name, process_fn):
        """Run a stage: process available work, sleep, repeat until done."""
        logger.info(f"[{name}] Stage started")
        idle_cycles = 0

        while not stop_event.is_set():
            try:
                processed = process_fn()
            except Exception as e:
                logger.error(f"[{name}] Error: {e}")
                processed = 0

            if processed and processed > 0:
                totals[name] += processed
                idle_cycles = 0
                logger.info(f"[{name}] Batch done: {processed} docs (total: {totals[name]})")
                continue  # immediately check for more

            idle_cycles += 1

            # After 2 idle polls, check if upstream is finished
            if idle_cycles >= 2:
                counts = _doc_counts()
                if _upstream_done(name, counts):
                    logger.info(f"[{name}] No upstream work remaining, exiting "
                                f"(total: {totals[name]})")
                    break

            stop_event.wait(poll_interval)

        logger.info(f"[{name}] Stage finished — {totals[name]} documents processed")

    threads = [
        threading.Thread(
            target=stage_loop, daemon=True, name='extract',
            args=('extract', lambda: run_extraction(workers=extract_workers)),
        ),
        threading.Thread(
            target=stage_loop, daemon=True, name='enrich',
            args=('enrich', lambda: run_enrichment(workers=enrich_workers)),
        ),
        threading.Thread(
            target=stage_loop, daemon=True, name='embed',
            args=('embed', lambda: run_embedding(workers=embed_workers)),
        ),
    ]

    logger.info("=== RECON Pipeline Starting (concurrent) ===")
    logger.info(f"  Extract: {extract_workers} workers | "
                f"Enrich: {enrich_workers} workers | "
                f"Embed: {embed_workers} workers")

    for t in threads:
        t.start()

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(60)
            c = _doc_counts()
            logger.info(
                f"[pipeline] queued={c.get('queued', 0)} "
                f"extracting={c.get('extracting', 0)} "
                f"extracted={c.get('extracted', 0)} "
                f"enriching={c.get('enriching', 0)} "
                f"enriched={c.get('enriched', 0)} "
                f"embedding={c.get('embedding', 0)} "
                f"complete={c.get('complete', 0)} "
                f"failed={c.get('failed', 0)}"
            )
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted, stopping stages...")
        stop_event.set()
        for t in threads:
            t.join(timeout=30)

    logger.info(f"=== RECON Pipeline Complete: "
                f"{totals['extract']} extracted, "
                f"{totals['enrich']} enriched, "
                f"{totals['embed']} embedded ===")
    return 0


def cmd_status(args):
    db = StatusDB()
    counts = db.get_status_counts()

    print("=== RECON Status ===\n")

    cat = counts.get('catalogue', {})
    print("Catalogue:")
    for status, count in sorted(cat.items()):
        print(f"  {status}: {count}")
    print(f"  TOTAL: {sum(cat.values())}")

    doc = counts.get('documents', {})
    print("\nPipeline:")
    for status in ['queued', 'extracting', 'extracted', 'enriching', 'enriched', 'embedding', 'complete', 'failed']:
        count = doc.get(status, 0)
        if count > 0:
            print(f"  {status}: {count}")
    print(f"  TOTAL: {sum(doc.values())}")
    return 0


def cmd_catalogue(args):
    db = StatusDB()

    if args.sources:
        sources = db.source_breakdown()
        print(f"{'Source':<30} {'Count':>6} {'Size (MB)':>10}")
        print("-" * 50)
        for s in sources:
            size_mb = (s.get('total_bytes', 0) or 0) / (1024 * 1024)
            print(f"{s['source']:<30} {s['count']:>6} {size_mb:>10.1f}")
        return 0

    if args.categories:
        cats = db.category_breakdown(source=args.source)
        for c in cats:
            src = c.get('source', args.source or '')
            print(f"  {src}/{c['category']}: {c['count']}")
        return 0

    items = db.get_catalogued(
        source=args.source,
        category=args.category,
        limit=args.limit or 50
    )

    for item in items:
        size_mb = (item.get('size_bytes', 0) or 0) / (1024 * 1024)
        print(f"  [{item['hash'][:8]}] {item['filename']:<60} {size_mb:>8.1f} MB  {item['source']}/{item['category']}")

    print(f"\nShowing {len(items)} items")
    return 0


def cmd_failures(args):
    db = StatusDB()
    failures = db.get_failures()

    if not failures:
        print("No failures")
        return 0

    for f in failures:
        print(f"  [{f['hash'][:8]}] {f['filename']}")
        print(f"    Error: {f.get('error_message', 'unknown')}")
        print(f"    Retries: {f.get('retry_count', 0)}")
        print()

    print(f"Total failures: {len(failures)}")

    if args.retry:
        for f in failures:
            db.increment_retry(f['hash'])
        print(f"Re-queued {len(failures)} documents")

    return 0


def cmd_search(args):
    from qdrant_client import QdrantClient
    from lib.embedder import get_embedding_single

    config = get_config()
    query = ' '.join(args.query)
    query_vector = get_embedding_single(query, config)

    qdrant = QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=60
    )

    response = qdrant.query_points(
        collection_name=config['vector_db']['collection'],
        query=query_vector,
        limit=args.limit
    )
    results = response.points

    if not results:
        print("No results found")
        return 0

    for i, r in enumerate(results, 1):
        p = r.payload
        print(f"\n--- Result {i} (score: {r.score:.4f}) ---")
        print(f"  Title: {p.get('title', 'Untitled')}")
        print(f"  Book: {p.get('book_title', p.get('filename', '?'))}")
        print(f"  Type: {p.get('source_type', 'document')}")
        summary = p.get('summary', '')
        if summary:
            print(f"  Summary: {summary[:200]}")
        domains = p.get('domain', [])
        if domains:
            print(f"  Domains: {', '.join(domains) if isinstance(domains, list) else domains}")

    return 0


def cmd_upload(args):
    config = get_config()
    db = StatusDB()
    library_root = config['library_root']
    upload_paths = config.get('upload_paths', {})
    category = args.category or ''

    def _resolve_path(cat):
        if cat in upload_paths:
            return upload_paths[cat]
        default_path = upload_paths.get('default', library_root)
        if cat:
            from werkzeug.utils import secure_filename
            safe = secure_filename(cat)
            if safe:
                return os.path.join(default_path, safe)
        return default_path

    def _upload_one(filepath):
        filename = os.path.basename(filepath)
        if not filename.lower().endswith('.pdf'):
            print(f"  SKIP (not PDF): {filename}")
            return False

        file_hash = content_hash(filepath)

        # Check duplicate
        conn = db._get_conn()
        existing = conn.execute("SELECT filename FROM catalogue WHERE hash = ?", (file_hash,)).fetchone()
        if existing:
            print(f"  DUPLICATE: {filename} (matches {existing['filename']})")
            return False

        target_dir = _resolve_path(category)
        os.makedirs(target_dir, exist_ok=True)

        dest = os.path.join(target_dir, filename)
        if os.path.exists(dest):
            base, ext = os.path.splitext(filename)
            dest = os.path.join(target_dir, f"{base}_{file_hash[:8]}{ext}")

        shutil.copy2(filepath, dest)
        size = os.path.getsize(dest)
        source, derived_cat = derive_source_and_category(dest, library_root)

        db.add_to_catalogue(file_hash, filename, dest, size, source, derived_cat)
        db.queue_document(file_hash)

        print(f"  QUEUED: {filename} -> {source}/{derived_cat} [{file_hash[:8]}]")
        return True

    uploaded = 0

    if args.file:
        if not os.path.isfile(args.file):
            print(f"File not found: {args.file}")
            return 1
        if _upload_one(args.file):
            uploaded += 1

    elif args.dir:
        if not os.path.isdir(args.dir):
            print(f"Directory not found: {args.dir}")
            return 1
        for fname in sorted(os.listdir(args.dir)):
            fpath = os.path.join(args.dir, fname)
            if os.path.isfile(fpath) and fname.lower().endswith('.pdf'):
                if _upload_one(fpath):
                    uploaded += 1
    else:
        print("Specify --file or --dir")
        return 1

    print(f"\nUploaded {uploaded} PDF(s)")
    return 0


def cmd_ingest_url(args):
    from lib.web_scraper import ingest_url, ingest_urls

    urls = []

    if args.url:
        urls.append(args.url)

    if args.file:
        with open(args.file) as f:
            urls.extend([line.strip() for line in f if line.strip() and not line.startswith('#')])

    if not urls:
        print("Error: Provide a URL argument or --file with URLs")
        return 1

    print(f"Ingesting {len(urls)} URL(s) into category '{args.category}'...")

    if len(urls) == 1:
        try:
            result = ingest_url(urls[0], category=args.category, source=args.source)
            status = result.get('status', 'unknown').upper()
            print(f"  {status}: {result.get('title', 'Untitled')}")
            print(f"  Hash: {result['hash'][:16]}...")
            if result.get('page_count'):
                print(f"  Pages: {result['page_count']}")
            if result.get('existing_status'):
                print(f"  Existing status: {result['existing_status']}")
        except Exception as e:
            print(f"  FAILED: {e}")
            return 1
    else:
        results = ingest_urls(urls, category=args.category, source=args.source, delay=args.delay)
        for r in results:
            status = r.get('status', 'unknown').upper()
            title = r.get('title', r.get('url', 'Unknown'))
            print(f"  {status}: {title}")

        succeeded = sum(1 for r in results if r['status'] not in ('failed', 'duplicate'))
        dupes = sum(1 for r in results if r.get('status') == 'duplicate')
        failed = sum(1 for r in results if r.get('status') == 'failed')
        print(f"\nTotal: {succeeded} new, {dupes} duplicates, {failed} failed")

    # Optional: run enrichment
    if args.enrich or args.process:
        print("\nRunning enrichment on extracted content...")
        from lib.enricher import run_enrichment
        enriched = run_enrichment()
        print(f"  Enriched: {enriched}")

    # Optional: run embedding too
    if args.process:
        print("\nRunning embedding...")
        from lib.embedder import run_embedding
        embedded = run_embedding()
        print(f"  Embedded: {embedded}")

    return 0



def cmd_validate(args):
    from scripts.validate import run_validation
    run_validation(deep=args.deep)
    return 0


def cmd_rebuild(args):
    from scripts.rebuild_qdrant import run_rebuild
    run_rebuild()
    return 0


def cmd_serve(args):
    from lib.api import run_server
    run_server()
    return 0


def cmd_service(args):
    """Run RECON as a long-lived service. Called by systemd.

    Bundles: Flask dashboard + dispatcher + pipeline stages + filing worker + progress reporter.
    All threads are daemon threads; SIGTERM/SIGINT trigger graceful shutdown.
    """
    from lib.enricher import run_enrichment
    from lib.embedder import run_embedding
    from lib.api import app, run_server as start_dashboard
    from lib.dispatcher import dispatch_loop
    from lib.filing import filing_worker_loop
    from lib.acquisition.peertube import acquisition_loop

    config = get_config()
    proc = config.get('processing', {})
    svc = config.get('service', {})

    enrich_workers = proc.get('enrich_workers', 16)
    embed_workers = proc.get('embed_workers', 4)
    poll_interval = svc.get('stage_poll_interval', 30)
    dispatch_interval = svc.get('dispatch_interval', 30)
    filing_interval = svc.get('filing_interval', 30)
    progress_interval = svc.get('progress_interval', 60)
    web_host = config.get('web', {}).get('host', '0.0.0.0')
    web_port = config.get('web', {}).get('port', 8420)

    stop_event = threading.Event()
    totals = {'enrich': 0, 'embed': 0}

    def shutdown(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name}, shutting down gracefully...")
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def stage_loop(name, process_fn):
        """Run a pipeline stage: process batch, sleep, repeat forever."""
        logger.info(f"[{name}] Stage started")
        while not stop_event.is_set():
            try:
                processed = process_fn()
            except Exception as e:
                logger.error(f"[{name}] Error: {e}", exc_info=True)
                processed = 0
            if processed and processed > 0:
                totals[name] += processed
                continue
            stop_event.wait(poll_interval)
        logger.info(f"[{name}] Stage stopped (total: {totals[name]})")

    def progress_loop():
        """Log pipeline status periodically."""
        while not stop_event.is_set():
            stop_event.wait(progress_interval)
            if stop_event.is_set():
                break
            try:
                db = StatusDB()
                raw = db.get_status_counts()
                c = raw.get('documents', {})
                logger.info(
                    f"[pipeline] queued={c.get('queued', 0)} "
                    f"extracting={c.get('extracting', 0)} "
                    f"extracted={c.get('extracted', 0)} "
                    f"enriching={c.get('enriching', 0)} "
                    f"enriched={c.get('enriched', 0)} "
                    f"embedding={c.get('embedding', 0)} "
                    f"complete={c.get('complete', 0)} "
                    f"failed={c.get('failed', 0)}"
                )
            except Exception:
                pass

    db = StatusDB()

    threads = [
        threading.Thread(target=lambda: dispatch_loop(stop_event, db, config, interval=dispatch_interval),
                         daemon=True, name='dispatcher'),
        threading.Thread(target=stage_loop, daemon=True, name='enrich',
                         args=('enrich', lambda: run_enrichment(workers=enrich_workers))),
        threading.Thread(target=stage_loop, daemon=True, name='embed',
                         args=('embed', lambda: run_embedding(workers=embed_workers))),
        threading.Thread(target=lambda: filing_worker_loop(stop_event, db, config, interval=filing_interval),
                         daemon=True, name='filing'),
        threading.Thread(target=lambda: acquisition_loop(stop_event, db, config,
                                                       interval=config.get("peertube", {}).get("poll_interval", 1800)),
                         daemon=True, name="peertube-acq"),
        threading.Thread(target=progress_loop, daemon=True, name='progress'),
        threading.Thread(target=lambda: start_dashboard(stop_event),
                         daemon=True, name='dashboard'),
    ]

    # Scraper daemon: polls for pending scrape jobs, runs wget+zimwriterfs pipeline
    scraper_cfg = config.get('scraper', {})
    if scraper_cfg.get('workspace'):
        from lib.scraper_runner import scraper_loop
        threads.append(
            threading.Thread(target=lambda: scraper_loop(stop_event, config),
                             daemon=True, name='scraper')
        )

    logger.info("=== RECON Service Starting ===")
    logger.info(f"  Dashboard: {web_host}:{web_port}")
    logger.info(f"  Workers: enrich={enrich_workers}, embed={embed_workers}")
    logger.info(f"  Dispatcher: every {dispatch_interval}s | Filing: every {filing_interval}s")
    pt_interval = config.get("peertube", {}).get("poll_interval", 1800)
    logger.info(f"  PeerTube acquisition: every {pt_interval}s")
    if scraper_cfg.get('workspace'):
        logger.info(f"  Scraper: every {scraper_cfg.get('poll_interval', 300)}s")
    logger.info(f"  Progress: every {progress_interval}s")

    for t in threads:
        t.start()

    # Start metrics collector for time-series charts
    try:
        from lib.peertube_collector import start_collector
        start_collector(stop_event)
        logger.info("  Metrics collector started")
    except Exception as e:
        logger.warning(f"Metrics collector failed to start: {e}")

    logger.info("=== RECON Service Ready ===")

    # Block main thread until shutdown signal
    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        stop_event.set()

    # Give threads a moment to finish current batch
    logger.info("Waiting for threads to finish...")
    time.sleep(5)
    logger.info("=== RECON Service Stopped ===")
    return 0

def cmd_ingest_peertube(args):
    from lib.peertube_scraper import get_instance_stats
    from lib.acquisition.peertube import acquire_batch
    from lib.status import StatusDB

    if args.stats:
        stats = get_instance_stats()
        print("=== PeerTube Instance Stats ===")
        print(f"  Total videos: {stats['total_videos']}")
        print(f"  Ingested into RECON: {stats['ingested']}")
        if stats.get('status_breakdown'):
            print("  Pipeline status:")
            for status, count in sorted(stats['status_breakdown'].items()):
                print(f"    {status}: {count}")
        return 0

    db = StatusDB()

    print("Acquiring PeerTube transcripts to hopper...")
    result = acquire_batch(db)

    print(f"\nResults:")
    print(f"  Acquired:     {result['acquired']}")
    print(f"  Skipped:      {result['skipped']}")
    print(f"  Errors:       {result['errors']}")

    if result['acquired']:
        print(f"\n{result['acquired']} transcript(s) staged in hopper.")
        print("The dispatcher will pick them up on its next cycle.")
        print("Run 'recon status' to monitor progress.")

    return 0


def cmd_organize(args):
    """Organize completed documents into Domain/Subdomain folders."""
    from lib.organizer import organize_document, organize_from_manifest

    config = get_config()
    db = StatusDB()
    dry_run = args.dry_run

    if args.manifest:
        # Bulk migration from manifest
        print(f"Organizing from manifest: {args.manifest}")
        if dry_run:
            print("[DRY RUN] No files will be moved")
        stats = organize_from_manifest(args.manifest, db, config, dry_run=dry_run)
        print(f"\nManifest results:")
        print(f"  Total entries:      {stats['total']}")
        print(f"  Moved:              {stats['moved']}")
        print(f"  Already organized:  {stats['already_organized']}")
        print(f"  Skipped (ambig):    {stats['skipped']}")
        print(f"  Not found on disk:  {stats['not_found']}")
        print(f"  Errors:             {stats['errors']}")

        if not dry_run and stats['moved'] > 0:
            print("\nSyncing paths to Qdrant...")
            synced = sync_qdrant_paths()
            print(f"  Synced {synced} document paths")
        return 0

    # Single hash or batch of unorganized docs
    if args.hash:
        hashes = [args.hash]
    else:
        docs = db.get_unorganized(limit=args.limit)
        hashes = [d['hash'] for d in docs]
        if not hashes:
            print("No unorganized documents found")
            return 0
        print(f"Found {len(hashes)} unorganized documents")

    if dry_run:
        print("[DRY RUN] No files will be moved\n")

    moved = 0
    skipped = 0
    errors = 0

    for doc_hash in hashes:
        result = organize_document(doc_hash, db, config, dry_run=dry_run)
        action = result['action']

        if action == 'moved' or action == 'would_move':
            moved += 1
            if dry_run or args.verbose:
                print(f"  {'WOULD MOVE' if dry_run else 'MOVED'}: {result['before_path']}")
                print(f"    -> {result['after_path']}")
                print(f"    [{result['domain']}/{result['subdomain']}]")
        elif action == 'already_organized':
            skipped += 1
            if args.verbose:
                print(f"  SKIP (already organized): {result['before_path']}")
        elif action == 'skip_unclassified':
            skipped += 1
            if args.verbose:
                print(f"  SKIP (unclassified): {result['before_path']}")
        elif action == 'error':
            errors += 1
            print(f"  ERROR: {result.get('before_path', doc_hash[:8])}: {result['error']}")
        else:
            skipped += 1

    print(f"\nSummary: {moved} {'would move' if dry_run else 'moved'}, {skipped} skipped, {errors} errors")

    if not dry_run and moved > 0:
        print("\nSyncing paths to Qdrant...")
        synced = sync_qdrant_paths()
        print(f"  Synced {synced} document paths")

    return 0


def cmd_ingest(args):
    from lib.ingester import ingest_file, run_ingestion
    if args.file:
        results = ingest_file(args.file)
        success = sum(1 for r in results if r is not None)
        print(f"Ingested {success}/{len(results)} items from {args.file}")
    else:
        total = run_ingestion(directory=args.directory)
        print(f"Ingested {total} intel items")
    return 0


def cmd_pipeline(args):
    """Stream B library pipeline: status, migrate, reverse, watch, sweep."""
    from lib.new_pipeline import (
        migrate_domain, migrate_civil_org, reverse_operation, run_watchdog, update_qdrant_payload,
        compute_sweep_plan, execute_sweep_plan, verify_sweep, _save_sweep_plan,
    )

    config = get_config()
    db = StatusDB()

    if args.pipeline_action == 'status':
        stats = db.get_pipeline_stats()
        print("=== Stream B Pipeline Status ===\n")
        print("File Operations:")
        for op, cnt in stats.get('operations', {}).items():
            print(f"  {op}: {cnt}")
        if not stats.get('operations'):
            print("  (none)")
        print("\nDuplicate Review Queue:")
        for status, cnt in stats.get('duplicates', {}).items():
            print(f"  {status}: {cnt}")
        if not stats.get('duplicates'):
            print("  (none)")
        print(f"\nAcquired pending: {stats.get('acquired_pending', 0)}")
        print(f"Ingest pending:   {stats.get('ingest_pending', 0)}")

        # Recent operations
        recent = db.get_file_operations(limit=10)
        if recent:
            print("\nRecent operations:")
            for op in recent:
                print(f"  [{op['id']}] {op['operation']} {op['source_filename']} -> {op['target_filename']} "
                      f"(step {op['collision_step']}, {op['qdrant_points_updated']} vectors) "
                      f"at {op['performed_at']}")
        return 0

    elif args.pipeline_action == 'migrate':
        dry_run = args.dry_run
        domain = getattr(args, 'domain', None) or 'Civil Organization'
        if dry_run:
            print(f"[DRY RUN] {domain} — No files will be moved\n")
        else:
            print(f"=== {domain} Migration ===\n")

        stats = migrate_domain(domain, db, config, dry_run=dry_run)

        print(f"\nResults:")
        print(f"  Total PDFs found:     {stats['total']}")
        print(f"  Moved:                {stats['moved']}")
        print(f"  Renamed:              {stats['renamed']}")
        print(f"  Already correct:      {stats['already_correct']}")
        print(f"  Skipped:              {stats['skipped']}")
        print(f"  Not catalogued:       {stats['not_catalogued']}")
        print(f"  No book_title:        {stats['no_book_title']}")
        print(f"  Domain mismatch:      {stats['domain_mismatch']}")
        print(f"  Duplicates:           {stats['duplicates']}")
        print(f"  Failed:               {stats['failed']}")

        if stats.get('errors'):
            print(f"\nErrors:")
            for err in stats['errors'][:20]:
                print(f"  {err}")
        return 0

    elif args.pipeline_action == 'reverse':
        if not args.operation_id:
            print("Error: --id required for reverse")
            return 1
        op_id = int(args.operation_id)
        if reverse_operation(op_id, db, config):
            print(f"Reversed operation {op_id}")
        else:
            print(f"Failed to reverse operation {op_id}")
        return 0

    elif args.pipeline_action == 'watch':
        print("Starting pipeline watchdog (Ctrl+C to stop)...")
        run_watchdog(config)
        return 0

    elif args.pipeline_action == 'sweep':
        data_dir = config.get('paths', {}).get('data', '/opt/recon/data')
        output_dir = os.path.join(data_dir, 'sweep')

        if args.verify:
            print("=== Sweep Verification ===\n")
            ok, issues, domain_counts = verify_sweep(db, config)
            print(f"Verified operations: {ok} OK, {len(issues)} issues\n")
            if issues:
                print("Issues:")
                for iss in issues[:50]:
                    print(f"  [{iss['type']}] {iss['doc_hash'][:8]}: {iss.get('target_path', iss.get('source_path', '?'))}")
                if len(issues) > 50:
                    print(f"  ... and {len(issues) - 50} more")
            print("\nDomain distribution (post-sweep):")
            for dom, cnt in sorted(domain_counts.items(), key=lambda x: -x[1]):
                print(f"  {dom:<35s} {cnt:>6d}")
            return 0

        if args.execute or args.resume:
            plan_file = args.plan_file or os.path.join(output_dir, 'sweep_plan.json')
            if not os.path.exists(plan_file):
                print(f"Error: No sweep plan found at {plan_file}")
                print("Run: recon.py pipeline sweep --dry-run  first")
                return 1

            if args.plan_file:
                checkpoint_file = args.plan_file.replace('.json', '_checkpoint.json') if args.resume else None
            else:
                checkpoint_file = os.path.join(output_dir, 'sweep_checkpoint.json') if args.resume else None

            print(f"=== Executing Sweep ===")
            print(f"Plan: {plan_file}")
            if checkpoint_file:
                print(f"Resuming from checkpoint: {checkpoint_file}")
            print()

            stats = execute_sweep_plan(db, config, plan_file,
                                       batch_size=args.batch_size or 500,
                                       max_entries=args.max_entries,
                                       checkpoint_file=checkpoint_file)

            print(f"\nSweep Results:")
            print(f"  Total entries:       {stats['total']}")
            print(f"  Relocated:           {stats['relocated']}")
            print(f"  Rescued:             {stats['rescued']}")
            print(f"  Unclassified moved:  {stats['unclassified_moved']}")
            print(f"  No-op (marked):      {stats['no_op_marked']}")
            print(f"  Duplicates:          {stats['duplicates']}")
            print(f"  Skipped:             {stats['skipped']}")
            print(f"  Failed:              {stats['failed']}")
            print(f"  Qdrant updated:      {stats['qdrant_updated']}")
            return 0

        # Default: dry-run
        print("=== Sweep Dry Run ===\n")
        print("Computing plan (this may take several minutes)...\n")
        plan, stats = compute_sweep_plan(db, config)

        plan_file, summary_file = _save_sweep_plan(plan, stats, output_dir)

        print(f"Plan Summary:")
        print(f"  Total files scanned:  {stats['total_files']}")
        print(f"  Relocate:             {stats['relocate']}")
        print(f"  Rescue (uncataloged): {stats['rescue']}")
        print(f"  Unclassified:         {stats['unclassified']}")
        print(f"  No-op (correct):      {stats['no_op']}")
        print(f"  Skip in-progress:     {stats['skip_in_progress']}")
        print(f"  Skip failed:          {stats['skip_failed']}")
        print(f"  Skip garbage title:   {stats['skip_garbage']}")
        print(f"  Skip other:           {stats['skip_other']}")
        print(f"  Errors:               {stats['errors']}")
        print()
        print(f"Collision steps:")
        for step, cnt in sorted(stats['collision_steps'].items()):
            labels = {1: 'Title.pdf', 2: 'Title_Author.pdf', 3: 'Title_Author_Year.pdf', 4: 'duplicate_review'}
            print(f"  Step {step} ({labels.get(step, '?')}): {cnt}")
        print()
        print(f"Plan saved:    {plan_file}")
        print(f"Summary saved: {summary_file}")
        print()
        print("Review the plan, then execute with:")
        print(f"  recon.py pipeline sweep --execute")
        return 0

    else:
        print("Usage: recon.py pipeline {status|migrate|reverse|watch|sweep}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description='RECON — Knowledge Base Management System',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest='command', help='Available commands')

    # scan
    p = sub.add_parser('scan', help='Scan library and catalogue PDFs')
    p.add_argument('--path', help='Specific path to scan (default: library_root)')
    p.set_defaults(func=cmd_scan)

    # queue
    p = sub.add_parser('queue', help='Queue catalogued documents for processing')
    p.add_argument('--hash', help='Queue a specific document by hash')
    p.add_argument('--source', help='Filter by source')
    p.add_argument('--category', help='Filter by category')
    p.add_argument('--limit', type=int, help='Limit number queued')
    p.set_defaults(func=cmd_queue)

    # extract
    p = sub.add_parser('extract', help='Extract text from queued PDFs')
    p.add_argument('--workers', type=int, help='Number of workers')
    p.set_defaults(func=cmd_extract)

    # enrich
    p = sub.add_parser('enrich', help='Enrich extracted text with Gemini')
    p.add_argument('--workers', type=int, help='Number of workers')
    p.add_argument('--limit', type=int, help='Limit number enriched')
    p.set_defaults(func=cmd_enrich)

    # embed
    p = sub.add_parser('embed', help='Embed concepts into Qdrant')
    p.add_argument('--workers', type=int, help='Number of workers')
    p.add_argument('--limit', type=int, help='Limit number embedded')
    p.set_defaults(func=cmd_embed)

    # run
    p = sub.add_parser('run', help='Run full pipeline (extract -> enrich -> embed)')
    p.add_argument('--workers', type=int, default=4, help='Number of workers')
    p.add_argument('--enrich-workers', type=int, help='Override enrich worker count')
    p.add_argument('--limit', type=int, help='Limit documents per stage')
    p.set_defaults(func=cmd_run)

    # status
    p = sub.add_parser('status', help='Show pipeline status')
    p.set_defaults(func=cmd_status)

    # catalogue
    p = sub.add_parser('catalogue', help='Browse catalogue')
    p.add_argument('--sources', action='store_true', help='Show source breakdown')
    p.add_argument('--categories', action='store_true', help='Show category breakdown')
    p.add_argument('--source', help='Filter by source')
    p.add_argument('--category', help='Filter by category')
    p.add_argument('--limit', type=int, help='Limit results')
    p.set_defaults(func=cmd_catalogue)

    # failures
    p = sub.add_parser('failures', help='Show failed documents')
    p.add_argument('--retry', action='store_true', help='Re-queue all failures')
    p.set_defaults(func=cmd_failures)

    # search
    p = sub.add_parser('search', help='Semantic search the knowledge base')
    p.add_argument('query', nargs='+', help='Search query')
    p.add_argument('--limit', type=int, default=10, help='Number of results')
    p.set_defaults(func=cmd_search)

    # upload
    p = sub.add_parser('upload', help='Upload PDFs to the knowledge base')
    p.add_argument('--file', help='Upload a single PDF file')
    p.add_argument('--dir', help='Upload all PDFs from a directory')
    p.add_argument('--category', help='Category for uploaded files')
    p.set_defaults(func=cmd_upload)

    # ingest-url
    p = sub.add_parser('ingest-url', help='Ingest web content from URLs')
    p.add_argument('url', nargs='?', help='URL to ingest')
    p.add_argument('--file', help='File containing URLs (one per line)')
    p.add_argument('--category', default='Web', help='Category for ingested content')
    p.add_argument('--source', default='web', help='Source identifier')
    p.add_argument('--delay', type=float, default=1.0, help='Delay between URL fetches (seconds)')
    p.add_argument('--enrich', action='store_true', help='Run enrichment after ingestion')
    p.add_argument('--process', action='store_true', help='Full pipeline: ingest + enrich + embed')
    p.set_defaults(func=cmd_ingest_url)

    # crawl
    # validate
    p = sub.add_parser('validate', help='Validate pipeline consistency')
    p.add_argument('--deep', action='store_true', help='Deep validation (check all files)')
    p.set_defaults(func=cmd_validate)

    # rebuild
    p = sub.add_parser('rebuild', help='Rebuild Qdrant from concept JSONs')
    p.set_defaults(func=cmd_rebuild)

    # serve
    p = sub.add_parser('serve', help='Start web dashboard')
    p.set_defaults(func=cmd_serve)

    # service
    p = sub.add_parser('service', help='Run as long-lived service (dashboard + pipeline + scanner)')
    p.set_defaults(func=cmd_service)

    # organize
    p = sub.add_parser('organize', help='Organize completed docs into Domain/Subdomain folders')
    p.add_argument('--manifest', help='Bulk migration using pre-built manifest JSON')
    p.add_argument('--hash', help='Organize a single document by hash')
    p.add_argument('--dry-run', action='store_true', help='Show what would happen without moving')
    p.add_argument('--limit', type=int, help='Limit number of docs to organize')
    p.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    p.set_defaults(func=cmd_organize)

    # ingest-peertube
    p = sub.add_parser('ingest-peertube', help='Acquire PeerTube transcripts to hopper')
    p.add_argument('--stats', action='store_true', help='Show PeerTube instance stats')
    p.set_defaults(func=cmd_ingest_peertube)

    # ingest
    p = sub.add_parser('ingest', help='Ingest intel data')
    p.add_argument('--file', help='Ingest a specific JSON file')
    p.add_argument('--directory', help='Ingest all JSON files from directory')
    p.set_defaults(func=cmd_ingest)


    # pipeline (Stream B)
    p = sub.add_parser('pipeline', help='Stream B library pipeline (status, migrate, reverse, watch, sweep)')
    p.add_argument('pipeline_action', nargs='?', default='status',
                   choices=['status', 'migrate', 'reverse', 'watch', 'sweep'],
                   help='Pipeline sub-action')
    p.add_argument('--dry-run', action='store_true', help='Show what would happen without moving')
    p.add_argument('--id', dest='operation_id', help='Operation ID for reverse')
    p.add_argument('--domain', default=None, help='Domain name for migrate (default: Civil Organization)')
    p.add_argument('--execute', action='store_true', help='Execute sweep plan')
    p.add_argument('--resume', action='store_true', help='Resume sweep from checkpoint')
    p.add_argument('--verify', action='store_true', help='Verify sweep results')
    p.add_argument('--batch-size', type=int, default=500, help='Batch size for sweep execution')
    p.add_argument('--max-entries', type=int, default=None, help='Max entries to process per invocation (for gated execution)')
    p.add_argument('--plan-file', default=None, help='Path to sweep plan JSON (default: data/sweep/sweep_plan.json)')
    p.set_defaults(func=cmd_pipeline)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
