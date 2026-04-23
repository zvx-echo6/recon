"""
Wiki link rewriter — rewrites OSM wikipedia/wikidata/wikivoyage/appropedia
links to local Kiwix URLs where the article exists in a loaded ZIM.

Falls back silently to public URLs when article is unavailable locally.
Caches positive results only in place_cache.db.

Kiwix catalog is parsed from the OPDS Atom feed at startup and refreshed
hourly to pick up newly loaded ZIMs without a restart.

Operations note:
  - After loading a new ZIM, either restart RECON (forces fresh catalog
    fetch) or wait up to 1 hour for automatic refresh.
  - To invalidate the wiki cache (e.g. after ZIM update):
      sqlite3 /opt/recon/data/place_cache.db "DELETE FROM wiki_cache;"
"""
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, quote

import requests as http_requests

from .utils import setup_logging

logger = setup_logging('recon.wiki_rewrite')

# ── Configuration ───────────────────────────────────────────────────────

KIWIX_BASE = "http://localhost:8430"
KIWIX_PUBLIC_BASE = "https://wiki.echo6.co"
KIWIX_CATALOG_URL = f"{KIWIX_BASE}/catalog/v2/entries"
HEAD_TIMEOUT = 1.5  # seconds
CATALOG_REFRESH_INTERVAL = 3600  # 1 hour

# OPDS Atom namespace
_ATOM_NS = "http://www.w3.org/2005/Atom"

# ── ZIM catalog map ─────────────────────────────────────────────────────

_zim_map = {}        # source_type → content_path  e.g. 'wikipedia' → 'wikipedia_en_all_maxi_2026-02'
_zim_map_ts = 0.0    # last refresh timestamp

# Prefix-to-source-type mapping (order matters: longest prefix first)
_ZIM_PREFIX_MAP = [
    ('wikipedia_en_all', 'wikipedia'),
    ('appropedia_en_all', 'appropedia'),
    ('wikivoyage_en', 'wikivoyage'),
    ('wikidata_en', 'wikidata'),
]


def _discover_zims():
    """Parse Kiwix OPDS Atom catalog to map source types to content paths."""
    global _zim_map, _zim_map_ts

    try:
        resp = http_requests.get(KIWIX_CATALOG_URL, timeout=5)
        if resp.status_code != 200:
            logger.warning(f"Kiwix catalog returned HTTP {resp.status_code}")
            return

        root = ET.fromstring(resp.content)
        new_map = {}

        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            name_el = entry.find(f"{{{_ATOM_NS}}}name")
            if name_el is None:
                continue
            book_name = name_el.text or ""

            # <link type="text/html" href="/content/..."/>
            content_path = None
            for link in entry.findall(f"{{{_ATOM_NS}}}link"):
                if link.get("type") == "text/html":
                    href = link.get("href", "")
                    if href.startswith("/content/"):
                        content_path = href[len("/content/"):]
                    break

            if not content_path:
                continue

            # Match book name against known prefixes
            for prefix, source_type in _ZIM_PREFIX_MAP:
                if book_name.startswith(prefix):
                    new_map[source_type] = content_path
                    break

        _zim_map = new_map
        _zim_map_ts = time.time()
        logger.info(f"ZIM catalog refreshed: {new_map}")

    except Exception as e:
        logger.warning(f"Failed to discover ZIMs from Kiwix catalog: {e}")


def _ensure_zim_map():
    """Lazy-load and refresh ZIM map if stale."""
    if not _zim_map or (time.time() - _zim_map_ts) > CATALOG_REFRESH_INTERVAL:
        _discover_zims()


# ── Database (wiki_cache in place_cache.db) ─────────────────────────────

_db_conn = None


def _get_db():
    """Return a module-level SQLite connection to place_cache.db (lazy init)."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, 'place_cache.db')

    _db_conn = sqlite3.connect(db_path, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_cache (
            source_type TEXT NOT NULL,
            article_id  TEXT NOT NULL,
            kiwix_url   TEXT NOT NULL,
            cached_at   INTEGER NOT NULL,
            PRIMARY KEY (source_type, article_id)
        )
    """)
    _db_conn.commit()
    logger.info(f"Wiki cache table ready in {db_path}")
    return _db_conn


# ── URL classification ──────────────────────────────────────────────────

# Patterns for OSM wikipedia/wikidata tag values
_WIKI_TAG_RE = re.compile(r'^(?:en:)?(.+)$')  # "en:Title" or just "Title"
_WIKI_URL_RE = re.compile(r'https?://en\.wikipedia\.org/wiki/(.+)')
_WIKIDATA_TAG_RE = re.compile(r'^(Q\d+)$')
_WIKIDATA_URL_RE = re.compile(r'https?://(?:www\.)?wikidata\.org/wiki/(Q\d+)')
_WIKIVOYAGE_URL_RE = re.compile(r'https?://en\.wikivoyage\.org/wiki/(.+)')
_APPROPEDIA_URL_RE = re.compile(r'https?://(?:www\.)?appropedia\.org/(?:wiki/)?(.+)')


def _normalize_article_id(article_id):
    """Normalize article ID to MediaWiki/Kiwix convention: spaces → underscores."""
    return article_id.replace(' ', '_')


def classify_wiki_link(tag_name, value):
    """
    Classify an OSM extratag value into (source_type, article_id) or None.

    tag_name: the extratags key ('wikipedia', 'wikidata', etc.)
    value: the raw tag value from OSM

    Article IDs are normalized to MediaWiki convention (spaces → underscores).
    """
    if not value or not isinstance(value, str):
        return None

    value = value.strip()

    if tag_name == 'wikidata':
        m = _WIKIDATA_TAG_RE.match(value)
        if m:
            return ('wikidata', m.group(1))
        m = _WIKIDATA_URL_RE.match(value)
        if m:
            return ('wikidata', m.group(1))
        return None

    if tag_name == 'wikipedia':
        # URL form: https://en.wikipedia.org/wiki/Title
        m = _WIKI_URL_RE.match(value)
        if m:
            return ('wikipedia', _normalize_article_id(unquote(m.group(1))))
        # Tag form: "en:Title" or "Title"
        m = _WIKI_TAG_RE.match(value)
        if m:
            return ('wikipedia', _normalize_article_id(m.group(1)))
        return None

    if tag_name == 'wikivoyage':
        m = _WIKIVOYAGE_URL_RE.match(value)
        if m:
            return ('wikivoyage', _normalize_article_id(unquote(m.group(1))))
        # Plain tag: "en:Title" or "Title"
        m = _WIKI_TAG_RE.match(value)
        if m:
            return ('wikivoyage', _normalize_article_id(m.group(1)))
        return None

    if tag_name == 'appropedia':
        m = _APPROPEDIA_URL_RE.match(value)
        if m:
            return ('appropedia', _normalize_article_id(unquote(m.group(1))))
        return ('appropedia', _normalize_article_id(value))

    return None


# ── URL builders ────────────────────────────────────────────────────────

def build_kiwix_url(source_type, article_id):
    """Build a public Kiwix URL. Returns None if source_type not in ZIM map."""
    _ensure_zim_map()
    content_path = _zim_map.get(source_type)
    if not content_path:
        return None
    return f"{KIWIX_PUBLIC_BASE}/content/{content_path}/{quote(article_id, safe='/:@!$&\'()*+,;=')}"


_PUBLIC_URL_TEMPLATES = {
    'wikipedia':  "https://en.wikipedia.org/wiki/{id}",
    'wikidata':   "https://www.wikidata.org/wiki/{id}",
    'wikivoyage': "https://en.wikivoyage.org/wiki/{id}",
    'appropedia': "https://www.appropedia.org/wiki/{id}",
}


def build_public_url(source_type, article_id):
    """Build the canonical public URL for a wiki article."""
    tmpl = _PUBLIC_URL_TEMPLATES.get(source_type)
    if not tmpl:
        return None
    return tmpl.format(id=quote(article_id, safe='/:@!$&\'()*+,;='))


# ── Kiwix availability check ───────────────────────────────────────────

def check_kiwix_has_article(source_type, article_id):
    """
    Check if an article exists in local Kiwix.

    Returns (bool, url):
      - (True, kiwix_public_url) if article exists locally
      - (False, None) if not found or Kiwix unavailable

    Only positive results are cached.
    """
    # Check cache first
    db = _get_db()
    row = db.execute(
        "SELECT kiwix_url FROM wiki_cache WHERE source_type=? AND article_id=?",
        (source_type, article_id)
    ).fetchone()
    if row:
        return (True, row[0])

    # Build local HEAD URL
    _ensure_zim_map()
    content_path = _zim_map.get(source_type)
    if not content_path:
        return (False, None)

    head_url = f"{KIWIX_BASE}/content/{content_path}/{quote(article_id, safe='/:@!$&\'()*+,;=')}"

    try:
        resp = http_requests.head(head_url, timeout=HEAD_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            kiwix_url = build_kiwix_url(source_type, article_id)
            # Cache positive result
            now = int(time.time())
            db.execute("""
                INSERT OR REPLACE INTO wiki_cache (source_type, article_id, kiwix_url, cached_at)
                VALUES (?, ?, ?, ?)
            """, (source_type, article_id, kiwix_url, now))
            db.commit()
            return (True, kiwix_url)
        else:
            return (False, None)
    except Exception as e:
        logger.debug(f"Kiwix HEAD failed for {source_type}/{article_id}: {e}")
        return (False, None)


# ── Primary entry point ────────────────────────────────────────────────

def rewrite_wiki_link(tag_name, value):
    """
    Rewrite an OSM wiki tag value to a local Kiwix URL if available.

    Returns (url, 'local'|'public') or (None, None) if unrecognized.
    """
    classified = classify_wiki_link(tag_name, value)
    if not classified:
        return (value, 'original')

    source_type, article_id = classified

    # Try local Kiwix
    found, kiwix_url = check_kiwix_has_article(source_type, article_id)
    if found and kiwix_url:
        return (kiwix_url, 'local')

    # Fall back to public URL
    public_url = build_public_url(source_type, article_id)
    if public_url:
        return (public_url, 'public')

    return (value, 'original')


# ── Discovery stubs (disabled, for future activation) ───────────────────

def discover_wikivoyage_article(name, category, lat, lon):
    """
    Discover a related Wikivoyage article for a place.
    Enabled by has_wiki_discovery. Currently returns None.
    """
    return None


def discover_appropedia_article(name, category):
    """
    Discover a related Appropedia article for a place.
    Enabled by has_wiki_discovery. Currently returns None.
    """
    return None
