"""
RECON Library Organizer

After a document completes the pipeline (extract -> enrich -> embed),
this module classifies it by dominant domain and moves it into the
correct Domain/Subdomain/ folder with a sanitized filename.

Two modes:
  1. Per-document: determine_dominant_domain() from on-disk concept JSONs
  2. Bulk manifest: organize_from_manifest() using pre-built manifest JSON

Path updates trigger the existing catalogue.path_updated_at mechanism,
which sync_qdrant_paths() propagates to Qdrant payloads.
"""
import json
import logging
import os
import shutil
from collections import Counter

from .utils import sanitize_filename

logger = logging.getLogger('recon.organizer')

# ── Domain folder mapping (canonical) ───────────────────────────────────
# Keys = exact domain strings from Gemini enrichment
# Values = filesystem-safe folder names

DOMAIN_FOLDERS = {
    'Agriculture & Livestock': 'Agriculture-and-Livestock',
    'Civil Organization': 'Civil-Organization',
    'Communications': 'Communications',
    'Food Systems': 'Food-Systems',
    'Foundational Skills': 'Foundational-Skills',
    'Logistics': 'Logistics',
    'Medical': 'Medical',
    'Navigation': 'Navigation',
    'Operations': 'Operations',
    'Power Systems': 'Power-Systems',
    'Preservation & Storage': 'Preservation-and-Storage',
    'Security': 'Security',
    'Shelter & Construction': 'Shelter-and-Construction',
    'Technology': 'Technology',
    'Tools & Equipment': 'Tools-and-Equipment',
    'Vehicles': 'Vehicles',
    'Water Systems': 'Water-Systems',
    'Wilderness Skills': 'Wilderness-Skills',
}


def normalize_folder_name(name):
    """Normalize a domain/subdomain name to a folder-safe string.

    Examples:
        'Edible Plants & Foraging' -> 'Edible-Plants-and-Foraging'
        'emergency medicine' -> 'Emergency-Medicine'
    """
    if not name:
        return 'Uncategorized'
    name = name.strip()
    name = name.replace('&', 'and')
    words = name.split()
    titled = []
    for w in words:
        if w.lower() in ('and', 'of', 'the', 'to', 'for', 'in', 'on', 'at'):
            titled.append(w.lower())
        else:
            titled.append(w.capitalize())
    return '-'.join(titled)


def determine_dominant_domain(doc_hash, data_dir):
    """Determine a document's dominant domain from on-disk concept JSONs.

    Reads all /data/concepts/{hash}/window_*.json files, counts domain
    occurrences across all concepts, returns the top domain.

    Args:
        doc_hash: Document hash
        data_dir: Path to /opt/recon/data

    Returns:
        (domain, subdomain, confidence) tuple.
        domain/subdomain are strings or None.
        confidence is float 0-1 (top domain count / total concepts).
    """
    concepts_dir = os.path.join(data_dir, 'concepts', doc_hash)
    if not os.path.isdir(concepts_dir):
        return (None, None, 0.0)

    domain_counter = Counter()
    subdomain_counter = Counter()
    total_concepts = 0

    for fname in os.listdir(concepts_dir):
        if not fname.startswith('window_') or not fname.endswith('.json'):
            continue
        fpath = os.path.join(concepts_dir, fname)
        try:
            with open(fpath, 'r') as f:
                concepts = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(concepts, list):
            continue

        for concept in concepts:
            total_concepts += 1
            # domain is usually a list with one element
            dom = concept.get('domain')
            if isinstance(dom, list):
                for d in dom:
                    if isinstance(d, str):
                        domain_counter[d] += 1
            elif isinstance(dom, str):
                domain_counter[dom] += 1

            sub = concept.get('subdomain')
            if isinstance(sub, list):
                for s in sub:
                    if isinstance(s, str):
                        subdomain_counter[s] += 1
            elif isinstance(sub, str):
                subdomain_counter[sub] += 1

    if total_concepts == 0 or not domain_counter:
        return (None, None, 0.0)

    top_domains = domain_counter.most_common(2)
    dom_name = top_domains[0][0]
    dom_count = top_domains[0][1]
    confidence = dom_count / total_concepts

    # Check ambiguity
    is_ambiguous = False
    if len(top_domains) >= 2:
        dom2_count = top_domains[1][1]
        if dom2_count >= dom_count * 0.8:
            is_ambiguous = True
    if confidence < 0.4:
        is_ambiguous = True

    if is_ambiguous:
        return (None, None, confidence)

    top_sub = subdomain_counter.most_common(1)
    sub_name = top_sub[0][0] if top_sub else None

    return (dom_name, sub_name, confidence)


def _build_target_path(library_root, domain, subdomain, filename, doc_hash):
    """Build the target path for a document, handling domain mapping and collisions.

    Returns:
        (target_path, sanitized_filename) tuple
    """
    san_name = sanitize_filename(filename, doc_hash=doc_hash)

    if domain is None:
        # Unclassified — leave in place (don't move to Review folder for pipeline)
        return (None, san_name)

    domain_folder = DOMAIN_FOLDERS.get(domain)
    if not domain_folder:
        domain_folder = normalize_folder_name(domain)

    if subdomain:
        sub_folder = normalize_folder_name(subdomain)
    else:
        sub_folder = 'General'

    target_dir = os.path.join(library_root, domain_folder, sub_folder)
    target_path = os.path.join(target_dir, san_name)

    # Handle collision at target
    if os.path.exists(target_path):
        stem, ext = os.path.splitext(san_name)
        h6 = doc_hash[:6]
        new_name = '{} [{}]{}'.format(stem, h6, ext)
        if len(new_name) > 120:
            max_stem = 120 - len(ext) - 9
            stem = stem[:max_stem].rstrip('. -,')
            new_name = '{} [{}]{}'.format(stem, h6, ext)
        san_name = new_name
        target_path = os.path.join(target_dir, san_name)

    return (target_path, san_name)


def organize_document(doc_hash, db, config, dry_run=False):
    """Organize a single document: classify, rename, and move.

    Args:
        doc_hash: Document hash
        db: StatusDB instance
        config: RECON config dict
        dry_run: If True, don't actually move files

    Returns:
        dict with keys: hash, action, before_path, after_path, domain, subdomain, error
    """
    library_root = config['library_root']
    data_dir = config['paths']['data']

    result = {
        'hash': doc_hash,
        'action': 'skip',
        'before_path': None,
        'after_path': None,
        'domain': None,
        'subdomain': None,
        'error': None,
    }

    # Look up current path from catalogue
    conn = db._get_conn()
    row = conn.execute(
        "SELECT path, filename FROM catalogue WHERE hash = ?", (doc_hash,)
    ).fetchone()
    if not row:
        result['error'] = 'Not in catalogue'
        return result

    current_path = row['path']
    current_filename = row['filename']
    result['before_path'] = current_path

    # Verify file exists on disk
    if not dry_run and not os.path.exists(current_path):
        result['error'] = 'File not found on disk'
        return result

    # Determine domain from concept JSONs
    domain, subdomain, confidence = determine_dominant_domain(doc_hash, data_dir)
    result['domain'] = domain
    result['subdomain'] = subdomain

    if domain is None:
        result['action'] = 'skip_unclassified'
        return result

    # Build target path
    target_path, san_name = _build_target_path(
        library_root, domain, subdomain, current_filename, doc_hash
    )

    if target_path is None:
        result['action'] = 'skip_unclassified'
        return result

    result['after_path'] = target_path

    # Already at target?
    if os.path.abspath(current_path) == os.path.abspath(target_path):
        result['action'] = 'already_organized'
        # Still mark as organized
        if not dry_run:
            db.mark_organized(doc_hash)
        return result

    if dry_run:
        result['action'] = 'would_move'
        return result

    # Move the file
    try:
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        shutil.move(current_path, target_path)

        # Update catalogue (triggers path_updated_at for Qdrant sync)
        db.update_catalogue_path(doc_hash, target_path, san_name)
        db.mark_organized(doc_hash)

        result['action'] = 'moved'
        logger.info("Organized %s -> %s [%s/%s]",
                     doc_hash[:8], target_path, domain, subdomain)
    except Exception as e:
        result['action'] = 'error'
        result['error'] = str(e)
        logger.error("Failed to organize %s: %s", doc_hash[:8], e)

    return result


def organize_from_manifest(manifest_path, db, config, dry_run=False):
    """Bulk migration using a pre-built manifest JSON.

    The manifest is produced by recon_manifest_builder.py and contains
    entries with current_path, sanitized_path, sanitized_filename, hash, etc.

    Args:
        manifest_path: Path to manifest JSON file
        db: StatusDB instance
        config: RECON config dict
        dry_run: If True, don't actually move files

    Returns:
        dict with summary stats: moved, skipped, errors, already_organized, total
    """
    with open(manifest_path, 'r') as f:
        entries = json.load(f)

    stats = {
        'total': len(entries),
        'moved': 0,
        'skipped': 0,
        'already_organized': 0,
        'errors': 0,
        'not_found': 0,
    }

    for i, entry in enumerate(entries):
        doc_hash = entry['hash']
        current_path = entry['current_path']
        target_path = entry.get('sanitized_path', entry.get('proposed_path'))
        san_name = entry.get('sanitized_filename', entry.get('filename'))

        if not target_path or not san_name:
            stats['skipped'] += 1
            continue

        # Skip ambiguous entries
        if entry.get('ambiguous'):
            stats['skipped'] += 1
            continue

        # Already at target?
        if os.path.abspath(current_path) == os.path.abspath(target_path):
            stats['already_organized'] += 1
            if not dry_run:
                db.mark_organized(doc_hash)
            continue

        if dry_run:
            stats['moved'] += 1
            continue

        # Verify source exists
        if not os.path.exists(current_path):
            stats['not_found'] += 1
            logger.warning("Manifest: file not found: %s [%s]", current_path, doc_hash[:8])
            continue

        try:
            target_dir = os.path.dirname(target_path)
            os.makedirs(target_dir, exist_ok=True)

            # Check for collision at target (different file already there)
            if os.path.exists(target_path):
                stem, ext = os.path.splitext(san_name)
                h6 = doc_hash[:6]
                san_name = '{} [{}]{}'.format(stem, h6, ext)
                target_path = os.path.join(target_dir, san_name)

            shutil.move(current_path, target_path)

            # Update catalogue + mark organized
            db.update_catalogue_path(doc_hash, target_path, san_name)
            db.mark_organized(doc_hash)
            stats['moved'] += 1

        except Exception as e:
            stats['errors'] += 1
            logger.error("Manifest: failed to move %s: %s", doc_hash[:8], e)

        # Progress reporting
        if (i + 1) % 1000 == 0:
            logger.info("Manifest progress: %d / %d (moved=%d, errors=%d)",
                        i + 1, stats['total'], stats['moved'], stats['errors'])

    return stats
