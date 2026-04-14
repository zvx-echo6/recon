"""
RECON Stream B — New Library Pipeline

Handles file acquisition, standardized naming (from enriched book_title),
collision resolution, and library placement for new and existing documents.

Two-phase ingest for new files:
  Phase A (ingest_acquire): _acquired/ -> _ingest/ (preserve original name, queue for processing)
  Phase B (ingest_place): _ingest/ -> library (after enrichment populates book_title)

Migration for existing files:
  migrate_civil_org(): Rename/move enriched Civil Org docs to standardized filenames

All moves are logged to file_operations table for audit and rollback.
"""
import json
import logging
import os
import re
import shutil
import signal
import time

from .utils import (
    content_hash,
    generate_download_url,
    sanitize_filename,
    get_config,
)
from .organizer import (
    DOMAIN_FOLDERS,
    normalize_folder_name,
    determine_dominant_domain,
)
from .status import StatusDB

logger = logging.getLogger('recon.pipeline')

# ── Filename Helpers ─────────────────────────────────────────────────────


def pipeline_sanitize_filename(title_or_filename, doc_hash=None):
    """Sanitize a title/filename and convert spaces to underscores.

    Primary input should be book_title + ".pdf" from documents table.
    Falls back to raw filename if title is empty.

    Returns sanitized filename with underscores instead of spaces.
    """
    if not title_or_filename or not title_or_filename.strip():
        return None

    # Ensure .pdf extension
    if not title_or_filename.lower().endswith('.pdf'):
        title_or_filename = title_or_filename + '.pdf'

    # Use existing sanitizer for base cleanup
    sanitized = sanitize_filename(title_or_filename, doc_hash=doc_hash)

    # Convert spaces to underscores
    stem, ext = os.path.splitext(sanitized)
    stem = stem.replace(' ', '_')
    # Collapse multiple underscores
    stem = re.sub(r'_{2,}', '_', stem)
    # Strip leading/trailing underscores
    stem = stem.strip('_')

    if not stem:
        return None

    return stem + ext


def extract_author_last_name(book_author):
    """Extract the last name from a book_author string.

    "John Smith" -> "Smith"
    "Smith, John" -> "Smith"
    Returns None for empty, organizational, or unparse-able authors.
    """
    if not book_author:
        return None

    author = book_author.strip()

    # Skip non-personal authors
    skip_patterns = [
        r'^\(YT\)',           # YouTube-sourced
        r'^Wikipedia',
        r'^null$',
        r'^unknown$',
        r'^N/A$',
        r'^Various',
        r'^Staff',
        r'^Anonymous',
        r'Department\b',
        r'Committee\b',
        r'Organization\b',
        r'Institute\b',
        r'Association\b',
        r'Foundation\b',
        r'University\b',
        r'Government\b',
        r'Bureau\b',
        r'Agency\b',
        r'Office\b',
        r'Division\b',
        r'Center\b',
        r'Centre\b',
        r'Ministry\b',
        r'Commission\b',
        r'Council\b',
        r'Board\b',
        r'Corps\b',
        r'Army\b',
        r'Navy\b',
        r'Air Force\b',
        r'Marine\b',
        r'U\.?S\.?\b',
        r'United States\b',
    ]
    for pattern in skip_patterns:
        if re.search(pattern, author, re.IGNORECASE):
            return None

    # "Last, First" format
    if ',' in author:
        parts = author.split(',')
        last = parts[0].strip()
        if last and len(last) > 1:
            return last

    # "First Last" or "First Middle Last" format
    parts = author.split()
    if len(parts) >= 2:
        last = parts[-1].strip()
        # Skip if last part looks like a suffix
        if last.lower() in ('jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'phd', 'md', 'do'):
            if len(parts) >= 3:
                last = parts[-2].strip()
            else:
                return None
        if last and len(last) > 1 and last[0].isupper():
            return last

    return None


def extract_year(filename, book_title=None):
    """Extract a publication year from filename or title.

    Looks for 4-digit years (1800-2099). Returns the last match
    (more likely to be publication year vs chapter/section numbers).
    """
    years = []

    for text in [filename, book_title]:
        if not text:
            continue
        matches = re.findall(r'\b(1[89]\d{2}|20\d{2})\b', text)
        years.extend(matches)

    if years:
        return years[-1]

    return None


# ── Collision Resolution ─────────────────────────────────────────────────


def resolve_collision(sanitized_name, target_dir, doc_hash, author, title, orig_filename, db):
    """Resolve filename collisions using a 4-step ladder.

    Step 1: Title.pdf (base name)
    Step 2: Title_AuthorLast.pdf (if author available)
    Step 3: Title_AuthorLast_Year.pdf (if both author AND year available)
    Step 4: -> _duplicates/ + duplicate_review table

    If existing file has same content hash, it's the same file (not a collision).

    Returns: (final_filename, collision_step, is_duplicate)
    """
    target_path = os.path.join(target_dir, sanitized_name)

    # Step 1: Base name
    if not os.path.exists(target_path):
        return (sanitized_name, 1, False)

    # Check if same file (same content hash)
    existing_hash = content_hash(target_path)
    if existing_hash == doc_hash:
        return (sanitized_name, 1, False)  # Same file, overwrite is fine

    stem, ext = os.path.splitext(sanitized_name)
    author_last = extract_author_last_name(author)

    # Step 2: Add author last name
    if author_last:
        name_with_author = f"{stem}_{author_last}{ext}"
        target_path_2 = os.path.join(target_dir, name_with_author)
        if not os.path.exists(target_path_2):
            return (name_with_author, 2, False)

        existing_hash_2 = content_hash(target_path_2)
        if existing_hash_2 == doc_hash:
            return (name_with_author, 2, False)

        # Step 3: Add year
        year = extract_year(orig_filename, title)
        if year:
            name_with_year = f"{stem}_{author_last}_{year}{ext}"
            target_path_3 = os.path.join(target_dir, name_with_year)
            if not os.path.exists(target_path_3):
                return (name_with_year, 3, False)

            existing_hash_3 = content_hash(target_path_3)
            if existing_hash_3 == doc_hash:
                return (name_with_year, 3, False)

    # Step 4: Duplicate review
    return (sanitized_name, 4, True)


# ── File Operations ──────────────────────────────────────────────────────


def move_file(source, target, doc_hash, db, orig_filename, collision_step=1, notes=None):
    """Move a file and log the operation.

    Uses os.rename() for same-volume moves (atomic on NFS),
    falls back to shutil.move() on cross-device errors.

    Returns True on success, False on failure.
    """
    source_filename = os.path.basename(source)
    target_filename = os.path.basename(target)

    try:
        target_dir = os.path.dirname(target)
        os.makedirs(target_dir, exist_ok=True)

        try:
            os.rename(source, target)
        except OSError:
            # Cross-device — fall back to shutil.move
            shutil.move(source, target)

        # Verify
        if not os.path.exists(target):
            logger.error("Move failed — target not found after move: %s", target)
            return False

        # Log the operation
        db.log_file_operation(
            doc_hash=doc_hash,
            operation='move',
            source_path=source,
            target_path=target,
            source_filename=source_filename,
            target_filename=target_filename,
            original_filename=orig_filename,
            collision_step=collision_step,
            notes=notes,
        )

        return True

    except Exception as e:
        logger.error("Failed to move %s -> %s: %s", source, target, e)
        # Attempt rollback if target was partially written
        if os.path.exists(target) and not os.path.exists(source):
            try:
                os.rename(target, source)
                logger.info("Rolled back partial move: %s -> %s", target, source)
            except Exception:
                pass
        return False


def update_qdrant_payload(doc_hash, new_path, new_filename, original_filename, config):
    """Update Qdrant payloads for all points matching doc_hash.

    Sets download_url, filename, and original_filename.
    Returns count of points updated, 0 if Qdrant unreachable.
    """
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, MatchValue, Filter
    except ImportError:
        logger.warning("qdrant_client not installed — skipping Qdrant update")
        return 0

    library_root = config.get('library_root', '/mnt/library')
    new_url = generate_download_url(new_path, library_root)

    try:
        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=60,
        )
        collection = config['vector_db']['collection']

        hits = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="doc_hash", match=MatchValue(value=doc_hash))
            ]),
            limit=2000,
            with_payload=False,
        )
        point_ids = [p.id for p in hits[0]]

        if not point_ids:
            return 0

        payload = {
            "download_url": new_url,
            "filename": new_filename,
            "original_filename": original_filename,
        }

        qdrant.set_payload(
            collection_name=collection,
            payload=payload,
            points=point_ids,
        )

        logger.debug("Updated %d Qdrant points for %s", len(point_ids), doc_hash[:8])
        return len(point_ids)

    except Exception as e:
        logger.warning("Qdrant update failed for %s: %s", doc_hash[:8], e)
        return 0


def reverse_operation(operation_id, db, config):
    """Reverse a file operation by moving the file back.

    Returns True on success, False on failure.
    """
    op = db.get_file_operation(operation_id)
    if not op:
        logger.error("Operation %d not found", operation_id)
        return False

    if op['reversed_at']:
        logger.warning("Operation %d already reversed", operation_id)
        return False

    source = op['target_path']   # current location
    target = op['source_path']   # original location

    if not os.path.exists(source):
        logger.error("Cannot reverse — file not at target: %s", source)
        return False

    try:
        target_dir = os.path.dirname(target)
        os.makedirs(target_dir, exist_ok=True)

        try:
            os.rename(source, target)
        except OSError:
            shutil.move(source, target)

        if not os.path.exists(target):
            logger.error("Reverse failed — file not at original location: %s", target)
            return False

        # Update DB
        db.mark_operation_reversed(operation_id)

        # Update catalogue and documents back to original path
        doc_hash = op['doc_hash']
        db.update_catalogue_path(doc_hash, target, op['source_filename'])
        db.sync_document_path(doc_hash, target, op['source_filename'])

        # Clear organized_at so Phase B can re-trigger placement
        conn = db._get_conn()
        conn.execute('UPDATE documents SET organized_at = NULL WHERE hash = ?', (doc_hash,))
        conn.commit()

        # Update Qdrant
        update_qdrant_payload(
            doc_hash, target, op['source_filename'],
            op.get('original_filename'), config,
        )

        logger.info("Reversed operation %d: %s -> %s", operation_id, source, target)
        return True

    except Exception as e:
        logger.error("Failed to reverse operation %d: %s", operation_id, e)
        return False


# ── Two-Phase Ingest ─────────────────────────────────────────────────────


def ingest_acquire(filepath, db, config):
    """Phase A: Move a stable PDF from _acquired/ to _ingest/ and queue for processing.

    Returns dict with: hash, action, source, target, error
    """
    result = {'hash': None, 'action': 'skip', 'source': filepath, 'target': None, 'error': None}

    if not os.path.exists(filepath):
        result['error'] = 'File not found'
        return result

    try:
        file_hash = content_hash(filepath)
        result['hash'] = file_hash
    except Exception as e:
        result['error'] = f'Hash failed: {e}'
        return result

    # Check if hash already in catalogue (true duplicate)
    doc = db.get_document(file_hash)
    if doc and doc.get('status') == 'complete':
        # Already processed — move to _duplicates/
        dup_dir = config.get('new_pipeline', {}).get('duplicates_dir', '/mnt/library/_ingest/_duplicates')
        dup_target = os.path.join(dup_dir, os.path.basename(filepath))
        try:
            os.makedirs(dup_dir, exist_ok=True)
            shutil.move(filepath, dup_target)
        except Exception:
            pass
        result['action'] = 'duplicate'
        result['target'] = dup_target
        return result

    # Move to _ingest/ preserving original filename
    ingest_dir = config.get('new_pipeline', {}).get('ingest_dir', '/mnt/library/_ingest')
    filename = os.path.basename(filepath)
    target = os.path.join(ingest_dir, filename)

    # Handle name collision in _ingest/
    if os.path.exists(target):
        stem, ext = os.path.splitext(filename)
        target = os.path.join(ingest_dir, f"{stem}_{file_hash[:6]}{ext}")

    try:
        os.makedirs(ingest_dir, exist_ok=True)
        shutil.move(filepath, target)
    except Exception as e:
        result['error'] = f'Move to ingest failed: {e}'
        return result

    result['target'] = target

    # Add to catalogue + queue for pipeline
    size = os.path.getsize(target)
    db.add_to_catalogue(file_hash, filename, target, size, '_ingest', 'Pipeline')
    db.queue_document(file_hash)

    result['action'] = 'acquired'
    logger.info("Acquired %s -> %s [%s]", filename, target, file_hash[:8])
    return result


def ingest_place(doc_hash, db, config):
    """Phase B: Move an enriched document from _ingest/ to its final library location.

    Only called for docs where status='complete' AND organized_at IS NULL.

    Returns dict with: hash, action, source, target, domain, subdomain,
                       collision_step, error, qdrant_points_updated
    """
    result = {
        'hash': doc_hash, 'action': 'skip', 'source': None, 'target': None,
        'domain': None, 'subdomain': None, 'collision_step': None,
        'error': None, 'qdrant_points_updated': 0,
    }

    doc = db.get_document(doc_hash)
    if not doc:
        result['error'] = 'Document not found'
        return result

    current_path = doc.get('path', '')
    result['source'] = current_path

    if not current_path or not os.path.exists(current_path):
        result['error'] = 'File not found on disk'
        return result

    # Only process files in _ingest/
    ingest_dir = config.get('new_pipeline', {}).get('ingest_dir', '/mnt/library/_ingest')
    if ingest_dir not in current_path:
        result['action'] = 'skip_not_ingest'
        return result

    # Check pilot domain restriction
    pilot_domain = config.get('new_pipeline', {}).get('pilot_domain')

    # Get enriched metadata
    book_title = doc.get('book_title')
    book_author = doc.get('book_author')
    orig_filename = doc.get('filename', os.path.basename(current_path))

    # Determine domain
    data_dir = config['paths']['data']
    domain, subdomain, confidence = determine_dominant_domain(doc_hash, data_dir)
    result['domain'] = domain
    result['subdomain'] = subdomain

    if domain is None:
        result['action'] = 'skip_unclassified'
        return result

    if pilot_domain and domain != pilot_domain:
        result['action'] = 'skip_wrong_domain'
        return result

    # Build standardized filename from book_title
    if book_title:
        san_name = pipeline_sanitize_filename(book_title + '.pdf', doc_hash)
    else:
        # Fallback to current filename
        san_name = pipeline_sanitize_filename(orig_filename, doc_hash)
        logger.warning("No book_title for %s, using filename: %s", doc_hash[:8], orig_filename)

    if not san_name:
        result['error'] = 'Sanitization produced empty filename'
        return result

    # Build target directory
    library_root = config['library_root']
    domain_folder = DOMAIN_FOLDERS.get(domain, normalize_folder_name(domain))
    sub_folder = normalize_folder_name(subdomain) if subdomain else 'General'
    target_dir = os.path.join(library_root, domain_folder, sub_folder)

    # Resolve collisions
    final_name, step, is_duplicate = resolve_collision(
        san_name, target_dir, doc_hash, book_author, book_title, orig_filename, db
    )
    result['collision_step'] = step

    if is_duplicate:
        # Move to _duplicates/
        dup_dir = config.get('new_pipeline', {}).get('duplicates_dir', '/mnt/library/_ingest/_duplicates')
        dup_target = os.path.join(dup_dir, os.path.basename(current_path))
        try:
            os.makedirs(dup_dir, exist_ok=True)
            shutil.move(current_path, dup_target)
        except Exception:
            pass

        # Find the hash of the colliding file
        collision_path = os.path.join(target_dir, san_name)
        collision_hash = content_hash(collision_path) if os.path.exists(collision_path) else None

        db.queue_duplicate_review(
            doc_hash=doc_hash,
            original_filename=orig_filename,
            sanitized_filename=san_name,
            collision_with_hash=collision_hash,
            collision_path=collision_path,
            duplicate_path=dup_target,
            domain=domain,
            subdomain=subdomain,
            book_author=book_author,
            book_title=book_title,
        )

        result['action'] = 'duplicate_review'
        result['target'] = dup_target
        return result

    # Move file to final location
    target_path = os.path.join(target_dir, final_name)

    if not move_file(current_path, target_path, doc_hash, db, orig_filename, step):
        result['error'] = 'Move failed'
        return result

    result['target'] = target_path
    result['action'] = 'placed'

    # Update catalogue and documents table
    db.update_catalogue_path(doc_hash, target_path, final_name)
    db.sync_document_path(doc_hash, target_path, final_name)
    db.mark_organized(doc_hash)

    # Update Qdrant payloads
    points = update_qdrant_payload(doc_hash, target_path, final_name, orig_filename, config)
    result['qdrant_points_updated'] = points

    # Update file_operations with Qdrant count
    if points > 0:
        conn = db._get_conn()
        conn.execute(
            "UPDATE file_operations SET qdrant_points_updated = ? "
            "WHERE doc_hash = ? AND reversed_at IS NULL ORDER BY performed_at DESC LIMIT 1",
            (points, doc_hash)
        )
        conn.commit()

    logger.info("Placed %s -> %s [%s/%s, step %d, %d vectors]",
                doc_hash[:8], target_path, domain, subdomain, step, points)

    return result


def ingest_scan(db, config):
    """Run both ingest phases in a single scan cycle.

    Phase A: Scan _acquired/ for stable PDFs -> ingest_acquire()
    Phase B: Query enriched-but-unplaced docs -> ingest_place()

    Returns dict with: acquired, placed, skipped, failed, duplicates
    """
    stats = {'acquired': 0, 'placed': 0, 'skipped': 0, 'failed': 0, 'duplicates': 0}

    # Phase A: Scan _acquired/
    acquired_dir = config.get('new_pipeline', {}).get('acquired_dir', '/mnt/library/_acquired')
    mtime_stability = config.get('new_pipeline', {}).get('mtime_stability', 10)

    if os.path.isdir(acquired_dir):
        now = time.time()
        for fname in os.listdir(acquired_dir):
            if not fname.lower().endswith('.pdf'):
                continue
            filepath = os.path.join(acquired_dir, fname)
            if not os.path.isfile(filepath):
                continue

            # Check mtime stability (file is fully written)
            mtime = os.path.getmtime(filepath)
            if now - mtime < mtime_stability:
                continue

            result = ingest_acquire(filepath, db, config)
            if result['action'] == 'acquired':
                stats['acquired'] += 1
            elif result['action'] == 'duplicate':
                stats['duplicates'] += 1
            elif result.get('error'):
                stats['failed'] += 1

    # Phase B: Place enriched-but-unplaced documents from _ingest/
    ingest_dir = config.get('new_pipeline', {}).get('ingest_dir', '/mnt/library/_ingest')
    unplaced = db.get_ingest_pending(ingest_dir, limit=50)

    for doc_row in unplaced:

        result = ingest_place(doc_row['hash'], db, config)
        if result['action'] == 'placed':
            stats['placed'] += 1
        elif result['action'] == 'duplicate_review':
            stats['duplicates'] += 1
        elif result.get('error'):
            stats['failed'] += 1
        else:
            stats['skipped'] += 1

    return stats


# ── Domain Migration ─────────────────────────────────────────────────────


def migrate_domain(domain_name, db, config, dry_run=False):
    """Migrate documents in a domain directory to standardized filenames.

    Walks /mnt/library/<DomainFolder>/**/*.pdf, looks up enriched metadata
    from the documents table, and renames/moves to standardized filenames
    derived from book_title.

    Args:
        domain_name: Domain name as it appears in documents/Qdrant (e.g., "Logistics", "Civil Organization")
        db: StatusDB instance
        config: RECON config dict
        dry_run: If True, report what would happen without moving

    Returns summary dict.
    """
    library_root = config['library_root']
    data_dir = config['paths']['data']

    # Resolve domain folder name
    domain_folder = DOMAIN_FOLDERS.get(domain_name)
    if not domain_folder:
        domain_folder = normalize_folder_name(domain_name)
    domain_dir = os.path.join(library_root, domain_folder)

    stats = {
        'total': 0,
        'moved': 0,
        'renamed': 0,
        'skipped': 0,
        'already_correct': 0,
        'failed': 0,
        'duplicates': 0,
        'domain_mismatch': 0,
        'no_book_title': 0,
        'not_catalogued': 0,
        'errors': [],
    }

    if not os.path.isdir(domain_dir):
        logger.error("%s directory not found: %s", domain_name, domain_dir)
        return stats

    # Collect all PDFs
    pdf_files = []
    for root, dirs, files in os.walk(domain_dir):
        dirs[:] = [d for d in dirs if not d.startswith('_')]
        for fname in files:
            if fname.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, fname))

    stats['total'] = len(pdf_files)
    logger.info("%s migration: found %d PDFs (dry_run=%s)", domain_name, len(pdf_files), dry_run)

    for filepath in sorted(pdf_files):
        current_filename = os.path.basename(filepath)

        # Hash and look up in DB
        try:
            file_hash = content_hash(filepath)
        except Exception as e:
            stats['failed'] += 1
            stats['errors'].append(f"Hash error: {filepath}: {e}")
            continue

        doc = db.get_document(file_hash)
        if not doc:
            stats['not_catalogued'] += 1
            logger.warning("Not catalogued: %s [%s]", filepath, file_hash[:8])
            continue

        # Check dominant domain — only proceed if it matches the target domain
        domain, subdomain, confidence = determine_dominant_domain(file_hash, data_dir)

        if domain != domain_name:
            stats['domain_mismatch'] += 1
            if not dry_run:
                logger.info("Domain mismatch for %s: %s (conf=%.2f), skipping",
                            file_hash[:8], domain, confidence)
            continue

        # Get book_title for standardized filename
        book_title = doc.get('book_title')
        book_author = doc.get('book_author')
        orig_filename = current_filename

        if book_title:
            san_name = pipeline_sanitize_filename(book_title + '.pdf', file_hash)
        else:
            san_name = pipeline_sanitize_filename(current_filename, file_hash)
            stats['no_book_title'] += 1
            if not dry_run:
                logger.warning("No book_title for %s, using filename", file_hash[:8])

        if not san_name:
            stats['failed'] += 1
            stats['errors'].append(f"Empty sanitized name: {filepath}")
            continue

        # Build target directory
        sub_folder = normalize_folder_name(subdomain) if subdomain else 'General'
        target_dir = os.path.join(library_root, domain_folder, sub_folder)
        target_path = os.path.join(target_dir, san_name)

        # Check if already at correct location
        if os.path.abspath(filepath) == os.path.abspath(target_path):
            stats['already_correct'] += 1
            if not dry_run:
                update_qdrant_payload(file_hash, filepath, san_name, orig_filename, config)
                db.mark_organized(file_hash)
            continue

        # Resolve collisions
        final_name, step, is_duplicate = resolve_collision(
            san_name, target_dir, file_hash, book_author, book_title, orig_filename, db
        )

        if is_duplicate:
            stats['duplicates'] += 1
            if not dry_run:
                dup_dir = config.get('new_pipeline', {}).get('duplicates_dir', '/mnt/library/_ingest/_duplicates')
                dup_target = os.path.join(dup_dir, current_filename)
                try:
                    os.makedirs(dup_dir, exist_ok=True)
                    shutil.move(filepath, dup_target)
                except Exception:
                    pass

                collision_path = os.path.join(target_dir, san_name)
                collision_hash = content_hash(collision_path) if os.path.exists(collision_path) else None
                db.queue_duplicate_review(
                    doc_hash=file_hash,
                    original_filename=orig_filename,
                    sanitized_filename=san_name,
                    collision_with_hash=collision_hash,
                    collision_path=collision_path,
                    duplicate_path=dup_target,
                    domain=domain,
                    subdomain=subdomain,
                    book_author=book_author,
                    book_title=book_title,
                )
            else:
                logger.info("  [DRY RUN] DUPLICATE: %s -> _duplicates/ (step %d)", filepath, step)
            continue

        final_target = os.path.join(target_dir, final_name)

        if dry_run:
            action = 'rename' if os.path.dirname(filepath) == target_dir else 'move'
            logger.info("  [DRY RUN] %s: %s -> %s (step %d)",
                        action.upper(), filepath, final_target, step)
            if action == 'rename':
                stats['renamed'] += 1
            else:
                stats['moved'] += 1
            continue

        # Execute move
        if move_file(filepath, final_target, file_hash, db, orig_filename, step,
                      notes=f'{domain_name.lower().replace(" ", "_")}_migration'):
            if os.path.dirname(filepath) == target_dir:
                stats['renamed'] += 1
            else:
                stats['moved'] += 1

            db.update_catalogue_path(file_hash, final_target, final_name)
            db.sync_document_path(file_hash, final_target, final_name)
            db.mark_organized(file_hash)

            points = update_qdrant_payload(
                file_hash, final_target, final_name, orig_filename, config
            )

            if points > 0:
                conn = db._get_conn()
                conn.execute(
                    "UPDATE file_operations SET qdrant_points_updated = ? "
                    "WHERE doc_hash = ? AND reversed_at IS NULL "
                    "ORDER BY performed_at DESC LIMIT 1",
                    (points, file_hash)
                )
                conn.commit()
        else:
            stats['failed'] += 1
            stats['errors'].append(f"Move failed: {filepath}")

    logger.info("%s migration complete: total=%d, moved=%d, renamed=%d, "
                "already_correct=%d, skipped=%d, failed=%d, duplicates=%d, "
                "domain_mismatch=%d, no_book_title=%d, not_catalogued=%d",
                domain_name, stats['total'], stats['moved'], stats['renamed'],
                stats['already_correct'], stats['skipped'], stats['failed'],
                stats['duplicates'], stats['domain_mismatch'],
                stats['no_book_title'], stats['not_catalogued'])

    return stats


def migrate_civil_org(db, config, dry_run=False):
    """Migrate Civil Organization domain. Thin wrapper around migrate_domain()."""
    return migrate_domain('Civil Organization', db, config, dry_run)


_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received, finishing current cycle...")


def run_watchdog(config):
    """Polling watchdog loop for the new library pipeline.

    Runs ingest_scan() every poll_interval seconds.
    Checks new_pipeline.enabled each cycle — exits if disabled.
    Handles SIGTERM/SIGINT for graceful shutdown.
    """
    from .utils import setup_logging as _setup_logging
    _setup_logging('recon.pipeline')

    global _shutdown
    _shutdown = False

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    poll_interval = config.get('new_pipeline', {}).get('poll_interval', 60)
    db = StatusDB()

    logger.info("Pipeline watchdog started (poll=%ds)", poll_interval)

    while not _shutdown:
        # Re-read config each cycle to pick up enable/disable changes
        try:
            current_config = get_config()
        except Exception:
            current_config = config

        if not current_config.get('new_pipeline', {}).get('enabled', False):
            logger.debug("Pipeline disabled, sleeping...")
            time.sleep(poll_interval)
            continue

        try:
            stats = ingest_scan(db, current_config)
            if stats['acquired'] or stats['placed'] or stats['failed'] or stats['duplicates']:
                logger.info("Watchdog cycle: acquired=%d placed=%d failed=%d dupes=%d",
                            stats['acquired'], stats['placed'], stats['failed'], stats['duplicates'])
        except Exception as e:
            logger.error("Watchdog cycle error: %s", e)

        time.sleep(poll_interval)

    logger.info("Pipeline watchdog stopped")


# ── Library Sweep ───────────────────────────────────────────────────────

# Garbage title exact matches (case-insensitive)
GARBAGE_TITLES_EXACT = {
    'download', 'cmspage', 'plant uses',
    'natural resources conservation service',
    'national center for home food preservation',
    'home', 'index', 'about', 'contact', 'menu', 'page', 'search', 'untitled',
}

# Garbage title pattern fragments (URL/filename indicators only)
GARBAGE_TITLE_FRAGMENTS = ('.html', '.php', '.asp', '://', '\\\\')


def is_garbage_title(book_title):
    """Check if a book_title is known garbage metadata.

    Returns True if the title matches exact garbage strings, is too short,
    or contains URL/filename fragments. Bare '/' is allowed (common in titles
    like "Chemical/Biological" or "Vauxhall/Opel").
    """
    if not book_title:
        return True
    t = book_title.strip()
    if len(t) < 4:
        return True
    if t.lower() in GARBAGE_TITLES_EXACT:
        return True
    for frag in GARBAGE_TITLE_FRAGMENTS:
        if frag in t:
            return True
    return False


def _sweep_classify_file(filepath, library_root, db, config):
    """Classify a single file for the sweep. Returns a plan entry dict or None to skip."""
    data_dir = config['paths']['data']

    try:
        file_hash = content_hash(filepath)
    except Exception as e:
        return {'action': 'error', 'path': filepath, 'error': 'hash_failed: {}'.format(e)}

    # Look up in documents table
    doc = db.get_document(file_hash)

    if not doc:
        # Not catalogued — rescue to _acquired/
        return {
            'action': 'rescue',
            'path': filepath,
            'hash': file_hash,
            'target_dir': os.path.join(library_root, '_acquired'),
            'filename': os.path.basename(filepath),
        }

    status = doc.get('status', '')
    if status in ('queued', 'extracting', 'enriching', 'embedding'):
        return {'action': 'skip_in_progress', 'path': filepath, 'hash': file_hash}
    if status == 'failed':
        return {'action': 'skip_failed', 'path': filepath, 'hash': file_hash}
    if status != 'complete':
        return {'action': 'skip_other_status', 'path': filepath, 'hash': file_hash, 'status': status}

    # Complete document — classify
    book_title = doc.get('book_title')
    book_author = doc.get('book_author')
    orig_filename = doc.get('filename', os.path.basename(filepath))

    # Garbage title filter
    if is_garbage_title(book_title):
        return {
            'action': 'skip_garbage',
            'path': filepath,
            'hash': file_hash,
            'book_title': book_title,
        }

    # Determine domain
    domain, subdomain, confidence = determine_dominant_domain(file_hash, data_dir)

    if domain is None:
        # Unclassifiable — move to _unclassified/
        target_dir = os.path.join(library_root, '_unclassified')
        if book_title:
            san_name = pipeline_sanitize_filename(book_title + '.pdf', file_hash)
        else:
            san_name = pipeline_sanitize_filename(orig_filename, file_hash)
        if not san_name:
            san_name = pipeline_sanitize_filename(orig_filename, file_hash) or os.path.basename(filepath)
        return {
            'action': 'unclassified',
            'path': filepath,
            'hash': file_hash,
            'target_dir': target_dir,
            'filename': san_name,
            'book_title': book_title,
            'book_author': book_author,
            'orig_filename': orig_filename,
            'domain': None,
            'subdomain': None,
            'confidence': confidence,
        }

    # Build target path
    domain_folder = DOMAIN_FOLDERS.get(domain, normalize_folder_name(domain))
    sub_folder = normalize_folder_name(subdomain) if subdomain else 'General'
    target_dir = os.path.join(library_root, domain_folder, sub_folder)

    # Standardized filename from book_title
    if book_title:
        san_name = pipeline_sanitize_filename(book_title + '.pdf', file_hash)
    else:
        san_name = pipeline_sanitize_filename(orig_filename, file_hash)
    if not san_name:
        san_name = pipeline_sanitize_filename(orig_filename, file_hash) or os.path.basename(filepath)

    target_path = os.path.join(target_dir, san_name)

    # Already at correct location?
    if os.path.abspath(filepath) == os.path.abspath(target_path):
        return {
            'action': 'no_op',
            'path': filepath,
            'hash': file_hash,
            'domain': domain,
            'subdomain': subdomain,
        }

    return {
        'action': 'relocate',
        'path': filepath,
        'hash': file_hash,
        'target_dir': target_dir,
        'filename': san_name,
        'book_title': book_title,
        'book_author': book_author,
        'orig_filename': orig_filename,
        'domain': domain,
        'subdomain': subdomain,
        'confidence': confidence,
    }


def compute_sweep_plan(db, config):
    """Walk entire library and compute target for each file.

    Returns (plan_entries, stats) where plan_entries is a list of dicts
    and stats summarizes counts.
    """
    library_root = config['library_root']
    plan = []
    stats = {
        'total_files': 0,
        'relocate': 0,
        'rescue': 0,
        'unclassified': 0,
        'no_op': 0,
        'skip_in_progress': 0,
        'skip_failed': 0,
        'skip_garbage': 0,
        'skip_other': 0,
        'errors': 0,
        'collision_steps': {1: 0, 2: 0, 3: 0, 4: 0},
        'title_frequency': {},
    }

    logger.info("Computing sweep plan — walking %s", library_root)

    for root, dirs, files in os.walk(library_root):
        # Skip underscore-prefixed directories
        dirs[:] = [d for d in dirs if not d.startswith('_')]
        for fname in files:
            if not fname.lower().endswith('.pdf'):
                continue
            filepath = os.path.join(root, fname)
            stats['total_files'] += 1

            entry = _sweep_classify_file(filepath, library_root, db, config)
            if entry is None:
                continue

            action = entry.get('action', 'skip')

            if action == 'relocate':
                # Resolve collision with author-priority strategy
                san_name = entry['filename']
                target_dir = entry['target_dir']
                book_author = entry.get('book_author')
                book_title = entry.get('book_title')
                orig_filename = entry.get('orig_filename', fname)
                author_last = extract_author_last_name(book_author)

                # Author-priority: files WITH author skip step 1
                if author_last:
                    stem, ext = os.path.splitext(san_name)
                    name_with_author = '{}_{}{}'.format(stem, author_last, ext)
                    target_path_2 = os.path.join(target_dir, name_with_author)
                    if not os.path.exists(target_path_2):
                        entry['final_name'] = name_with_author
                        entry['collision_step'] = 2
                        entry['is_duplicate'] = False
                    else:
                        # Check if same file
                        try:
                            existing_hash = content_hash(target_path_2)
                        except Exception:
                            existing_hash = None
                        if existing_hash == entry['hash']:
                            entry['final_name'] = name_with_author
                            entry['collision_step'] = 2
                            entry['is_duplicate'] = False
                        else:
                            # Try step 3: add year
                            year = extract_year(orig_filename, book_title)
                            if year:
                                name_with_year = '{}_{}_{}{}'.format(stem, author_last, year, ext)
                                target_path_3 = os.path.join(target_dir, name_with_year)
                                if not os.path.exists(target_path_3):
                                    entry['final_name'] = name_with_year
                                    entry['collision_step'] = 3
                                    entry['is_duplicate'] = False
                                else:
                                    try:
                                        existing_hash_3 = content_hash(target_path_3)
                                    except Exception:
                                        existing_hash_3 = None
                                    if existing_hash_3 == entry['hash']:
                                        entry['final_name'] = name_with_year
                                        entry['collision_step'] = 3
                                        entry['is_duplicate'] = False
                                    else:
                                        entry['final_name'] = san_name
                                        entry['collision_step'] = 4
                                        entry['is_duplicate'] = True
                            else:
                                entry['final_name'] = san_name
                                entry['collision_step'] = 4
                                entry['is_duplicate'] = True
                else:
                    # No author — use standard resolve_collision (starts at step 1)
                    final_name, step, is_dup = resolve_collision(
                        san_name, target_dir, entry['hash'],
                        book_author, book_title, orig_filename, db
                    )
                    entry['final_name'] = final_name
                    entry['collision_step'] = step
                    entry['is_duplicate'] = is_dup

                step = entry.get('collision_step', 1)
                stats['collision_steps'][step] = stats['collision_steps'].get(step, 0) + 1
                stats['relocate'] += 1

                # Track title frequency for garbage report
                bt = entry.get('book_title', '')
                if bt:
                    stats['title_frequency'][bt] = stats['title_frequency'].get(bt, 0) + 1

            elif action == 'rescue':
                stats['rescue'] += 1
            elif action == 'unclassified':
                stats['unclassified'] += 1
            elif action == 'no_op':
                stats['no_op'] += 1
            elif action == 'skip_in_progress':
                stats['skip_in_progress'] += 1
            elif action == 'skip_failed':
                stats['skip_failed'] += 1
            elif action == 'skip_garbage':
                stats['skip_garbage'] += 1
                bt = entry.get('book_title', '')
                if bt:
                    stats['title_frequency'][bt] = stats['title_frequency'].get(bt, 0) + 1
            elif action == 'error':
                stats['errors'] += 1
            else:
                stats['skip_other'] += 1

            plan.append(entry)

            # Progress logging
            if stats['total_files'] % 2000 == 0:
                logger.info("Sweep plan progress: %d files scanned...", stats['total_files'])

    logger.info("Sweep plan complete: %d files scanned", stats['total_files'])
    return plan, stats


def _save_sweep_plan(plan, stats, output_dir):
    """Save sweep plan and summary to files."""
    os.makedirs(output_dir, exist_ok=True)

    plan_file = os.path.join(output_dir, 'sweep_plan.json')
    with open(plan_file, 'w') as f:
        json.dump(plan, f, indent=2, default=str)
    logger.info("Plan saved: %s (%d entries)", plan_file, len(plan))

    # Summary
    summary_file = os.path.join(output_dir, 'sweep_summary.md')
    lines = [
        '# Library Sweep Plan Summary',
        '',
        '| Metric | Count |',
        '|--------|-------|',
        '| Total files scanned | {} |'.format(stats['total_files']),
        '| Relocate | {} |'.format(stats['relocate']),
        '| Rescue (uncataloged) | {} |'.format(stats['rescue']),
        '| Unclassified | {} |'.format(stats['unclassified']),
        '| No-op (already correct) | {} |'.format(stats['no_op']),
        '| Skip in-progress | {} |'.format(stats['skip_in_progress']),
        '| Skip failed | {} |'.format(stats['skip_failed']),
        '| Skip garbage title | {} |'.format(stats['skip_garbage']),
        '| Skip other | {} |'.format(stats['skip_other']),
        '| Errors | {} |'.format(stats['errors']),
        '',
        '## Collision Steps',
        '',
        '| Step | Count |',
        '|------|-------|',
    ]
    for step in sorted(stats['collision_steps']):
        lines.append('| {} | {} |'.format(step, stats['collision_steps'][step]))

    # Top 50 titles by frequency (garbage report)
    lines.extend([
        '',
        '## Top 50 Titles by Frequency',
        '',
        '| Count | Title |',
        '|-------|-------|',
    ])
    title_freq = sorted(stats['title_frequency'].items(), key=lambda x: -x[1])[:50]
    for title, count in title_freq:
        safe_title = title.replace('|', '\\|')[:80]
        lines.append('| {} | {} |'.format(count, safe_title))

    with open(summary_file, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    logger.info("Summary saved: %s", summary_file)

    # Garbage metadata log
    garbage_entries = [e for e in plan if e.get('action') == 'skip_garbage']
    if garbage_entries:
        garbage_file = os.path.join(output_dir, 'sweep_garbage_metadata.json')
        with open(garbage_file, 'w') as f:
            json.dump(garbage_entries, f, indent=2, default=str)
        logger.info("Garbage metadata log: %s (%d entries)", garbage_file, len(garbage_entries))

    return plan_file, summary_file


def execute_sweep_plan(db, config, plan_file, batch_size=500, checkpoint_file=None, max_entries=None):
    """Execute a sweep plan from JSON file.

    Moves files in batches with checkpointing for resume support.

    Returns stats dict.
    """
    library_root = config['library_root']

    with open(plan_file, 'r') as f:
        plan = json.load(f)

    stats = {
        'total': len(plan),
        'relocated': 0,
        'rescued': 0,
        'unclassified_moved': 0,
        'no_op_marked': 0,
        'duplicates': 0,
        'skipped': 0,
        'failed': 0,
        'qdrant_updated': 0,
    }

    # Resume from checkpoint
    start_index = 0
    if checkpoint_file and os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, 'r') as f:
                cp = json.load(f)
            start_index = cp.get('last_completed_index', 0) + 1
            logger.info("Resuming from checkpoint index %d", start_index)
        except Exception:
            pass

    if not checkpoint_file:
        checkpoint_file = plan_file.replace('.json', '_checkpoint.json')

    logger.info("Executing sweep plan: %d entries (starting at %d)", len(plan), start_index)
    end_index = min(start_index + max_entries, len(plan)) if max_entries else len(plan)

    for i in range(start_index, end_index):
        entry = plan[i]
        action = entry.get('action', 'skip')

        try:
            if action == 'relocate':
                _execute_relocate(entry, db, config, stats)
            elif action == 'rescue':
                _execute_rescue(entry, db, config, stats)
            elif action == 'unclassified':
                _execute_unclassified(entry, db, config, stats)
            elif action == 'no_op':
                _execute_noop(entry, db, config, stats)
            else:
                stats['skipped'] += 1
        except Exception as e:
            stats['failed'] += 1
            logger.error("Sweep entry %d failed (%s): %s", i, entry.get('path', '?'), e)

        # Checkpoint
        if (i + 1) % batch_size == 0:
            _save_checkpoint(checkpoint_file, i, stats)
            logger.info("Checkpoint at index %d: relocated=%d rescued=%d failed=%d",
                        i, stats['relocated'], stats['rescued'], stats['failed'])

    # Final checkpoint
    _save_checkpoint(checkpoint_file, end_index - 1, stats)
    logger.info("Sweep execution complete: %s", json.dumps(stats))
    return stats


def _execute_relocate(entry, db, config, stats):
    """Execute a relocate operation from the sweep plan."""
    filepath = entry['path']
    file_hash = entry['hash']
    target_dir = entry['target_dir']
    final_name = entry.get('final_name', entry['filename'])
    is_duplicate = entry.get('is_duplicate', False)
    collision_step = entry.get('collision_step', 1)
    book_title = entry.get('book_title')
    book_author = entry.get('book_author')
    orig_filename = entry.get('orig_filename', os.path.basename(filepath))
    domain = entry.get('domain')
    subdomain = entry.get('subdomain')

    if not os.path.exists(filepath):
        logger.warning("Sweep: source missing, skip: %s", filepath)
        stats['failed'] += 1
        return

    if is_duplicate:
        # Move to _duplicates/
        dup_dir = config.get('new_pipeline', {}).get('duplicates_dir', '/mnt/library/_ingest/_duplicates')
        dup_target = os.path.join(dup_dir, os.path.basename(filepath))
        if os.path.exists(dup_target):
            stem, ext = os.path.splitext(os.path.basename(filepath))
            dup_target = os.path.join(dup_dir, '{}_{}{}'.format(stem, file_hash[:8], ext))
        try:
            os.makedirs(dup_dir, exist_ok=True)
            shutil.move(filepath, dup_target)
        except Exception as e:
            logger.error("Sweep: duplicate move failed %s: %s", filepath, e)
            stats['failed'] += 1
            return

        collision_path = os.path.join(target_dir, entry['filename'])
        collision_hash = None
        if os.path.exists(collision_path):
            try:
                collision_hash = content_hash(collision_path)
            except Exception:
                pass

        db.queue_duplicate_review(
            doc_hash=file_hash,
            original_filename=orig_filename,
            sanitized_filename=entry['filename'],
            collision_with_hash=collision_hash,
            collision_path=collision_path,
            duplicate_path=dup_target,
            domain=domain,
            subdomain=subdomain,
            book_author=book_author,
            book_title=book_title,
        )
        stats['duplicates'] += 1
        return

    target_path = os.path.join(target_dir, final_name)

    # Re-check: if target now exists (another file moved there during sweep)
    if os.path.exists(target_path):
        try:
            existing_hash = content_hash(target_path)
        except Exception:
            existing_hash = None
        if existing_hash == file_hash:
            # Same file already there — just update DB
            db.update_catalogue_path(file_hash, target_path, final_name)
            db.sync_document_path(file_hash, target_path, final_name)
            db.mark_organized(file_hash)
            stats['no_op_marked'] += 1
            return
        else:
            # Collision appeared during execution — send to duplicate review
            dup_dir = config.get('new_pipeline', {}).get('duplicates_dir', '/mnt/library/_ingest/_duplicates')
            dup_target = os.path.join(dup_dir, '{}_{}'.format(file_hash[:8], os.path.basename(filepath)))
            try:
                os.makedirs(dup_dir, exist_ok=True)
                shutil.move(filepath, dup_target)
            except Exception as e:
                logger.error("Sweep: runtime collision move failed %s: %s", filepath, e)
                stats['failed'] += 1
                return
            db.queue_duplicate_review(
                doc_hash=file_hash,
                original_filename=orig_filename,
                sanitized_filename=final_name,
                collision_with_hash=existing_hash,
                collision_path=target_path,
                duplicate_path=dup_target,
                domain=domain,
                subdomain=subdomain,
                book_author=book_author,
                book_title=book_title,
            )
            stats['duplicates'] += 1
            return

    # Execute move
    if not move_file(filepath, target_path, file_hash, db, orig_filename,
                     collision_step, notes='sweep'):
        stats['failed'] += 1
        return

    # Update DB
    db.update_catalogue_path(file_hash, target_path, final_name)
    db.sync_document_path(file_hash, target_path, final_name)
    db.mark_organized(file_hash)

    # Update Qdrant
    points = update_qdrant_payload(file_hash, target_path, final_name, orig_filename, config)
    if points > 0:
        stats['qdrant_updated'] += points
        conn = db._get_conn()
        conn.execute(
            "UPDATE file_operations SET qdrant_points_updated = ? "
            "WHERE doc_hash = ? AND reversed_at IS NULL ORDER BY performed_at DESC LIMIT 1",
            (points, file_hash)
        )
        conn.commit()

    stats['relocated'] += 1


def _execute_rescue(entry, db, config, stats):
    """Move an uncataloged file to _acquired/ for watchdog pickup."""
    filepath = entry['path']
    file_hash = entry['hash']
    target_dir = entry['target_dir']
    filename = entry['filename']

    if not os.path.exists(filepath):
        stats['failed'] += 1
        return

    target_path = os.path.join(target_dir, filename)
    if os.path.exists(target_path):
        stem, ext = os.path.splitext(filename)
        target_path = os.path.join(target_dir, '{}_{}{}'.format(file_hash[:8], stem, ext))

    try:
        os.makedirs(target_dir, exist_ok=True)
        shutil.move(filepath, target_path)
        logger.debug("Rescued %s -> %s", filepath, target_path)
        stats['rescued'] += 1
    except Exception as e:
        logger.error("Rescue failed %s: %s", filepath, e)
        stats['failed'] += 1


def _execute_unclassified(entry, db, config, stats):
    """Move an unclassifiable file to _unclassified/."""
    filepath = entry['path']
    file_hash = entry['hash']
    target_dir = entry['target_dir']
    san_name = entry['filename']
    orig_filename = entry.get('orig_filename', os.path.basename(filepath))

    if not os.path.exists(filepath):
        stats['failed'] += 1
        return

    target_path = os.path.join(target_dir, san_name)
    if os.path.exists(target_path):
        try:
            existing_hash = content_hash(target_path)
        except Exception:
            existing_hash = None
        if existing_hash == file_hash:
            db.mark_organized(file_hash)
            stats['no_op_marked'] += 1
            return
        stem, ext = os.path.splitext(san_name)
        target_path = os.path.join(target_dir, '{}_{}{}'.format(stem, file_hash[:8], ext))

    if not move_file(filepath, target_path, file_hash, db, orig_filename,
                     notes='sweep_unclassified'):
        stats['failed'] += 1
        return

    final_name = os.path.basename(target_path)
    db.update_catalogue_path(file_hash, target_path, final_name)
    db.sync_document_path(file_hash, target_path, final_name)
    db.mark_organized(file_hash)

    points = update_qdrant_payload(file_hash, target_path, final_name, orig_filename, config)
    if points > 0:
        stats['qdrant_updated'] = stats.get('qdrant_updated', 0) + points

    stats['unclassified_moved'] += 1


def _execute_noop(entry, db, config, stats):
    """Handle a no-op: file already at correct location."""
    file_hash = entry['hash']
    filepath = entry['path']
    filename = os.path.basename(filepath)
    orig_filename = entry.get('orig_filename', filename)

    # Ensure DB is consistent
    db.update_catalogue_path(file_hash, filepath, filename)
    db.sync_document_path(file_hash, filepath, filename)
    db.mark_organized(file_hash)

    # Set original_filename in Qdrant if missing
    update_qdrant_payload(file_hash, filepath, filename, orig_filename, config)

    stats['no_op_marked'] += 1


def _save_checkpoint(checkpoint_file, index, stats):
    """Save checkpoint for resume."""
    cp = {
        'last_completed_index': index,
        'stats': stats,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    try:
        with open(checkpoint_file, 'w') as f:
            json.dump(cp, f, indent=2)
    except Exception as e:
        logger.error("Failed to save checkpoint: %s", e)


def verify_sweep(db, config):
    """Verify sweep results: check file_operations from sweep, confirm files exist.

    Returns (ok_count, discrepancies) where discrepancies is a list of issue dicts.
    """
    conn = db._get_conn()
    ops = conn.execute(
        "SELECT * FROM file_operations WHERE notes LIKE '%sweep%' AND reversed_at IS NULL "
        "ORDER BY performed_at"
    ).fetchall()

    ok = 0
    issues = []

    for op in ops:
        op = dict(op)
        target = op['target_path']
        source = op['source_path']
        doc_hash = op['doc_hash']

        # Check target exists
        if not os.path.exists(target):
            issues.append({
                'type': 'target_missing',
                'op_id': op['id'],
                'doc_hash': doc_hash,
                'target_path': target,
            })
            continue

        # Check source is gone
        if os.path.exists(source) and os.path.abspath(source) != os.path.abspath(target):
            issues.append({
                'type': 'source_still_exists',
                'op_id': op['id'],
                'doc_hash': doc_hash,
                'source_path': source,
                'target_path': target,
            })
            continue

        # Check DB consistency
        doc = db.get_document(doc_hash)
        if doc and doc.get('path') != target:
            issues.append({
                'type': 'db_path_mismatch',
                'op_id': op['id'],
                'doc_hash': doc_hash,
                'db_path': doc.get('path'),
                'target_path': target,
            })
            continue

        ok += 1

    # Domain distribution
    domain_counts = {}
    library_root = config['library_root']
    for entry in os.scandir(library_root):
        if entry.is_dir() and not entry.name.startswith('_'):
            pdf_count = 0
            for root, dirs, files in os.walk(entry.path):
                dirs[:] = [d for d in dirs if not d.startswith('_')]
                pdf_count += sum(1 for f in files if f.lower().endswith('.pdf'))
            if pdf_count > 0:
                domain_counts[entry.name] = pdf_count

    return ok, issues, domain_counts
