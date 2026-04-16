"""
RECON shared filing logic.

Provides file_processed_item() — the shared function that any processor
can call to file a completed item from /opt/recon/data/processing/{hash}/
into /mnt/library/Domain/Subdomain/{canonical_name}.{ext}.

The function:
  1. Reads dominant domain from concept JSONs (existing logic)
  2. Derives canonical name (level 1, escalating to 2/3/4 only on collision)
  3. Moves the source file from processing/ to library/Domain/Subdomain/
  4. Updates catalogue + documents + Qdrant payloads atomically
  5. Marks organized

This function does NOT extract, enrich, or embed. Those are upstream stages.
This function does NOT touch the legacy organize_document() — that stays in place
until cutover (Phase 5).

Phase 2: function exists, is tested in isolation. Not yet called by anything
in the service loop.
"""

import logging
import os
import shutil

from .organizer import determine_dominant_domain, _build_target_path
from .new_pipeline import update_qdrant_payload

logger = logging.getLogger("recon.filing")


def file_processed_item(doc_hash, source_file_path, db, config, dry_run=False):
    """File a completed item into the library.

    Args:
        doc_hash: Document hash
        source_file_path: Current absolute path to the source file
            (typically in /opt/recon/data/processing/{hash}/ or current library path)
        db: StatusDB instance
        config: RECON config dict
        dry_run: If True, plan but don't move

    Returns:
        dict with keys:
            hash, action, source_path, target_path,
            domain, subdomain, qdrant_points_updated, error
    """
    result = {
        "hash": doc_hash,
        "action": "skip",
        "source_path": source_file_path,
        "target_path": None,
        "domain": None,
        "subdomain": None,
        "qdrant_points_updated": 0,
        "error": None,
    }

    # Verify source file exists
    if not os.path.exists(source_file_path):
        result["action"] = "error"
        result["error"] = f"Source file not found: {source_file_path}"
        return result

    # Determine domain from existing concept JSONs
    data_dir = config["paths"]["data"]
    domain, subdomain, confidence = determine_dominant_domain(doc_hash, data_dir)
    result["domain"] = domain
    result["subdomain"] = subdomain

    if domain is None:
        result["action"] = "skip_unclassified"
        return result

    # Get the original filename from catalogue
    conn = db._get_conn()
    row = conn.execute(
        "SELECT filename FROM catalogue WHERE hash = ?", (doc_hash,)
    ).fetchone()
    if not row:
        result["action"] = "error"
        result["error"] = f"Hash not in catalogue: {doc_hash}"
        return result

    original_filename = row["filename"]

    # Build target path using existing collision-handling logic
    library_root = config["library_root"]
    target_path, sanitized_name = _build_target_path(
        library_root, domain, subdomain, original_filename, doc_hash
    )

    if target_path is None:
        result["action"] = "skip_unclassified"
        return result

    # Fix 1.1: Preserve the source file's actual extension instead of
    # the default .pdf that sanitize_filename() may have applied
    source_ext = os.path.splitext(source_file_path)[1].lower()
    if source_ext:
        target_stem, _old_ext = os.path.splitext(target_path)
        target_path = target_stem + source_ext
        san_stem, _old_ext = os.path.splitext(sanitized_name)
        sanitized_name = san_stem + source_ext

    result["target_path"] = target_path

    # If already at target (idempotency), just mark organized
    if os.path.abspath(source_file_path) == os.path.abspath(target_path):
        result["action"] = "skip_already_filed"
        if not dry_run:
            db.mark_organized(doc_hash)
        return result

    if dry_run:
        result["action"] = "would_file"
        return result

    # Move the file
    try:
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        shutil.move(source_file_path, target_path)
    except Exception as e:
        result["action"] = "error"
        result["error"] = f"Move failed: {e}"
        logger.error("Move failed for %s: %s", doc_hash[:8], e)
        return result

    # Update DB and Qdrant
    try:
        db.update_catalogue_path(doc_hash, target_path, sanitized_name)
        db.sync_document_path(doc_hash, target_path, sanitized_name)
        db.mark_organized(doc_hash)

        # Update Qdrant payloads (download_url, filename, original_filename)
        points = update_qdrant_payload(
            doc_hash, target_path, sanitized_name, original_filename, config
        )
        result["qdrant_points_updated"] = points

        result["action"] = "filed"
        logger.info(
            "Filed %s -> %s [%s/%s, %d vectors]",
            doc_hash[:8],
            target_path,
            domain,
            subdomain,
            points,
        )
    except Exception as e:
        # File was moved but DB update failed — log the dangerous state
        result["action"] = "error"
        result["error"] = f"DB/Qdrant update failed after move: {e}"
        logger.error("DB/Qdrant update failed for %s: %s", doc_hash[:8], e)

    return result


def filing_worker_loop(stop_event, db, config, interval=30):
    """Run filing on items ready to be filed until stop_event is set.

    Watches for documents with status='complete', organized_at IS NULL,
    and path in /opt/recon/data/processing/. Files them to library.

    Designed to run as a service thread. Never raises to the caller.
    """
    logger.info("[filing] Worker started (interval: %ds)", interval)

    while not stop_event.is_set():
        try:
            conn = db._get_conn()
            rows = conn.execute(
                "SELECT hash, path FROM documents "
                "WHERE status = 'complete' "
                "AND organized_at IS NULL "
                "AND path LIKE '/opt/recon/data/processing/%' "
                "LIMIT 50"
            ).fetchall()

            if rows:
                filed = 0
                skipped = 0
                errors = 0
                for row in rows:
                    if stop_event.is_set():
                        break
                    try:
                        result = file_processed_item(row['hash'], row['path'], db, config)
                        action = result.get('action', 'unknown')
                        if action == 'filed':
                            filed += 1
                        elif action.startswith('skip'):
                            skipped += 1
                        elif action == 'error':
                            errors += 1
                            logger.warning("[filing] Error filing %s: %s",
                                           row['hash'][:8], result.get('error', 'unknown'))
                    except Exception as e:
                        errors += 1
                        logger.error("[filing] Exception filing %s: %s",
                                     row['hash'][:8], e, exc_info=True)

                logger.info("[filing] Batch: %d filed, %d skipped, %d errors",
                            filed, skipped, errors)
            else:
                logger.debug("[filing] No items ready to file")

        except Exception as e:
            logger.error("[filing] Error in filing worker: %s", e, exc_info=True)

        stop_event.wait(interval)

    logger.info("[filing] Worker stopped")
