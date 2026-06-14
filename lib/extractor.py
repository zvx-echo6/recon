"""
RECON Text Extractor

PDF to text via PyPDF2 -> pdftotext -> Tesseract -> Gemini Vision fallback chain.
Saves to data/text/{hash}/page_NNNN.txt (4-digit zero-padded, 1-indexed).

Safety guards:
  - Layer 1: Pre-flight size check (max_pdf_size_mb, default 200)
  - Layer 2: Per-document timeout (extract_timeout, default 300s)
  - Layer 3: Per-page timeout (page_timeout, default 30s)
  - Partial extractions saved as 'extracted' with error_message noting incompleteness

Fallback chain per page:
  1. PyPDF2 (fast, free, text-based PDFs)
  2. pdftotext/poppler (handles some PDFs PyPDF2 misses)
  3. Tesseract OCR (renders page → local OCR)
  4. Gemini Vision (renders page → cloud vision API, last resort for scanned docs)

Dependencies: PyPDF2, pdftotext (poppler-utils), pytesseract, google-generativeai
Config: processing.extract_workers, processing.max_pdf_size_mb,
        processing.extract_timeout, processing.page_timeout
"""
import base64
import re
import json
import os
import random
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from pathlib import Path

import google.generativeai as genai
from PyPDF2 import PdfReader

from .utils import get_config, content_hash, clean_filename_to_title, setup_logging
from .status import StatusDB

logger = setup_logging('recon.extractor')

# ── Gemini Vision singleton (lazy, thread-safe) ──

_vision_keys = None
_vision_key_index = 0
_vision_lock = threading.Lock()


def _get_vision_keys():
    """Load Gemini API keys once from .env (same keys the enricher uses)."""
    global _vision_keys
    if _vision_keys is not None:
        return _vision_keys

    with _vision_lock:
        if _vision_keys is not None:
            return _vision_keys

        keys = []
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key_name, val = line.split('=', 1)
                    val = val.strip().strip('"').strip("'")
                    if key_name.strip().startswith('GEMINI_KEY_') and val != 'PASTE_KEY_HERE':
                        keys.append(val)

        _vision_keys = keys
        if keys:
            logger.info(f"Gemini vision OCR: {len(keys)} API key(s) available")
        else:
            logger.warning("No Gemini API keys found — vision OCR fallback disabled")
        return keys


def _next_vision_key():
    """Round-robin through available Gemini keys."""
    global _vision_key_index
    keys = _get_vision_keys()
    if not keys:
        return None
    with _vision_lock:
        key = keys[_vision_key_index % len(keys)]
        _vision_key_index += 1
    return key


def _is_transient(error_str):
    """Classify whether an error is transient (worth retrying)."""
    s = error_str.lower()
    transient_signals = ['429', 'resource_exhausted', 'quota', 'rate',
                         '500', '503', 'unavailable', 'timeout',
                         'connection', 'reset by peer', 'broken pipe']
    return any(sig in s for sig in transient_signals)


def _text_quality_ok(text, min_length=50):
    """Check if extracted text meets quality thresholds.

    Beyond the basic length check, validates:
    - Word-boundary ratio: at least 60% of tokens should be real words (2+ alpha chars)
    - Concatenation ratio: lowercase-immediately-followed-by-uppercase shouldn't exceed 10% of word count

    Returns True if text passes all checks.
    """
    text = text.strip()
    if len(text) < min_length:
        return False

    words = text.split()
    if not words:
        return False

    # Word-like ratio: tokens with 2+ alphabetic characters
    word_like = sum(1 for w in words if len(re.findall(r'[a-zA-Z]', w)) >= 2)
    word_ratio = word_like / len(words)
    if word_ratio < 0.60:
        return False

    # Concatenation detector: lowercase immediately followed by uppercase
    # Filter out common camelCase patterns in code (short tokens)
    concat_hits = len(re.findall(r'[a-z][A-Z]', text))
    concat_ratio = concat_hits / len(words) if words else 0
    if concat_ratio > 0.10:
        return False

    return True



def _render_page_to_png(pdf_path, page_num_1indexed, dpi=200, timeout=30):
    """Render a single PDF page to PNG bytes using pdftoppm.

    Args:
        pdf_path: Path to PDF file
        page_num_1indexed: 1-indexed page number
        dpi: Resolution (200 = readable text, reasonable file size)
        timeout: Subprocess timeout in seconds

    Returns:
        bytes or None: PNG image data, or None if render fails/blank
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, 'page')
        try:
            subprocess.run(
                ['pdftoppm', '-f', str(page_num_1indexed), '-l', str(page_num_1indexed),
                 '-png', '-r', str(dpi), pdf_path, prefix],
                capture_output=True, timeout=timeout, check=True
            )
            png_files = list(Path(tmpdir).glob('*.png'))
            if not png_files:
                return None

            img_data = png_files[0].read_bytes()

            # Skip blank pages (tiny image = solid white/blank page)
            if len(img_data) < 5000:
                return None

            return img_data

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            return None


def _try_gemini_vision(pdf_path, page_num_1indexed, page_timeout=60):
    """Last-resort OCR: render page to image, send to Gemini vision.

    Only called when PyPDF2, pdftotext, AND Tesseract all failed.

    Args:
        pdf_path: Path to PDF file
        page_num_1indexed: 1-indexed page number
        page_timeout: Max time for the render + API call

    Returns:
        str: Extracted text, or empty string if vision fails
    """
    api_key = _next_vision_key()
    if api_key is None:
        return ''

    # Render page to PNG
    img_data = _render_page_to_png(pdf_path, page_num_1indexed, timeout=min(page_timeout, 30))
    if img_data is None:
        return ''

    # Call Gemini vision with retry for transient errors
    last_exc = None
    for attempt in range(3):
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-2.5-flash-lite')
            response = model.generate_content([
                {
                    'mime_type': 'image/png',
                    'data': base64.b64encode(img_data).decode('utf-8')
                },
                "Extract ALL text from this scanned document page exactly as written. "
                "Preserve headings, lists, numbered items, tables, and paragraph structure. "
                "Return ONLY the extracted text, no commentary or markdown formatting."
            ])
            if response and response.text:
                text = response.text.strip()
                if len(text) > 10:
                    return text
            return ''

        except Exception as e:
            last_exc = e
            if not _is_transient(str(e)):
                break  # permanent error — don't retry
            if attempt < 2:
                delay = 5.0 * (2 ** attempt) + random.uniform(0, 3)
                time.sleep(delay)
                # Rotate to next key on rate limit
                api_key = _next_vision_key() or api_key

    if last_exc:
        logger.debug(f"  Vision OCR failed page {page_num_1indexed}: {last_exc}")
    return ''



def _get_page_count(pdf_path):
    """Get page count using pdfinfo (poppler) as fallback when PdfReader fails."""
    try:
        result = subprocess.run(
            ['pdfinfo', pdf_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith('Pages:'):
                    return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return 0


def _extract_page_without_reader(pdf_path, page_num_0indexed, page_timeout=30):
    """Extract text from a single page WITHOUT PyPDF2 reader.

    Used when PdfReader() fails entirely (corrupt/encrypted PDFs).
    Runs the pdftotext -> Tesseract -> Gemini Vision fallback chain.

    Returns:
        tuple: (text, ocr_method)
    """
    text = ''

    # Method 1: pdftotext (poppler)
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', '-f', str(page_num_0indexed + 1),
             '-l', str(page_num_0indexed + 1), pdf_path, '-'],
            capture_output=True, text=True, timeout=page_timeout
        )
        if result.returncode == 0:
            text = result.stdout
    except Exception:
        pass

    if _text_quality_ok(text):
        return text, 'pdftotext'

    # Method 2: pdftoppm + Tesseract OCR
    try:
        from PIL import Image
        import pytesseract

        result = subprocess.run(
            ['pdftoppm', '-f', str(page_num_0indexed + 1),
             '-l', str(page_num_0indexed + 1),
             '-png', '-singlefile', pdf_path, '-'],
            capture_output=True, timeout=page_timeout * 2
        )
        if result.returncode == 0 and result.stdout:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=True) as tmp:
                tmp.write(result.stdout)
                tmp.flush()
                img = Image.open(tmp.name)
                ocr_text = pytesseract.image_to_string(img)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
    except Exception:
        pass

    if _text_quality_ok(text):
        return text, 'tesseract'

    # Method 3: Gemini Vision (last resort)
    vision_text = _try_gemini_vision(pdf_path, page_num_0indexed + 1,
                                     page_timeout=page_timeout * 2)
    if len(vision_text.strip()) > len(text.strip()):
        text = vision_text

    if len(text.strip()) >= 10:
        return text, 'gemini_vision'

    return text, 'none'


# ── Core extraction functions ──

def _pypdf2_extract(reader, page_num):
    """Extract text from a PyPDF2 page object. Runs inside a thread for timeout.

    Tries default extraction first (space_width=200). If quality check fails,
    retries with space_width=100 which better detects word boundaries in
    tightly-kerned PDFs (common in Haynes/workshop manuals).

    Note: PyPDF2 3.0.1 does not support layout=True. The space_width parameter
    controls word-boundary detection tolerance. Lower values = more aggressive
    space insertion between characters.
    """
    text = reader.pages[page_num].extract_text() or ''
    if _text_quality_ok(text):
        return text

    # Retry with tighter word-boundary detection
    text_tight = reader.pages[page_num].extract_text(space_width=100.0) or ''
    if len(text_tight.strip()) >= len(text.strip()):
        return text_tight

    return text


def extract_text_from_page(reader, page_num, pdf_path, page_timeout=30):
    """Extract text from a single page with fallback chain.

    Returns:
        tuple: (text, ocr_method) where ocr_method is one of:
            'pypdf2', 'pdftotext', 'tesseract', 'gemini_vision', 'none'
    """
    # Method 1: PyPDF2 (wrapped in thread for timeout — extract_text() can hang)
    text = ''
    try:
        ex = ThreadPoolExecutor(1)
        future = ex.submit(_pypdf2_extract, reader, page_num)
        try:
            text = future.result(timeout=page_timeout)
        except FuturesTimeoutError:
            logger.warning(f"  PyPDF2 timeout on page {page_num + 1}")
            text = ''
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception:
        text = ''

    if _text_quality_ok(text):
        return text, 'pypdf2'

    # Method 2: pdftotext via subprocess (inherently timeout-safe)
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', '-f', str(page_num + 1), '-l', str(page_num + 1), pdf_path, '-'],
            capture_output=True, text=True, timeout=page_timeout
        )
        if result.returncode == 0 and len(result.stdout.strip()) > len(text.strip()):
            text = result.stdout
    except Exception:
        pass

    if _text_quality_ok(text):
        return text, 'pdftotext'

    # Method 3: pdftoppm + Tesseract OCR
    try:
        from PIL import Image
        import pytesseract

        result = subprocess.run(
            ['pdftoppm', '-f', str(page_num + 1), '-l', str(page_num + 1),
             '-png', '-singlefile', pdf_path, '-'],
            capture_output=True, timeout=page_timeout * 2
        )
        if result.returncode == 0 and result.stdout:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=True) as tmp:
                tmp.write(result.stdout)
                tmp.flush()
                img = Image.open(tmp.name)
                ocr_text = pytesseract.image_to_string(img)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
    except Exception:
        pass

    if _text_quality_ok(text):
        return text, 'tesseract'

    # Method 4: Gemini Vision (last resort — costs API calls but handles scanned docs)
    vision_text = _try_gemini_vision(pdf_path, page_num + 1, page_timeout=page_timeout * 2)
    if len(vision_text.strip()) > len(text.strip()):
        text = vision_text

    if len(text.strip()) >= 10:
        return text, 'gemini_vision'

    return text, 'none'


def extract_book_metadata(first_page_text, config):
    keys = config.get('gemini_keys', [])
    if not keys or len(first_page_text.strip()) < 20:
        return None, None

    try:
        genai.configure(api_key=keys[0])
        model = genai.GenerativeModel(
            config['gemini']['model'],
            generation_config={"response_mime_type": config['gemini']['response_mime_type']}
        )
        prompt = f"""Extract the book title and author from this first page text.
Return JSON: {{"title": "...", "author": "..."}}
If unknown, use null for that field.

Text:
{first_page_text[:3000]}"""

        response = model.generate_content(prompt)
        data = json.loads(response.text)
        return data.get('title'), data.get('author')
    except Exception as e:
        logger.warning(f"Metadata extraction failed: {e}")
        return None, None


def extract_single(file_hash, db, config):
    doc = db.get_document(file_hash)
    if not doc:
        return False

    pdf_path = doc['path']
    filename = doc['filename']
    text_dir = os.path.join(config['paths']['text'], file_hash)

    if not os.path.exists(pdf_path):
        db.mark_failed(file_hash, f"File not found: {pdf_path}")
        return False

    # Layer 1: Pre-flight size check
    proc = config.get('processing', {})
    max_size_mb = proc.get('max_pdf_size_mb', 200)
    try:
        file_size_mb = os.path.getsize(pdf_path) / 1048576
    except OSError as e:
        db.mark_failed(file_hash, f"Cannot stat file: {e}")
        return False

    if file_size_mb > max_size_mb:
        msg = f"Skipped: {file_size_mb:.0f}MB exceeds {max_size_mb}MB limit"
        logger.warning(f"SIZE SKIP: {filename} — {msg}")
        db.mark_failed(file_hash, msg)
        return False

    db.update_status(file_hash, 'extracting')

    # Layer 2/3 setup
    max_doc_seconds = proc.get('extract_timeout', 300)
    page_timeout = proc.get('page_timeout', 30)
    start_time = time.time()
    page_count = 0
    pages_extracted = 0
    skipped_pages = 0
    ocr_pages = []
    ocr_methods = {'pypdf2': 0, 'pdftotext': 0, 'tesseract': 0, 'gemini_vision': 0, 'none': 0}

    try:
        os.makedirs(text_dir, exist_ok=True)
        # Try PyPDF2 first; fall back to poppler-only extraction if it fails
        reader = None
        use_reader = True
        try:
            reader = PdfReader(pdf_path)
            page_count = len(reader.pages)
        except Exception as pdf_err:
            logger.warning(f"PdfReader failed for {filename}: {pdf_err} — using poppler fallback")
            use_reader = False
            page_count = _get_page_count(pdf_path)
            if page_count == 0:
                db.mark_failed(file_hash, f"PdfReader failed and pdfinfo returned 0 pages: {str(pdf_err)[:200]}")
                return False

        for i in range(page_count):
            # Layer 2: Check total document time budget
            elapsed = time.time() - start_time
            if elapsed > max_doc_seconds:
                msg = f"Timed out after {elapsed:.0f}s at page {i}/{page_count}"
                logger.warning(f"TIMEOUT: {filename} — {msg}")
                if pages_extracted > 0:
                    _save_partial(file_hash, db, doc, config, text_dir,
                                  page_count, pages_extracted, ocr_pages,
                                  f"Partial: {pages_extracted}/{page_count} pages "
                                  f"(timed out after {elapsed:.0f}s)",
                                  ocr_methods=ocr_methods)
                    return True
                else:
                    db.mark_failed(file_hash, msg)
                    return False

            # Layer 3: Per-page extraction with fallback chain
            try:
                if use_reader:
                    text, method = extract_text_from_page(reader, i, pdf_path, page_timeout)
                else:
                    text, method = _extract_page_without_reader(pdf_path, i, page_timeout)
                ocr_methods[method] += 1
                if method in ('tesseract', 'gemini_vision'):
                    ocr_pages.append(i + 1)
            except Exception as e:
                logger.warning(f"  Page {i+1}/{page_count} failed: {e} — skipping")
                text = ''
                skipped_pages += 1
                ocr_methods['none'] += 1

            page_file = os.path.join(text_dir, f"page_{i+1:04d}.txt")
            with open(page_file, 'w', encoding='utf-8') as f:
                f.write(text)

            if text.strip():
                pages_extracted += 1

            # Progress logging every 50 pages (more frequent since vision is slower)
            if (i + 1) % 50 == 0:
                el = time.time() - start_time
                rate = (i + 1) / el if el > 0 else 0
                vision_n = ocr_methods['gemini_vision']
                vision_note = f", {vision_n} vision" if vision_n else ""
                logger.info(f"  {filename}: page {i+1}/{page_count} "
                            f"({rate:.1f} pages/sec, {skipped_pages} skipped{vision_note})")

        # Full extraction complete — save metadata
        first_page_text = ''
        first_page_file = os.path.join(text_dir, 'page_0001.txt')
        if os.path.exists(first_page_file):
            with open(first_page_file, encoding='utf-8') as f:
                first_page_text = f.read()

        book_title, book_author = extract_book_metadata(first_page_text, config)

        if not book_title:
            book_title = clean_filename_to_title(filename)

        meta = {
            'hash': file_hash,
            'filename': filename,
            'page_count': page_count,
            'ocr_pages': ocr_pages,
            'skipped_pages': skipped_pages,
            'ocr_methods': ocr_methods,
        }
        with open(os.path.join(text_dir, 'meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)

        kwargs = {
            'page_count': page_count,
            'pages_extracted': pages_extracted,
            'book_title': book_title,
        }
        if book_author:
            kwargs['book_author'] = book_author
        if skipped_pages > 0:
            kwargs['error_message'] = (f"Partial: {pages_extracted}/{page_count} pages "
                                       f"({skipped_pages} pages timed out)")

        elapsed = time.time() - start_time
        db.update_status(file_hash, 'extracted', **kwargs)
        ocr_note = f", {len(ocr_pages)} OCR" if ocr_pages else ""
        skip_note = f", {skipped_pages} skipped" if skipped_pages > 0 else ""
        vision_note = f", {ocr_methods['gemini_vision']} vision" if ocr_methods['gemini_vision'] else ""
        logger.info(f"Extracted {filename}: {pages_extracted}/{page_count} pages "
                     f"({elapsed:.1f}s{ocr_note}{vision_note}{skip_note})")
        return True

    except Exception as e:
        logger.error(f"Extraction failed for {file_hash}: {e}\n{traceback.format_exc()}")
        if pages_extracted > 0:
            _save_partial(file_hash, db, doc, config, text_dir,
                          page_count, pages_extracted, ocr_pages,
                          f"Partial: {pages_extracted}/{page_count} pages "
                          f"({str(e)[:150]})",
                          ocr_methods=ocr_methods)
            return True
        db.mark_failed(file_hash, str(e)[:500])
        return False


def _save_partial(file_hash, db, doc, config, text_dir, page_count,
                  pages_extracted, ocr_pages, error_msg, ocr_methods=None):
    """Save metadata and mark a partial extraction as 'extracted'."""
    book_title = clean_filename_to_title(doc['filename'])

    first_page_file = os.path.join(text_dir, 'page_0001.txt')
    if os.path.exists(first_page_file):
        with open(first_page_file, encoding='utf-8') as f:
            first_text = f.read()
        if len(first_text.strip()) > 20:
            title, _ = extract_book_metadata(first_text, config)
            if title:
                book_title = title

    meta = {
        'hash': file_hash,
        'filename': doc['filename'],
        'page_count': page_count,
        'ocr_pages': ocr_pages,
        'partial': True,
    }
    if ocr_methods:
        meta['ocr_methods'] = ocr_methods
    with open(os.path.join(text_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    db.update_status(file_hash, 'extracted',
                     page_count=page_count,
                     pages_extracted=pages_extracted,
                     book_title=book_title,
                     error_message=error_msg)
    logger.info(f"  Saved partial extraction: {pages_extracted}/{page_count} pages")


def run_extraction(workers=None):
    config = get_config()
    db = StatusDB()
    workers = workers or config['processing']['extract_workers']

    queued = db.get_by_status('queued')
    if not queued:
        logger.info("No queued documents to extract")
        return 0

    logger.info(f"Extracting {len(queued)} documents with {workers} workers")
    success = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_single, doc['hash'], StatusDB(), config): doc for doc in queued}
        for future in as_completed(futures):
            doc = futures[future]
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker error for {doc['hash']}: {e}")

    logger.info(f"Extraction complete: {success}/{len(queued)} succeeded")
    return success
