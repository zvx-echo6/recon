"""
RECON Text Processor

Handles pre_flight for plain .txt files arriving in acquired/text/.
These are primary source documents (books, manuals, guides), not derived
content like transcripts.

Metadata extraction via two-source vote:
  A. Filename parsing (title, optionally author)
  B. Gemini LLM extraction (title/author/edition/year from first 3 pages)

Filing behavior matches PDFs: files get organized to library/Domain/Subdomain/
by the filing worker (NOT organized in-place like transcripts).

Phase 6f: initial implementation.
"""
import json
import logging
import os
import re
import shutil

from lib.web_scraper import chunk_text
from lib.utils import content_hash, clean_filename_to_title
from lib.processors.pdf_processor import _extract_gemini_metadata

logger = logging.getLogger("recon.processors.text")

WORDS_PER_PAGE = 2000


def _extract_filename_metadata(filename):
    """Source A: Extract metadata by parsing the filename.

    Returns dict with keys: title, author, edition, year (any may be None).
    """
    result = {'title': None, 'author': None, 'edition': None, 'year': None}

    stem = os.path.splitext(filename)[0]

    result['title'] = clean_filename_to_title(filename)

    # Year: look for (YYYY) or [YYYY] or _YYYY_ or standalone YYYY
    year_match = re.search(r'[\(\[_\s]((?:19|20)\d{2})[\)\]_\s.]', stem)
    if year_match:
        result['year'] = year_match.group(1)

    # Edition: Nth Edition, Edition N, etc.
    ed_match = re.search(
        r'(\d+)(?:st|nd|rd|th)?\s*(?:edition|ed\.?)',
        stem, re.IGNORECASE
    )
    if ed_match:
        result['edition'] = ed_match.group(0).strip()

    # Author: "Title - Author" or "Title by Author" patterns
    # Try " - Author" at end
    dash_match = re.search(r'\s+-\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)$', stem)
    if dash_match:
        result['author'] = dash_match.group(1).strip()
    else:
        by_match = re.search(r'\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', stem)
        if by_match:
            result['author'] = by_match.group(1).strip()

    return result


def _vote_metadata(source_a, source_b):
    """Vote on each metadata field across two sources.

    Priority: B (Gemini) > A (filename).
    If both agree, note agreement.

    Returns:
        (voted_dict, provenance_record)
    """
    sources = {'filename': source_a, 'gemini': source_b}
    priority = ['gemini', 'filename']
    voted = {}
    provenance = {}

    for field in ('title', 'author', 'edition', 'year'):
        values = {}
        for name, src in sources.items():
            val = src.get(field)
            if val and str(val).strip().lower() != 'null':
                values[name] = val

        if not values:
            voted[field] = None
            provenance[field] = None
            continue

        def _norm(v):
            if field == 'year':
                return v.strip()
            return v.strip().lower()

        norm_groups = {}
        for name, val in values.items():
            nv = _norm(val)
            norm_groups.setdefault(nv, []).append((name, val))

        best_group = max(norm_groups.values(), key=len)

        if len(best_group) >= 2:
            # Both sources agree
            for p in priority:
                for name, val in best_group:
                    if name == p:
                        voted[field] = val
                        names = ','.join(n for n, _ in best_group)
                        provenance[field] = "agreed({})".format(names)
                        break
                if voted.get(field) is not None:
                    break
        else:
            # No agreement — highest priority wins
            for p in priority:
                if p in values:
                    voted[field] = values[p]
                    provenance[field] = p
                    break

    provenance_record = {
        'voted': voted,
        'provenance': provenance,
        'sources': {
            'filename': source_a,
            'gemini': source_b,
        }
    }

    return voted, provenance_record


def pre_flight(content_path, meta_path, db, config):
    """Process a .txt file dropped into acquired/text/.

    Args:
        content_path: Path to the .txt file
        meta_path: Path to .meta.json sidecar (may be None)
        db: StatusDB instance
        config: RECON config dict

    Returns:
        dict with keys: hash, action, source_path, error
        Actions: 'extracted', 'duplicate', 'skip_empty', 'error'
    """
    result = {
        'hash': None,
        'action': 'error',
        'source_path': content_path,
        'error': None,
    }

    filename = os.path.basename(content_path)

    # ── Step 1: Hash ──────────────────────────────────────────────
    try:
        file_hash = content_hash(content_path)
        result['hash'] = file_hash
    except Exception as e:
        result['error'] = "Cannot hash text file: {}".format(e)
        return result

    # ── Step 2: Stale state cleanup ───────────────────────────────
    processing_root = config.get('pipeline', {}).get(
        'processing_root', '/opt/recon/data/processing'
    )
    proc_dir = os.path.join(processing_root, file_hash)
    concepts_dir = os.path.join(config['paths']['concepts'], file_hash)
    if os.path.exists(proc_dir):
        try:
            shutil.rmtree(proc_dir)
        except Exception as e:
            logger.error("Stale cleanup failed for %s: %s", proc_dir, e)
            raise
    if os.path.exists(concepts_dir):
        try:
            shutil.rmtree(concepts_dir)
        except Exception as e:
            logger.error("Stale cleanup failed for %s: %s", concepts_dir, e)
            raise

    # ── Step 3: Hash dedupe ───────────────────────────────────────
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

    # ── Step 4: Read text content ─────────────────────────────────
    try:
        with open(content_path, encoding='utf-8', errors='replace') as f:
            raw_text = f.read()
    except Exception as e:
        result['error'] = "Cannot read text file: {}".format(e)
        return result

    if len(raw_text.strip()) < 50:
        logger.info("Text file too short (%d chars), skipping: %s",
                     len(raw_text.strip()), filename)
        try:
            os.remove(content_path)
            if meta_path:
                os.remove(meta_path)
        except OSError:
            pass
        result['action'] = 'skip_empty'
        return result

    # ── Step 5: Read optional sidecar ─────────────────────────────
    sidecar = None
    if meta_path:
        try:
            with open(meta_path, encoding='utf-8') as f:
                sidecar = json.load(f)
        except Exception as e:
            logger.warning("Cannot read sidecar %s: %s", meta_path, e)

    # ── Step 6: Set up processing directory ───────────────────────
    try:
        os.makedirs(proc_dir, exist_ok=True)
    except Exception as e:
        result['error'] = "Cannot create processing dir: {}".format(e)
        return result

    # ── Step 7: Move files to processing ──────────────────────────
    source_path = os.path.join(proc_dir, 'source.txt')
    try:
        shutil.move(content_path, source_path)
        if meta_path:
            shutil.move(meta_path, os.path.join(proc_dir, 'meta.json'))
    except Exception as e:
        result['error'] = "Cannot move files to processing: {}".format(e)
        return result

    # ── Step 8: Split into pages ──────────────────────────────────
    pages = chunk_text(raw_text, WORDS_PER_PAGE)
    for i, page_text in enumerate(pages, start=1):
        page_path = os.path.join(proc_dir, "page_{:04d}.txt".format(i))
        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(page_text)

    # ── Step 9: Source A — filename metadata ──────────────────────
    source_a = _extract_filename_metadata(filename)

    # ── Step 10: Source B — Gemini metadata ───────────────────────
    first_pages_text = "\n".join(pages[:3])
    source_b = _extract_gemini_metadata(first_pages_text, config)

    # ── Step 11: Vote ─────────────────────────────────────────────
    voted, provenance_record = _vote_metadata(source_a, source_b)

    # ── Step 12: Write meta.json ──────────────────────────────────
    meta = {
        'hash': file_hash,
        'filename': filename,
        'source_type': 'text',
        'page_count': len(pages),
        'text_length': len(raw_text),
        'metadata': voted,
        'metadata_provenance': provenance_record,
    }
    if sidecar:
        meta['sidecar'] = sidecar

    with open(os.path.join(proc_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)

    # ── Step 13: Register in catalogue ────────────────────────────
    display_title = voted.get('title') or clean_filename_to_title(filename)
    size_bytes = os.path.getsize(source_path)

    db.add_to_catalogue(
        file_hash, filename, source_path, size_bytes, 'text', 'Document'
    )

    # ── Step 14: Queue and update documents row ───────────────────
    db.queue_document(file_hash)

    conn = db._get_conn()
    conn.execute(
        "UPDATE documents SET "
        "text_dir = ?, page_count = ?, path = ?, "
        "book_title = ?, book_author = ?, metadata_provenance = ? "
        "WHERE hash = ?",
        (
            proc_dir,
            len(pages),
            source_path,
            voted.get('title'),
            voted.get('author'),
            json.dumps(provenance_record),
            file_hash,
        )
    )
    conn.commit()

    # ── Step 15: Status = extracted ───────────────────────────────
    db.update_status(file_hash, 'extracted', pages_extracted=len(pages))

    logger.info(
        "Text pre_flight complete: %s (%s) -> %d pages in %s",
        file_hash[:8], display_title, len(pages), proc_dir,
    )

    result['action'] = 'extracted'
    return result
