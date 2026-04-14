"""
RECON Utilities

Content hashing (MD5), config loading (YAML), download URL generation,
source/category derivation, logging setup, filename sanitization.

Config: Loads and caches config.yaml
"""
import hashlib
import logging
import os
import re
import unicodedata
from urllib.parse import quote

import yaml
from logging.handlers import RotatingFileHandler

_config = None


def get_config():
    global _config
    if _config is not None:
        return _config

    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
    with open(config_path) as f:
        _config = yaml.safe_load(f)

    # Load Gemini keys from .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    _config['gemini_keys'] = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    if key.startswith('GEMINI_KEY_') and val != 'PASTE_KEY_HERE':
                        _config['gemini_keys'].append(val)

    return _config


def content_hash(filepath):
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def concept_id(doc_hash, page_num, concept_index):
    raw = f"{doc_hash}:{page_num}:{concept_index}"
    h = hashlib.md5(raw.encode()).hexdigest()[:15]
    return int(h, 16)


def setup_logging(name='recon'):
    config = get_config()
    log_dir = config['paths']['logs']
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(os.path.join(log_dir, 'errors'), exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    fh = RotatingFileHandler(os.path.join(log_dir, 'recon.log'), maxBytes=10*1024*1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    eh = RotatingFileHandler(os.path.join(log_dir, 'errors', 'errors.log'), maxBytes=5*1024*1024, backupCount=3)
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    logger.addHandler(eh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


def derive_source_and_category(filepath, library_root):
    rel = os.path.relpath(filepath, library_root)
    parts = rel.split(os.sep)
    source = parts[0] if parts else 'unknown'
    category = parts[1] if len(parts) > 2 else source
    return source, category


def clean_filename_to_title(filename):
    """Convert a PDF filename into a human-readable title."""
    # Strip extension
    name = os.path.splitext(filename)[0]
    # Remove common PDF download suffixes (with or without parens)
    name = re.sub(r'[\s_]*\(?\s*PDFDrive\s*\)?\s*_?', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[\s_]*\(?\s*z-lib\.org\s*\)?\s*_?', '', name, flags=re.IGNORECASE)
    # Handle military manual prefixes: FM_23_10 -> FM 23-10, ATP_3_21 -> ATP 3-21
    name = re.sub(
        r'\b(FM|ATP|TC|TM|AR|STP|GTA|ATTP|FMFRP|ADP|ADRP)[-_](\d+)[-_](\d+)',
        lambda m: f"{m.group(1)} {m.group(2)}-{m.group(3)}",
        name
    )
    # Fix common abbreviations: U_S -> U.S., etc.
    name = re.sub(r'(?<![A-Za-z])U[_\s]S(?=[_\s]|$)', 'U.S.', name)
    # Replace underscores and hyphens with spaces (but not in manual numbers like FM 23-10)
    name = re.sub(r'(?<!\d)[-_](?!\d)', ' ', name)
    name = name.replace('_', ' ')
    # Remove bracketed years like [1990]
    year_match = re.search(r'\[(\d{4})\]', name)
    year_suffix = f" ({year_match.group(1)})" if year_match else ''
    name = re.sub(r'\s*\[\d{4}\]\s*', ' ', name)
    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    # Title-case, but preserve uppercase military abbreviations
    words = name.split()
    titled = []
    for w in words:
        if w.isupper() and len(w) >= 2:
            titled.append(w)
        elif re.match(r'^\d', w):
            titled.append(w)
        else:
            titled.append(w.capitalize() if w.islower() else w)
    name = ' '.join(titled) + year_suffix
    name = name.strip()
    if len(name) < 3:
        return os.path.splitext(filename)[0]
    return name


# ── Mojibake fix table ──────────────────────────────────────────────
_MOJIBAKE = {
    '\u00e2\u0080\u0099': "'",       # â€™ → '  (right single quote)
    '\u00e2\u0080\u0098': "'",       # â€˜ → '  (left single quote)
    '\u00e2\u0080\u009c': '"',       # â€œ → "  (left double quote)
    '\u00e2\u0080\u009d': '"',       # â€ → "   (right double quote)
    '\u00e2\u0080\u0093': '-',       # â€" → -  (en dash)
    '\u00e2\u0080\u0094': '-',       # â€" → -  (em dash)
    '\u00e2\u0080\u00a6': '...',     # â€¦ → ... (ellipsis)
    '\u00c3\u00a9': 'e',             # Ã© → e   (e-acute)
    '\u00c3\u00a8': 'e',             # Ã¨ → e   (e-grave)
    '\u00c3\u00b6': 'o',             # Ã¶ → o   (o-umlaut)
    '\u00c3\u00bc': 'u',             # Ã¼ → u   (u-umlaut)
    '\u00c3\u00a4': 'a',             # Ã¤ → a   (a-umlaut)
    '\u00c3\u00b1': 'n',             # Ã± → n   (n-tilde)
    '\u00c3\u00ad': 'i',             # Ã­ → i   (i-acute)
    '\u00c3\u00a1': 'a',             # Ã¡ → a   (a-acute)
    '\u00c3\u00ba': 'u',             # Ãº → u   (u-acute)
    '\u00c3\u00b3': 'o',             # Ã³ → o   (o-acute)
    '\u00c2\u00ae': '',              # Â® → (registered)
    '\u00c2\u00a9': '',              # Â© → (copyright)
    '\u00c2\u00ab': '"',             # Â« → "   (guillemet left)
    '\u00c2\u00bb': '"',             # Â» → "   (guillemet right)
}

# Pre-compile: replace longer sequences first to avoid partial matches
_MOJIBAKE_PATTERN = re.compile(
    '|'.join(re.escape(k) for k in sorted(_MOJIBAKE.keys(), key=len, reverse=True))
)


def sanitize_filename(filename, doc_hash=None):
    """Sanitize a PDF filename for cross-platform filesystem safety.

    Six-phase pipeline:
      1. Strip source-site metadata (Anna's Archive, PDFDrive, z-lib, torrent tags)
      2. Strip embedded identifiers (ISBN, MD5 hash, z-lib hex suffix)
      3. Fix character encoding (mojibake, NFKD normalization)
      4. Normalize structure (military prefixes, period-separated words, underscores)
      5. Clean characters (Windows-illegal, control chars, collapse whitespace)
      6. Validate and truncate (120 char max, word-boundary break)

    Args:
        filename: Original filename (with extension)
        doc_hash: Optional doc_hash to verify z-lib suffix matches

    Returns:
        Sanitized filename (with extension preserved)
    """
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    if not ext:
        ext = '.pdf'

    # ── Phase 1: Strip source-site metadata ─────────────────────────
    # Anna's Archive pattern: Title -- Authors -- Edition -- ISBN -- Hash -- Source
    segments = stem.split(' -- ')
    if len(segments) >= 3:
        stem = segments[0]
    elif len(segments) == 2:
        second = segments[1]
        if re.search(r'97[89]\d{10}|[0-9a-f]{32}|(?:19|20)\d{2}|[Aa]nna', second):
            stem = segments[0]

    # PDFDrive tags
    stem = re.sub(r'\s*\(\s*PDFDrive\s*\)\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*_PDFDrive_\s*', ' ', stem, flags=re.IGNORECASE)

    # z-lib tags
    stem = re.sub(r'\s*\(\s*z-lib\.org\s*\)\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*_z-lib\.org_\s*', ' ', stem, flags=re.IGNORECASE)

    # Torrent tags in curly braces
    stem = re.sub(r'\s*\{[A-Za-z0-9]+\}\s*', ' ', stem)

    # ── Phase 2: Strip embedded identifiers ─────────────────────────
    # ISBN-13 (with optional dashes/spaces)
    stem = re.sub(r'\s*97[89][\s-]?\d[\s-]?\d{2}[\s-]?\d{5,6}[\s-]?\d\s*', ' ', stem)
    # ISBN-10 with dashes
    stem = re.sub(r'\s*\d[\s-]\d{2}[\s-]\d{5,6}[\s-][\dXx]\s*', ' ', stem)
    # MD5 hashes (32 hex chars, standalone)
    stem = re.sub(r'\s*\b[0-9a-f]{32}\b\s*', ' ', stem)
    # z-lib 8-char hex suffix like _4d969c3c
    if doc_hash:
        # Only strip if it matches the doc_hash prefix
        match = re.search(r'_([0-9a-f]{8})$', stem)
        if match and doc_hash.startswith(match.group(1)):
            stem = stem[:match.start()]
    else:
        # Strip any trailing 8-char hex suffix after underscore
        stem = re.sub(r'_[0-9a-f]{8}$', '', stem)

    # ── Phase 3: Fix character encoding ─────────────────────────────
    # Fix known mojibake sequences
    stem = _MOJIBAKE_PATTERN.sub(lambda m: _MOJIBAKE[m.group()], stem)

    # Common single-char mojibake that slip through
    stem = stem.replace('\u00e2\u0080', '-')  # partial em/en dash mojibake
    stem = stem.replace('H_', 'H. ')  # Anna's Archive initial abbreviation pattern

    # NFKD normalize: decompose accented chars, strip combining marks
    nfkd = unicodedata.normalize('NFKD', stem)
    cleaned = []
    for ch in nfkd:
        cat = unicodedata.category(ch)
        if cat.startswith('M'):  # combining mark — skip
            continue
        if cat.startswith('C') and ch not in (' ', '\t'):  # control char — skip
            continue
        # Keep ASCII + common punctuation; drop CJK/Cyrillic/etc if not transliteratable
        cp = ord(ch)
        if cp < 128:
            cleaned.append(ch)
        elif cat.startswith('L') or cat.startswith('N'):
            # Letter or number outside ASCII — try to keep if Latin-ish
            if cp < 0x0250:  # Latin Extended range
                cleaned.append(ch)
            # else: drop CJK, Cyrillic, etc.
        elif cat.startswith('P') or cat.startswith('S'):
            # Punctuation/symbol — map to ASCII equivalent
            if ch in ('\u2018', '\u2019', '\u201a', '\u0060'):
                cleaned.append("'")
            elif ch in ('\u201c', '\u201d', '\u201e'):
                cleaned.append('"')
            elif ch in ('\u2013', '\u2014', '\u2012'):
                cleaned.append('-')
            elif ch == '\u2026':
                cleaned.append('...')
            elif ch in ('\u00ab', '\u00bb'):
                cleaned.append('"')
            else:
                cleaned.append(' ')
        elif cat.startswith('Z'):
            cleaned.append(' ')
    stem = ''.join(cleaned)

    # ── Phase 4: Normalize structure ────────────────────────────────
    # Detect URL-derived filenames — skip aggressive normalization
    is_url_derived = bool(re.match(r'[a-z0-9-]+\.[a-z]{2,}[_/]', stem))

    if not is_url_derived:
        # Military manual prefixes: FM_23_10 -> FM 23-10
        stem = re.sub(
            r'\b(FM|ATP|TC|TM|AR|STP|GTA|ATTP|FMFRP|ADP|ADRP)[-_](\d+)[-_](\d+)',
            lambda m: '{} {}-{}'.format(m.group(1), m.group(2), m.group(3)),
            stem
        )
        # Period-separated words (4+ segments = likely word-separated, not abbreviations like U.S.)
        if stem.count('.') >= 4:
            stem = re.sub(r'\.(?=[A-Za-z])', ' ', stem)

    # Underscores to spaces (always)
    stem = stem.replace('_', ' ')

    # ── Phase 5: Clean characters ───────────────────────────────────
    # Remove Windows-illegal chars and control chars
    stem = re.sub(r'[<>:"|?*\\\/]', '', stem)
    stem = re.sub(r'[\x00-\x1f\x7f]', '', stem)

    # Collapse multiple spaces, hyphens, underscores
    stem = re.sub(r' {2,}', ' ', stem)
    stem = re.sub(r'-{2,}', '-', stem)

    # Strip leading/trailing dots, spaces, dashes
    stem = stem.strip('. -')

    # ── Phase 6: Validate and truncate ──────────────────────────────
    stem = stem.strip()
    if not stem or len(stem) < 2:
        stem = 'untitled'

    max_stem = 120 - len(ext)
    if len(stem) > max_stem:
        # Break at word boundary
        truncated = stem[:max_stem]
        last_space = truncated.rfind(' ')
        if last_space > max_stem * 0.6:
            truncated = truncated[:last_space]
        stem = truncated.rstrip('. -,')

    return stem + ext


def filename_needs_sanitization(filename, doc_hash=None):
    """Return True if sanitize_filename() would change the filename."""
    return sanitize_filename(filename, doc_hash) != filename


def resolve_collisions(entries):
    """Resolve filename collisions after sanitization.

    Args:
        entries: list of dicts, each with 'sanitized_filename', 'proposed_dir', 'hash'

    Returns:
        Updated entries with collision suffixes applied where needed.
        Each entry gets 'collision' key (True/False) and possibly updated 'sanitized_filename'.
    """
    from collections import defaultdict

    # Group by (dir, lowercase filename) to find collisions
    groups = defaultdict(list)
    for i, e in enumerate(entries):
        key = (e['proposed_dir'], e['sanitized_filename'].lower())
        groups[key].append(i)

    collision_count = 0
    for key, indices in groups.items():
        if len(indices) <= 1:
            for i in indices:
                entries[i]['collision'] = False
            continue

        # Collision — add hash suffix to all but the first
        collision_count += len(indices) - 1
        entries[indices[0]]['collision'] = False

        for i in indices[1:]:
            e = entries[i]
            h6 = e['hash'][:6]
            stem, ext = os.path.splitext(e['sanitized_filename'])
            new_name = '{} [{}]{}'.format(stem, h6, ext)
            # Re-check length
            if len(new_name) > 120:
                max_stem = 120 - len(ext) - 9  # 9 = len(' [XXXXXX]')
                stem = stem[:max_stem].rstrip('. -,')
                new_name = '{} [{}]{}'.format(stem, h6, ext)
            e['sanitized_filename'] = new_name
            e['collision'] = True

    return entries, collision_count


def generate_download_url(filepath, library_root='/mnt/library', base_url='https://files.echo6.co'):
    """Generate a download/source URL from a document path.

    For web URLs (http/https): returns the URL directly -- it's already a link.
    For file paths: converts to files.echo6.co URL.
    """
    if not filepath:
        return ''

    # Web content -- path IS the source URL
    if filepath.startswith(('http://', 'https://')):
        return filepath

    # File content -- convert to files.echo6.co URL
    rel = os.path.relpath(filepath, library_root)
    parts = rel.split(os.sep)
    encoded = '/'.join(quote(p) for p in parts)
    return f"{base_url}/{encoded}"
