"""
RECON Transcript Processor

Handles pre_flight for transcript content arriving in acquired/stream/.
Reads a raw text file + meta.json sidecar, hashes, dedupes, splits into
page_NNNN.txt files, and registers in the database.

Phase 3: first processor implementation.
Phase 4: added stale state cleanup at start of pre_flight.
"""
import hashlib
import json
import logging
import os
import shutil

from lib.web_scraper import chunk_text
from lib.utils import content_hash

logger = logging.getLogger("recon.processors.transcript")

# Words per page for transcript chunking (matches existing pipeline)
WORDS_PER_PAGE = 2000


def pre_flight(content_path, meta_path, db, config):
    """Process a transcript pair from acquired/stream/.

    Args:
        content_path: Path to the raw transcript .txt file
        meta_path: Path to the .meta.json sidecar
        db: StatusDB instance
        config: RECON config dict

    Returns:
        dict with keys: hash, action, source_path, error
        Actions: 'extracted', 'duplicate', 'error'
    """
    result = {
        'hash': None,
        'action': 'error',
        'source_path': content_path,
        'error': None,
    }

    # Read and hash the content file
    try:
        file_hash = content_hash(content_path)
        result['hash'] = file_hash
    except Exception as e:
        result['error'] = f"Cannot hash content file: {e}"
        return result

    # Stale state cleanup — remove any pre-existing processing/concepts dirs
    processing_root = config.get('pipeline', {}).get(
        'processing_root', '/opt/recon/data/processing'
    )
    proc_dir = os.path.join(processing_root, file_hash)
    concepts_dir = os.path.join(config['paths']['concepts'], file_hash)
    shutil.rmtree(proc_dir, ignore_errors=True)
    shutil.rmtree(concepts_dir, ignore_errors=True)

    # Hash dedupe: if hash exists in catalogue, delete the pair and return
    conn = db._get_conn()
    existing = conn.execute(
        "SELECT hash FROM catalogue WHERE hash = ?", (file_hash,)
    ).fetchone()
    if existing:
        logger.info("Duplicate hash %s, removing pair", file_hash[:8])
        try:
            os.remove(content_path)
            if meta_path:
                os.remove(meta_path)
        except OSError as e:
            logger.warning("Failed to remove duplicate pair: %s", e)
        result['action'] = 'duplicate'
        return result

    # Read meta.json sidecar
    try:
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
    except Exception as e:
        result['error'] = f"Cannot read meta.json: {e}"
        return result

    # Read raw transcript text
    try:
        with open(content_path, encoding='utf-8') as f:
            raw_text = f.read()
    except Exception as e:
        result['error'] = f"Cannot read content file: {e}"
        return result

    if not raw_text.strip():
        result['error'] = "Empty transcript"
        return result

    # Set up processing directory
    try:
        os.makedirs(proc_dir, exist_ok=True)
    except Exception as e:
        result['error'] = f"Cannot create processing dir: {e}"
        return result

    # Move the pair to processing/{hash}/
    try:
        shutil.move(content_path, os.path.join(proc_dir, 'transcript.txt'))
        shutil.move(meta_path, os.path.join(proc_dir, 'meta.json'))
    except Exception as e:
        result['error'] = f"Cannot move pair to processing: {e}"
        return result

    # Split raw text into page_NNNN.txt files
    pages = chunk_text(raw_text, WORDS_PER_PAGE)
    for i, page_text in enumerate(pages, start=1):
        page_path = os.path.join(proc_dir, f"page_{i:04d}.txt")
        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(page_text)

    # Update meta.json with processing metadata
    meta['hash'] = file_hash
    meta['source_type'] = meta.get('source_type', 'transcript')
    meta['page_count'] = len(pages)
    meta['text_length'] = len(raw_text)
    meta_out_path = os.path.join(proc_dir, 'meta.json')
    with open(meta_out_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)

    # Register in catalogue
    title = meta.get('title', os.path.basename(content_path))
    source_url = meta.get('source_url', meta.get('url', ''))
    source = 'stream.echo6.co'
    category = meta.get('category', 'Transcript')
    size_bytes = os.path.getsize(os.path.join(proc_dir, 'transcript.txt'))

    db.add_to_catalogue(file_hash, title, source_url, size_bytes, source, category)

    # Queue and advance to extracted
    db.queue_document(file_hash)

    # Set text_dir and page_count on the documents row
    conn = db._get_conn()
    conn.execute(
        "UPDATE documents SET text_dir = ?, page_count = ? WHERE hash = ?",
        (proc_dir, len(pages), file_hash)
    )
    conn.commit()

    # Update status to extracted with page count
    db.update_status(file_hash, 'extracted', pages_extracted=len(pages))

    logger.info(
        "Transcript pre_flight complete: %s (%s) -> %d pages in %s",
        file_hash[:8], title, len(pages), proc_dir,
    )

    result['action'] = 'extracted'
    return result
