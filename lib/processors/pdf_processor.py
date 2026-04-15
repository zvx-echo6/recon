"""
RECON PDF Processor

Handles pre_flight for PDF content arriving in acquired/pdf/.
Opens the PDF, extracts layered metadata (PDF dict + filename + Gemini),
votes on fields, checks level-4 dedupe, runs full text extraction,
and registers in the database.

Metadata sources:
  A. PDF info dictionary (PyPDF2 reader.metadata)
  B. Filename parsing (clean_filename_to_title + regex patterns)
  C. Gemini LLM on first 3 pages of extracted text

Voting: if 2+ sources agree on a field, use that value.
Otherwise priority: C (Gemini) > A (PDF dict) > B (filename).

Level-4 dedupe: requires ALL FOUR fields (title, author, edition, year)
present and matching an existing document. Very conservative — only
catches near-certain duplicates.

Failure modes:
  - Transient (Gemini API): retry 3x with backoff, then continue without
  - Content (unreadable PDF): move to _review/rejected_pdfs/
  - Level-4 duplicate: move to _review/duplicate_quarantine/

Phase 4: first implementation.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import time

import google.generativeai as genai
from PyPDF2 import PdfReader

from lib.extractor import extract_text_from_page
from lib.utils import content_hash, clean_filename_to_title, sanitize_filename

logger = logging.getLogger("recon.processors.pdf")

# Maximum retries for transient (API) failures
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = 30


# ── Metadata Extraction Sources ─────────────────────────────────────


def _extract_pdf_dict(reader):
    """Source A: Extract metadata from PDF's built-in info dictionary.

    Returns dict with keys: title, author, edition, year (any may be None).
    """
    result = {'title': None, 'author': None, 'edition': None, 'year': None}

    try:
        info = reader.metadata
        if not info:
            return result
    except Exception:
        return result

    # Title
    title = None
    try:
        title = info.get('/Title') or info.title
    except Exception:
        pass
    if title and isinstance(title, str) and len(title.strip()) > 2:
        result['title'] = title.strip()

    # Author
    author = None
    try:
        author = info.get('/Author') or info.author
    except Exception:
        pass
    if author and isinstance(author, str) and len(author.strip()) > 1:
        result['author'] = author.strip()

    # Year from CreationDate or ModDate
    for date_key in ('/CreationDate', '/ModDate'):
        try:
            date_val = info.get(date_key)
        except Exception:
            continue
        if date_val and isinstance(date_val, str):
            match = re.search(r'(?:D:)?(\d{4})', date_val)
            if match:
                year = int(match.group(1))
                if 1800 <= year <= 2030:
                    result['year'] = str(year)
                    break

    # Edition from Subject or Title
    for field_val in [info.get('/Subject'), title]:
        if field_val and isinstance(field_val, str):
            ed_match = re.search(
                r'(\d+)(?:st|nd|rd|th)?\s*(?:edition|ed\.?)',
                field_val, re.IGNORECASE
            )
            if ed_match:
                result['edition'] = ed_match.group(0).strip()
                break

    return result


def _extract_filename_metadata(filename):
    """Source B: Extract metadata by parsing the filename.

    Returns dict with keys: title, author, edition, year (any may be None).
    """
    result = {'title': None, 'author': None, 'edition': None, 'year': None}

    stem = os.path.splitext(filename)[0]

    # Title from filename (using existing utility)
    result['title'] = clean_filename_to_title(filename)

    # Year: look for (YYYY) or [YYYY] or _YYYY_ or standalone YYYY near boundaries
    year_match = re.search(r'[\(\[_\s]((?:19|20)\d{2})[\)\]_\s.]', stem)
    if year_match:
        result['year'] = year_match.group(1)

    # Edition: look for Nth Edition, Edition N, etc.
    ed_match = re.search(
        r'(\d+)(?:st|nd|rd|th)?\s*(?:edition|ed\.?)',
        stem, re.IGNORECASE
    )
    if ed_match:
        result['edition'] = ed_match.group(0).strip()

    # Author: look for "by Author Name" pattern
    by_match = re.search(r'\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', stem)
    if by_match:
        result['author'] = by_match.group(1).strip()

    return result


def _extract_gemini_metadata(pages_text, config):
    """Source C: Extract metadata using Gemini on first 3 pages text.

    Returns dict with keys: title, author, edition, year (any may be None).
    Retries up to MAX_TRANSIENT_RETRIES times on transient failures.
    """
    result = {'title': None, 'author': None, 'edition': None, 'year': None}

    keys = config.get('gemini_keys', [])
    if not keys or len(pages_text.strip()) < 50:
        return result

    prompt = (
        "Extract the following metadata from this book/document text.\n"
        'Return JSON: {"title": "...", "author": "...", "edition": "...", "year": "..."}\n'
        "- title: The full title of the book or document\n"
        "- author: The author(s) name(s)\n"
        '- edition: The edition (e.g. "2nd Edition", "Revised Edition")\n'
        "- year: The publication year (4-digit number as string)\n"
        "If a field cannot be determined, use null.\n\n"
        "Text from first pages:\n"
        + pages_text[:6000]
    )

    for attempt in range(MAX_TRANSIENT_RETRIES):
        try:
            key = keys[attempt % len(keys)]
            genai.configure(api_key=key)
            model = genai.GenerativeModel(
                config['gemini']['model'],
                generation_config={
                    "response_mime_type": config['gemini']['response_mime_type']
                }
            )
            response = model.generate_content(prompt)
            data = json.loads(response.text)

            for field in ('title', 'author', 'edition', 'year'):
                val = data.get(field)
                if val and isinstance(val, str) and val.strip() and val.strip().lower() != "null":
                    result[field] = val.strip()

            return result

        except Exception as e:
            logger.warning(
                "Gemini metadata attempt %d/%d failed: %s",
                attempt + 1, MAX_TRANSIENT_RETRIES, e
            )
            if attempt < MAX_TRANSIENT_RETRIES - 1:
                time.sleep(TRANSIENT_BACKOFF_SECONDS)

    logger.warning(
        "Gemini metadata extraction failed after %d attempts", MAX_TRANSIENT_RETRIES
    )
    return result


# ── Voting ───────────────────────────────────────────────────────────


def _vote_metadata(source_a, source_b, source_c):
    """Vote on each metadata field across three sources.

    Priority: C (Gemini) > A (PDF dict) > B (filename).
    If 2+ sources agree, use the agreed value.

    Returns:
        (voted_dict, provenance_dict)
        voted_dict:  {field: value_or_None}
        provenance_dict: {field: source_label_or_None}
    """
    sources = {'pdf_dict': source_a, 'filename': source_b, 'gemini': source_c}
    priority = ['gemini', 'pdf_dict', 'filename']
    voted = {}
    provenance = {}

    for field in ('title', 'author', 'edition', 'year'):
        values = {}
        for name, src in sources.items():
            val = src.get(field)
            if val and str(val).strip().lower() != "null":
                values[name] = val

        if not values:
            voted[field] = None
            provenance[field] = None
            continue

        # Normalize for comparison
        def _norm(v):
            if field == 'year':
                return v.strip()
            return v.strip().lower()

        # Group by normalized value
        norm_groups = {}
        for name, val in values.items():
            nv = _norm(val)
            norm_groups.setdefault(nv, []).append((name, val))

        # Find group with most agreement
        best_group = max(norm_groups.values(), key=len)

        if len(best_group) >= 2:
            # 2+ sources agree — use highest-priority original case
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

    return voted, provenance


# ── Level-4 Dedupe ───────────────────────────────────────────────────


def _check_level4_dedupe(voted, db):
    """Level-4 strict dedupe: all four fields must be present and match.

    Returns (is_duplicate, matching_hash) or (False, None).
    """
    title = voted.get('title')
    author = voted.get('author')
    edition = voted.get('edition')
    year = voted.get('year')

    if not all([title, author, edition, year]):
        return False, None

    conn = db._get_conn()
    rows = conn.execute(
        "SELECT hash, metadata_provenance FROM documents "
        "WHERE book_title = ? AND book_author = ?",
        (title, author)
    ).fetchall()

    if not rows:
        return False, None

    for row in rows:
        prov_json = row['metadata_provenance']
        if not prov_json:
            continue
        try:
            prov_data = json.loads(prov_json)
            existing_voted = prov_data.get('voted', {})
            ex_edition = existing_voted.get('edition', '')
            ex_year = existing_voted.get('year', '')
            if (ex_edition and ex_year
                    and ex_edition.lower() == edition.lower()
                    and ex_year == year):
                return True, row['hash']
        except (json.JSONDecodeError, AttributeError):
            continue

    return False, None


# ── Page Count Fallback ──────────────────────────────────────────────


def _pdfinfo_page_count(pdf_path):
    """Get page count via pdfinfo (poppler) when PdfReader fails."""
    try:
        proc = subprocess.run(
            ['pdfinfo', pdf_path],
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.startswith('Pages:'):
                    return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return 0


# ── Pre-Flight ───────────────────────────────────────────────────────


def pre_flight(content_path, meta_path, db, config):
    """Process a PDF from acquired/pdf/.

    Args:
        content_path: Path to the PDF file
        meta_path: Path to .meta.json sidecar (may be None)
        db: StatusDB instance
        config: RECON config dict

    Returns:
        dict with keys: hash, action, source_path, error
        Actions: 'extracted', 'duplicate', 'level4_duplicate',
                 'content_failure', 'error'
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
        result['error'] = "Cannot hash PDF: {}".format(e)
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

    # ── Step 4: Size check ────────────────────────────────────────
    proc_cfg = config.get('processing', {})
    max_size_mb = proc_cfg.get('max_pdf_size_mb', 200)
    try:
        file_size_mb = os.path.getsize(content_path) / 1048576
    except OSError as e:
        result['error'] = "Cannot stat PDF: {}".format(e)
        return result

    if file_size_mb > max_size_mb:
        result['error'] = "PDF too large: {:.0f}MB > {}MB limit".format(
            file_size_mb, max_size_mb
        )
        result['action'] = 'content_failure'
        _move_to_rejected(content_path, meta_path, config, filename)
        return result

    # ── Step 5: Open PDF ──────────────────────────────────────────
    reader = None
    page_count = 0

    try:
        reader = PdfReader(content_path)
        page_count = len(reader.pages)
    except Exception as e:
        logger.warning(
            "PdfReader failed for %s: %s — trying pdfinfo", filename, e
        )
        page_count = _pdfinfo_page_count(content_path)

    if page_count == 0:
        logger.error("Content failure: cannot read PDF %s", filename)
        result['action'] = 'content_failure'
        result['error'] = "Cannot read PDF (0 pages)"
        _move_to_rejected(content_path, meta_path, config, filename)
        return result

    # ── Step 6: Source A — PDF dict metadata ──────────────────────
    source_a = _extract_pdf_dict(reader) if reader else {
        'title': None, 'author': None, 'edition': None, 'year': None
    }

    # ── Step 7: Source B — Filename metadata ──────────────────────
    source_b = _extract_filename_metadata(filename)

    # ── Step 8: Extract first 3 pages for Source C ────────────────
    page_timeout = proc_cfg.get('page_timeout', 30)
    first_pages_text = ""

    if reader:
        for i in range(min(3, page_count)):
            try:
                text, _method = extract_text_from_page(
                    reader, i, content_path, page_timeout
                )
                first_pages_text += text + "\n"
            except Exception as e:
                logger.warning(
                    "Failed to extract page %d for metadata: %s", i + 1, e
                )

    # ── Step 9: Source C — Gemini metadata ────────────────────────
    source_c = _extract_gemini_metadata(first_pages_text, config)

    # ── Step 10: Vote ─────────────────────────────────────────────
    voted, provenance = _vote_metadata(source_a, source_b, source_c)

    provenance_record = {
        'voted': voted,
        'provenance': provenance,
        'sources': {
            'pdf_dict': source_a,
            'filename': source_b,
            'gemini': source_c,
        }
    }

    # ── Step 11: Level-4 dedupe ───────────────────────────────────
    is_dup, dup_hash = _check_level4_dedupe(voted, db)
    if is_dup:
        logger.info(
            "Level-4 duplicate: %s matches %s", file_hash[:8], dup_hash[:8]
        )
        quarantine_dir = os.path.join(
            config.get('library_root', '/mnt/library'),
            '_review', 'duplicate_quarantine'
        )
        try:
            os.makedirs(quarantine_dir, exist_ok=True)
            quarantine_path = os.path.join(quarantine_dir, filename)
            shutil.move(content_path, quarantine_path)
            if meta_path:
                os.remove(meta_path)

            san_name = sanitize_filename(filename, doc_hash=file_hash)
            db.queue_duplicate_review(
                doc_hash=file_hash,
                original_filename=filename,
                sanitized_filename=san_name,
                collision_with_hash=dup_hash,
                duplicate_path=quarantine_path,
                book_title=voted.get('title'),
                book_author=voted.get('author'),
            )
        except Exception as e:
            logger.error("Failed to quarantine duplicate: %s", e)

        result['action'] = 'level4_duplicate'
        return result

    # ── Step 12: Set up processing directory ──────────────────────
    try:
        os.makedirs(proc_dir, exist_ok=True)
    except Exception as e:
        result['error'] = "Cannot create processing dir: {}".format(e)
        return result

    pdf_proc_path = os.path.join(proc_dir, 'source.pdf')
    try:
        shutil.move(content_path, pdf_proc_path)
        if meta_path:
            shutil.move(meta_path, os.path.join(proc_dir, 'sidecar.meta.json'))
    except Exception as e:
        result['error'] = "Cannot move PDF to processing: {}".format(e)
        return result

    # ── Step 13: Full text extraction ─────────────────────────────
    # Re-open reader from new location
    try:
        reader = PdfReader(pdf_proc_path)
    except Exception:
        reader = None

    pages_extracted = 0
    ocr_pages = []
    ocr_methods = {
        'pypdf2': 0, 'pdftotext': 0, 'tesseract': 0,
        'gemini_vision': 0, 'none': 0,
    }

    for i in range(page_count):
        try:
            if reader:
                text, method = extract_text_from_page(
                    reader, i, pdf_proc_path, page_timeout
                )
            else:
                proc_result = subprocess.run(
                    ['pdftotext', '-f', str(i + 1), '-l', str(i + 1),
                     pdf_proc_path, '-'],
                    capture_output=True, text=True, timeout=page_timeout
                )
                text = proc_result.stdout if proc_result.returncode == 0 else ''
                method = 'pdftotext' if text.strip() else 'none'

            ocr_methods[method] += 1
            if method in ('tesseract', 'gemini_vision'):
                ocr_pages.append(i + 1)
        except Exception as e:
            logger.warning("Page %d/%d failed: %s", i + 1, page_count, e)
            text = ''
            ocr_methods['none'] += 1

        page_path = os.path.join(proc_dir, "page_{:04d}.txt".format(i + 1))
        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(text)

        if text.strip():
            pages_extracted += 1

        if (i + 1) % 50 == 0:
            logger.info("  %s: page %d/%d", filename, i + 1, page_count)

    # ── Step 14: Write meta.json ──────────────────────────────────
    meta = {
        'hash': file_hash,
        'filename': filename,
        'source_type': 'pdf',
        'page_count': page_count,
        'pages_extracted': pages_extracted,
        'ocr_pages': ocr_pages,
        'ocr_methods': ocr_methods,
        'metadata': voted,
        'metadata_provenance': provenance_record,
    }

    # Merge sidecar meta if present
    sidecar_path = os.path.join(proc_dir, 'sidecar.meta.json')
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path, encoding='utf-8') as f:
                sidecar = json.load(f)
            meta['sidecar'] = sidecar
        except Exception:
            pass

    with open(os.path.join(proc_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)

    # ── Step 15: Register in catalogue + documents ────────────────
    display_title = voted.get('title') or clean_filename_to_title(filename)
    size_bytes = os.path.getsize(pdf_proc_path)

    source = 'acquired/pdf'
    category = 'PDF'
    if meta.get('sidecar'):
        source = meta['sidecar'].get('source', source)
        category = meta['sidecar'].get('category', category)

    db.add_to_catalogue(
        file_hash, filename, pdf_proc_path, size_bytes, source, category
    )
    db.queue_document(file_hash)

    # ── Step 16: Update documents row ─────────────────────────────
    conn = db._get_conn()
    conn.execute(
        "UPDATE documents SET "
        "text_dir = ?, page_count = ?, book_title = ?, book_author = ?, "
        "metadata_provenance = ? "
        "WHERE hash = ?",
        (
            proc_dir,
            page_count,
            voted.get('title'),
            voted.get('author'),
            json.dumps(provenance_record),
            file_hash,
        )
    )
    conn.commit()

    # ── Step 17: Status = extracted ───────────────────────────────
    db.update_status(file_hash, 'extracted', pages_extracted=pages_extracted)

    logger.info(
        "PDF pre_flight complete: %s (%s) -> %d/%d pages in %s",
        file_hash[:8], display_title, pages_extracted, page_count, proc_dir,
    )

    result['action'] = 'extracted'
    return result


# ── Helpers ──────────────────────────────────────────────────────────


def _move_to_rejected(content_path, meta_path, config, filename):
    """Move an unreadable PDF to _review/rejected_pdfs/."""
    review_dir = os.path.join(
        config.get('library_root', '/mnt/library'),
        '_review', 'rejected_pdfs'
    )
    try:
        os.makedirs(review_dir, exist_ok=True)
        reject_path = os.path.join(review_dir, filename)
        shutil.move(content_path, reject_path)
        if meta_path:
            os.remove(meta_path)
        logger.info("Moved rejected PDF to %s", reject_path)
    except Exception as e:
        logger.error("Failed to move rejected PDF: %s", e)
