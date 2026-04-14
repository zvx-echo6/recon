"""
RECON Web Scraper — URL-based content ingestion.

Fetches web pages, extracts clean text, chunks into pages,
and feeds into the standard RECON enrichment pipeline.

Output format matches lib/extractor.py so the enricher
processes web content identically to PDF content.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import requests
import trafilatura

from .utils import get_config, setup_logging
from .status import StatusDB

logger = setup_logging('recon.web_scraper')

# Defaults (overridden by config.yaml web_scraper section)
DEFAULT_WORDS_PER_PAGE = 2000
DEFAULT_FETCH_TIMEOUT = 30
DEFAULT_USER_AGENT = 'RECON/1.0 (Knowledge Extraction Pipeline)'
DEFAULT_RATE_LIMIT_DELAY = 1.0


def _get_scraper_config(config=None):
    """Get web scraper settings from config, with defaults."""
    if config is None:
        config = get_config()
    ws = config.get('web_scraper', {})
    return {
        'words_per_page': ws.get('words_per_page', DEFAULT_WORDS_PER_PAGE),
        'fetch_timeout': ws.get('fetch_timeout', DEFAULT_FETCH_TIMEOUT),
        'user_agent': ws.get('user_agent', DEFAULT_USER_AGENT),
        'rate_limit_delay': ws.get('rate_limit_delay', DEFAULT_RATE_LIMIT_DELAY),
        'max_batch_size': ws.get('max_batch_size', 50),
    }


def fetch_url(url, config=None):
    """
    Fetch a URL and extract clean text + metadata using trafilatura.

    Returns dict with: text, title, author, date, description, url,
    sitename, raw_length, text_length.

    Raises ValueError if fetch or extraction fails.
    """
    sc = _get_scraper_config(config)
    logger.info(f"Fetching URL: {url}")

    try:
        response = requests.get(
            url,
            headers={'User-Agent': sc['user_agent']},
            timeout=sc['fetch_timeout'],
            allow_redirects=True
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(f"Failed to fetch {url}: {e}")

    raw_html = response.text
    if not raw_html or len(raw_html) < 100:
        raise ValueError(f"Empty or too-short response from {url}")

    text = trafilatura.extract(
        raw_html,
        include_comments=False,
        include_tables=True,
        include_links=False,
        include_images=False,
        favor_precision=False,
        deduplicate=True
    )

    if not text or len(text.strip()) < 50:
        raise ValueError(f"No meaningful text extracted from {url}")

    metadata = trafilatura.extract_metadata(raw_html)

    result = {
        'text': text.strip(),
        'title': '',
        'author': '',
        'date': '',
        'description': '',
        'url': url,
        'sitename': '',
        'raw_length': len(raw_html),
        'text_length': len(text),
    }

    if metadata:
        result['title'] = metadata.title or ''
        result['author'] = metadata.author or ''
        result['date'] = metadata.date or ''
        result['description'] = metadata.description or ''
        result['sitename'] = metadata.sitename or ''

    if not result['title']:
        result['title'] = _title_from_url(url)

    logger.info(f"Extracted {result['text_length']} chars from {url} — \"{result['title']}\"")
    return result


def _title_from_url(url):
    """Generate a readable title from a URL as fallback."""
    parsed = urlparse(url)
    path = unquote(parsed.path).strip('/')
    if path:
        segment = path.split('/')[-1]
        segment = re.sub(r'[-_]', ' ', segment)
        segment = re.sub(r'\.\w+$', '', segment)
        return segment.title() if segment else parsed.netloc
    return parsed.netloc


def chunk_text(text, words_per_page=DEFAULT_WORDS_PER_PAGE):
    """
    Split text into page-sized chunks for enrichment windows.

    Breaks at paragraph boundaries. Each chunk is ~words_per_page words.
    Returns list of strings (each is one "page").
    """
    paragraphs = text.split('\n\n')
    pages = []
    current_page = []
    current_words = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_words = len(para.split())

        if para_words > words_per_page * 1.5:
            if current_page:
                pages.append('\n\n'.join(current_page))
                current_page = []
                current_words = 0

            sentences = re.split(r'(?<=[.!?])\s+', para)
            for sentence in sentences:
                sentence_words = len(sentence.split())
                if current_words + sentence_words > words_per_page and current_page:
                    pages.append('\n\n'.join(current_page))
                    current_page = [sentence]
                    current_words = sentence_words
                else:
                    current_page.append(sentence)
                    current_words += sentence_words
        elif current_words + para_words > words_per_page and current_page:
            pages.append('\n\n'.join(current_page))
            current_page = [para]
            current_words = para_words
        else:
            current_page.append(para)
            current_words += para_words

    if current_page:
        pages.append('\n\n'.join(current_page))

    if not pages:
        pages = [text]

    return pages


def _content_hash(text):
    """MD5 hash of text content — same hash type as PDF pipeline."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def _display_filename(url):
    """Create a display filename from a URL."""
    parsed = urlparse(url)
    name = f"{parsed.netloc}_{parsed.path.strip('/').replace('/', '_')}"
    name = re.sub(r'[^\w._-]', '_', name)[:200]
    if not name.endswith('.html'):
        name += '.html'
    return name


def ingest_url(url, category='Web', source='web', config=None):
    """
    Full URL ingestion: fetch -> extract -> chunk -> save -> catalogue -> queue as extracted.

    Returns dict with hash, title, page_count, status.
    Raises ValueError on failure.
    """
    if config is None:
        config = get_config()
    sc = _get_scraper_config(config)
    db = StatusDB()

    # Fetch and extract
    extracted = fetch_url(url, config)

    # Hash the extracted text content
    doc_hash = _content_hash(extracted['text'])

    # Check for duplicate in catalogue
    conn = db._get_conn()
    existing = conn.execute("SELECT * FROM catalogue WHERE hash = ?", (doc_hash,)).fetchone()
    if existing:
        # Also check documents table for status
        doc = db.get_document(doc_hash)
        existing_status = doc['status'] if doc else existing['status']
        logger.info(f"Duplicate content (hash {doc_hash[:12]}...) — already exists as '{existing['filename']}'")
        return {
            'hash': doc_hash,
            'status': 'duplicate',
            'title': doc.get('book_title', '') if doc else existing['filename'],
            'existing_status': existing_status,
        }

    # Chunk into pages
    pages = chunk_text(extracted['text'], sc['words_per_page'])

    # Save text files in extractor-compatible format:
    # data/text/{hash}/page_0001.txt, page_0002.txt, ... + meta.json
    text_dir = os.path.join(config['paths']['text'], doc_hash)
    os.makedirs(text_dir, exist_ok=True)

    for i, page_text in enumerate(pages, 1):
        page_file = os.path.join(text_dir, f"page_{i:04d}.txt")
        with open(page_file, 'w', encoding='utf-8') as f:
            f.write(page_text)

    meta = {
        'hash': doc_hash,
        'source_type': 'web',
        'url': url,
        'title': extracted['title'],
        'author': extracted['author'],
        'date': extracted['date'],
        'description': extracted['description'],
        'sitename': extracted['sitename'],
        'page_count': len(pages),
        'text_length': extracted['text_length'],
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(text_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    display_name = _display_filename(url)

    # Add to catalogue
    db.add_to_catalogue(doc_hash, display_name, url, extracted['text_length'], source, category)

    # Queue (creates documents entry as 'queued')
    db.queue_document(doc_hash)

    # Advance directly to 'extracted' — text is already saved, skip PDF extraction
    db.update_status(doc_hash, 'extracted',
                     page_count=len(pages),
                     pages_extracted=len(pages),
                     book_title=extracted['title'],
                     book_author=extracted['author'] or None)

    logger.info(f"Ingested URL: {url} -> {doc_hash[:12]}... ({len(pages)} pages, \"{extracted['title']}\")")

    return {
        'hash': doc_hash,
        'status': 'extracted',
        'title': extracted['title'],
        'author': extracted['author'],
        'page_count': len(pages),
        'url': url,
    }


def ingest_urls(urls, category='Web', source='web', delay=None, config=None):
    """
    Batch URL ingestion with rate limiting.
    Returns list of result dicts (one per URL).
    """
    if config is None:
        config = get_config()
    if delay is None:
        delay = _get_scraper_config(config)['rate_limit_delay']

    results = []
    total = len(urls)

    for i, url in enumerate(urls, 1):
        url = url.strip()
        if not url or url.startswith('#'):
            continue

        logger.info(f"[{i}/{total}] Processing: {url}")

        try:
            result = ingest_url(url, category=category, source=source, config=config)
            result['url'] = url
            results.append(result)
        except Exception as e:
            logger.error(f"[{i}/{total}] Failed: {url} — {e}")
            results.append({
                'url': url,
                'status': 'failed',
                'error': str(e),
            })

        if i < total and delay > 0:
            time.sleep(delay)

    succeeded = sum(1 for r in results if r.get('status') not in ('failed', 'duplicate'))
    failed = sum(1 for r in results if r.get('status') == 'failed')
    dupes = sum(1 for r in results if r.get('status') == 'duplicate')
    logger.info(f"Batch complete: {succeeded} new, {dupes} duplicates, {failed} failed out of {total}")

    return results
