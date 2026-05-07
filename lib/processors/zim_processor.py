"""
RECON ZIM Processor

Batch importer for ZIM files. Opens a ZIM via python-libzim, iterates
HTML articles, strips to clean text, creates processing directories,
and registers each article as "extracted" for the enricher to pick up.

This is NOT a dispatcher-style processor (no pre_flight). ZIMs contain
thousands of articles — ingestion is triggered explicitly or by the
ZIM monitor.

Usage:
    python3 -m lib.processors.zim_processor --zim-source-id 1
    python3 -m lib.processors.zim_processor --zim-source-id 1 --limit 100 --batch-size 50
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time

from lxml import html as lxml_html

sys.path.insert(0, "/opt/recon")

from lib.utils import setup_logging, get_config
from lib.status import StatusDB
from lib.web_scraper import chunk_text

logger = logging.getLogger("recon.processors.zim")

WORDS_PER_PAGE = 2000
MIN_TEXT_LENGTH = 200

# Elements to strip before text extraction
STRIP_TAGS = {'nav', 'footer', 'script', 'style', 'header', 'aside'}

# Non-English article path suffix pattern (MediaWiki ZIMs use /XX or /XXX suffixes)
# Matches paths ending in /xx where xx is a 2-3 letter lowercase language code
_LANG_SUFFIX_RE = re.compile(r'/[a-z]{2,3}$')
# Common ISO 639-1/2 language codes to filter (excludes 'en')
_NON_EN_LANGS = {
    'aa','ab','af','ak','am','an','ar','as','av','ay','az',
    'ba','be','bg','bh','bi','bm','bn','bo','br','bs',
    'ca','ce','ch','co','cr','cs','cu','cv','cy',
    'da','de','dv','dz',
    'ee','el','eo','es','et','eu',
    'fa','ff','fi','fj','fo','fr','fy',
    'ga','gd','gl','gn','gu','gv',
    'ha','he','hi','ho','hr','ht','hu','hy','hz',
    'ia','id','ie','ig','ii','ik','io','is','it','iu',
    'ja','jv',
    'ka','kg','ki','kj','kk','kl','km','kn','ko','kr','ks','ku','kv','kw','ky',
    'la','lb','lg','li','ln','lo','lt','lu','lv',
    'mg','mh','mi','mk','ml','mn','mo','mr','ms','mt','my',
    'na','nb','nd','ne','ng','nl','nn','no','nr','nv','ny',
    'oc','oj','om','or','os',
    'pa','pi','pl','ps','pt',
    'qu',
    'rm','rn','ro','ru','rw',
    'sa','sc','sd','se','sg','sh','si','sk','sl','sm','sn','so','sq','sr','ss','st','su','sv','sw',
    'ta','te','tg','th','ti','tk','tl','tn','to','tr','ts','tt','tw','ty',
    'ug','uk','ur','uz',
    've','vi','vo',
    'wa','wo',
    'xh',
    'yi','yo',
    'za','zh','zu',
}


def _text_hash(text):
    """Compute MD5 hash of text content (matching content_hash style)."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def _flatten_table(table_el):
    """Convert a <table> element to pipe-delimited text.

    Each <tr> becomes a row with cells joined by ' | '.
    Returns the formatted table as a string with blank lines around it.
    """
    rows = []
    for tr in table_el.iter('tr'):
        cells = []
        for cell in tr:
            if cell.tag in ('td', 'th'):
                cell_text = (cell.text_content() or '').strip()
                # Collapse internal whitespace in each cell
                cell_text = re.sub(r'\s+', ' ', cell_text)
                if cell_text:
                    cells.append(cell_text)
        if cells:
            rows.append(' | '.join(cells))
    if not rows:
        return ''
    return '\n'.join(rows)


def _preprocess_tree(doc):
    """Pre-process HTML tree to add delimiters before text_content() flattens it.

    Handles: <table>, <br>, <li>, <dt>, <dd> -- elements that lxml's
    text_content() would concatenate without any separators.
    """
    from lxml import etree

    # 1. Replace <table> elements with their pipe-delimited text
    for table in list(doc.iter('table')):
        formatted = _flatten_table(table)
        if formatted:
            replacement = etree.Element('div')
            replacement.text = '\n\n' + formatted + '\n\n'
            parent = table.getparent()
            if parent is not None:
                parent.replace(table, replacement)
        else:
            table.drop_tree()

    # 2. <br> -> inject newline
    for br in list(doc.iter('br')):
        br.tail = '\n' + (br.tail or '')

    # 3. <li> -> inject newline + "- " prefix
    for li in list(doc.iter('li')):
        li.text = '- ' + (li.text or '')
        li.tail = '\n' + (li.tail or '')

    # 4. <dt> -> inject newline before
    for dt in list(doc.iter('dt')):
        dt.tail = '\n' + (dt.tail or '')

    # 5. <dd> -> inject newline + indent
    for dd in list(doc.iter('dd')):
        dd.text = '  ' + (dd.text or '')
        dd.tail = '\n' + (dd.tail or '')


def _html_to_text(html_bytes):
    """Convert HTML bytes to clean text via lxml.

    Strips nav, footer, script, style elements. Decodes entities.
    Pre-processes tables, lists, and line breaks for proper delimiters.
    Normalizes whitespace.
    """
    try:
        doc = lxml_html.fromstring(html_bytes)
    except Exception:
        return ""

    # Strip unwanted elements
    for tag in STRIP_TAGS:
        for el in doc.iter(tag):
            el.drop_tree()

    # Pre-process tree: tables -> pipe-delimited, br -> newlines, li -> dashes
    _preprocess_tree(doc)

    # Extract text
    text = doc.text_content()

    # Normalize whitespace: collapse runs of spaces, normalize newlines
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def ingest_zim(zim_source_id, db, config, stop_event=None,
               batch_size=100, batch_delay=1.0, limit=None):
    """Process all articles from a ZIM file registered in zim_sources.

    - Reads zim_path from zim_sources table
    - Iterates articles, creates processing dirs, registers in DB
    - Checkpoints progress via zim_sources.last_checkpoint
    - Respects stop_event for graceful shutdown
    - Yields after each batch to avoid monopolizing resources

    Args:
        zim_source_id: ID in zim_sources table
        db: StatusDB instance
        config: RECON config dict
        stop_event: threading.Event for graceful shutdown (optional)
        batch_size: articles per batch before sleeping
        batch_delay: seconds to sleep between batches
        limit: max articles to process (None = all)

    Returns:
        dict with counts: processed, skipped, duplicates, errors
    """
    from libzim.reader import Archive

    conn = db._get_conn()

    # Load ZIM source record
    row = conn.execute(
        "SELECT * FROM zim_sources WHERE id = ?", (zim_source_id,)
    ).fetchone()
    if not row:
        logger.error("ZIM source ID %d not found", zim_source_id)
        return {'processed': 0, 'skipped': 0, 'duplicates': 0, 'errors': 0}

    zim_source = dict(row)
    zim_path = zim_source['zim_path']
    zim_filename = zim_source['zim_filename']
    zim_title = zim_source.get('title') or zim_filename

    if not os.path.isfile(zim_path):
        logger.error("ZIM file not found: %s", zim_path)
        return {'processed': 0, 'skipped': 0, 'duplicates': 0, 'errors': 0}

    logger.info("Opening ZIM: %s (%s)", zim_title, zim_filename)
    zim = Archive(zim_path)
    total_entries = zim.entry_count

    # Read checkpoint to resume from
    last_checkpoint = zim_source.get('last_checkpoint')
    start_idx = 0
    if last_checkpoint:
        try:
            start_idx = int(last_checkpoint)
            logger.info("Resuming from checkpoint: entry %d", start_idx)
        except ValueError:
            logger.warning("Invalid checkpoint value: %s, starting from 0", last_checkpoint)

    # Update status to ingesting
    conn.execute(
        "UPDATE zim_sources SET status = 'ingesting', started_at = CURRENT_TIMESTAMP WHERE id = ?",
        (zim_source_id,)
    )
    conn.commit()

    processing_root = config.get('pipeline', {}).get(
        'processing_root', '/opt/recon/data/processing'
    )

    # Get already-processed article paths for this ZIM source (dedup within ZIM)
    existing_paths = set()
    for r in conn.execute(
        "SELECT article_path FROM zim_articles WHERE zim_source_id = ?",
        (zim_source_id,)
    ).fetchall():
        existing_paths.add(r['article_path'])

    stats = {'processed': 0, 'skipped': 0, 'duplicates': 0, 'errors': 0}
    # Track what was already flushed to DB to avoid double-counting
    flushed = {'processed': 0, 'skipped': 0, 'duplicates': 0, 'errors': 0}
    batch_count = 0
    total_processed_this_run = 0
    last_entry_idx = start_idx

    for entry_idx in range(start_idx, total_entries):
        if stop_event and stop_event.is_set():
            logger.info("Stop event set, halting ZIM ingest at entry %d", entry_idx)
            break

        if limit and total_processed_this_run >= limit:
            logger.info("Reached limit of %d articles", limit)
            break

        last_entry_idx = entry_idx

        try:
            entry = zim._get_entry_by_id(entry_idx)
        except Exception:
            continue

        # Skip redirects
        if entry.is_redirect:
            continue

        try:
            item = entry.get_item()
        except Exception:
            continue

        # Skip non-HTML
        if item.mimetype != "text/html":
            continue

        article_path = entry.path
        article_title = entry.title

        # Skip if already processed in a prior run
        if article_path in existing_paths:
            continue

        # Skip non-English articles (MediaWiki translation suffix pattern)
        lang_match = _LANG_SUFFIX_RE.search(article_path)
        if lang_match and lang_match.group(0)[1:] in _NON_EN_LANGS:
            stats['skipped'] += 1
            total_processed_this_run += 1
            continue

        # Extract and clean text
        try:
            html_bytes = bytes(item.content)
            clean_text = _html_to_text(html_bytes)
        except Exception as e:
            logger.debug("HTML extraction failed for %s: %s", article_path, e)
            stats['errors'] += 1
            continue

        # Skip stubs
        if len(clean_text) < MIN_TEXT_LENGTH:
            stats['skipped'] += 1
            continue

        # Compute content hash
        file_hash = _text_hash(clean_text)

        # Deduplicate against existing catalogue
        cat_row = conn.execute(
            "SELECT hash FROM catalogue WHERE hash = ?", (file_hash,)
        ).fetchone()
        if cat_row:
            # Record in zim_articles as skipped duplicate
            conn.execute(
                """INSERT OR IGNORE INTO zim_articles
                   (zim_source_id, article_path, article_title, status, processed_at)
                   VALUES (?, ?, ?, 'skipped', CURRENT_TIMESTAMP)""",
                (zim_source_id, article_path, article_title)
            )
            stats['duplicates'] += 1
            total_processed_this_run += 1
            continue

        # Create processing directory
        proc_dir = os.path.join(processing_root, file_hash)
        try:
            os.makedirs(proc_dir, exist_ok=True)
        except Exception as e:
            logger.error("Cannot create processing dir %s: %s", proc_dir, e)
            stats['errors'] += 1
            continue

        # Split into page files
        pages = chunk_text(clean_text, WORDS_PER_PAGE)
        for i, page_text in enumerate(pages, start=1):
            page_path = os.path.join(proc_dir, "page_{:04d}.txt".format(i))
            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(page_text)

        # Write meta.json
        meta = {
            'hash': file_hash,
            'filename': article_title + '.html',
            'source_type': 'zim',
            'zim_file': zim_filename,
            'zim_source_id': zim_source_id,
            'article_title': article_title,
            'article_path': article_path,
            'page_count': len(pages),
            'text_length': len(clean_text),
        }
        with open(os.path.join(proc_dir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2)

        # Register in catalogue
        db.add_to_catalogue(
            file_hash,
            article_title + '.html',
            zim_path,        # source path is the ZIM file
            len(clean_text),  # size in bytes (text)
            'kiwix',          # source
            zim_title,        # category = ZIM title
        )

        # Queue document
        db.queue_document(file_hash)

        # Set text_dir, page_count, book_title on documents row
        # Mark organized_at immediately (ZIM articles don't get filed to library)
        conn.execute(
            "UPDATE documents SET text_dir = ?, page_count = ?, "
            "book_title = ?, organized_at = CURRENT_TIMESTAMP "
            "WHERE hash = ?",
            (proc_dir, len(pages), article_title, file_hash)
        )

        # Update status to extracted
        db.update_status(file_hash, 'extracted', pages_extracted=len(pages))

        # Record in zim_articles
        conn.execute(
            """INSERT OR IGNORE INTO zim_articles
               (zim_source_id, article_path, article_title, status, processed_at)
               VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (zim_source_id, article_path, article_title)
        )
        conn.commit()

        stats['processed'] += 1
        total_processed_this_run += 1
        batch_count += 1

        # Progress logging
        total_done = zim_source['processed_count'] + stats['processed']
        article_count = zim_source.get('article_count', 0)
        if stats['processed'] % 500 == 0 and article_count > 0:
            pct = total_done / article_count * 100
            logger.info(
                "ZIM ingest [%s]: %s/%s (%.1f%%)",
                zim_title, f"{total_done:,}", f"{article_count:,}", pct
            )

        # Batch checkpoint — flush only the delta since last flush
        if batch_count >= batch_size:
            delta_p = stats['processed'] - flushed['processed']
            delta_s = (stats['skipped'] + stats['duplicates']) - (flushed['skipped'] + flushed['duplicates'])
            delta_e = stats['errors'] - flushed['errors']
            conn.execute(
                "UPDATE zim_sources SET processed_count = processed_count + ?, "
                "skipped_count = skipped_count + ?, error_count = error_count + ?, "
                "last_checkpoint = ? WHERE id = ?",
                (delta_p, delta_s, delta_e, str(entry_idx + 1), zim_source_id)
            )
            conn.commit()
            flushed['processed'] = stats['processed']
            flushed['skipped'] = stats['skipped']
            flushed['duplicates'] = stats['duplicates']
            flushed['errors'] = stats['errors']

            batch_count = 0

            if batch_delay > 0:
                time.sleep(batch_delay)

    # Final checkpoint — flush only the unflushed delta
    final_status = 'complete'
    if limit and total_processed_this_run >= limit:
        final_status = 'ingesting'  # not done yet, just hit the limit

    delta_p = stats['processed'] - flushed['processed']
    delta_s = (stats['skipped'] + stats['duplicates']) - (flushed['skipped'] + flushed['duplicates'])
    delta_e = stats['errors'] - flushed['errors']

    conn.execute(
        "UPDATE zim_sources SET processed_count = processed_count + ?, "
        "skipped_count = skipped_count + ?, error_count = error_count + ?, "
        "last_checkpoint = ?, status = ?, completed_at = CASE WHEN ? = 'complete' THEN CURRENT_TIMESTAMP ELSE completed_at END "
        "WHERE id = ?",
        (delta_p, delta_s, delta_e, str(last_entry_idx + 1),
         final_status, final_status, zim_source_id)
    )
    conn.commit()

    logger.info(
        "ZIM ingest [%s] %s: %d processed, %d skipped, %d duplicates, %d errors",
        zim_title, final_status,
        stats['processed'], stats['skipped'], stats['duplicates'], stats['errors']
    )

    return stats


def main():
    """CLI entry point for standalone ZIM processing."""
    parser = argparse.ArgumentParser(description="RECON ZIM Processor")
    parser.add_argument('--zim-source-id', type=int, required=True,
                        help="ID from zim_sources table")
    parser.add_argument('--batch-size', type=int, default=100,
                        help="Articles per batch (default: 100)")
    parser.add_argument('--batch-delay', type=float, default=1.0,
                        help="Seconds between batches (default: 1.0)")
    parser.add_argument('--limit', type=int, default=None,
                        help="Max articles to process (default: all)")
    args = parser.parse_args()

    setup_logging('recon.processors.zim')

    config = get_config()
    db = StatusDB(config['paths']['db'])

    stats = ingest_zim(
        zim_source_id=args.zim_source_id,
        db=db,
        config=config,
        batch_size=args.batch_size,
        batch_delay=args.batch_delay,
        limit=args.limit,
    )

    print(f"\nResults: {stats['processed']} processed, {stats['skipped']} skipped, "
          f"{stats['duplicates']} duplicates, {stats['errors']} errors")


if __name__ == "__main__":
    main()
