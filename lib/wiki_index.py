"""
Wiki location index lookup.

Provides wiki summaries, URLs, and population data from the wiki_index.db
for place detail enrichment. Read-only, opened once at startup.

DB path: /opt/recon/data/wiki_index.db
"""
import os
import sqlite3

from .utils import setup_logging

logger = setup_logging('recon.wiki_index')

_db_conn = None
_zim_books = {}


def _get_db():
    """Return a module-level SQLite connection (lazy init, read-only)."""
    global _db_conn, _zim_books

    if _db_conn is not None:
        return _db_conn

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'wiki_index.db'
    )

    if not os.path.exists(db_path):
        logger.warning(f"Wiki index DB not found at {db_path}")
        return None

    try:
        # Open read-only with URI
        _db_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row

        # Load zim_books for URL construction
        rows = _db_conn.execute("SELECT book_type, public_url FROM zim_books").fetchall()
        for row in rows:
            _zim_books[row['book_type']] = row['public_url']

        logger.info(f"Wiki index DB ready at {db_path} ({len(_zim_books)} ZIM books)")
        return _db_conn
    except Exception as e:
        logger.error(f"Failed to open wiki index DB: {e}")
        return None


def lookup_wiki(place_name, osm_key, osm_value, state, country_code):
    """
    Look up wiki data for a place by exact match.

    Args:
        place_name: Name of the place (e.g., "Twin Falls")
        osm_key: OSM key (e.g., "place", "natural", "waterway")
        osm_value: OSM value (e.g., "city", "peak", "river")
        state: State/province name (may be None)
        country_code: ISO country code (e.g., "us", "ca")

    Returns:
        dict with wiki_summary, wiki_url, wikivoyage_url, wiki_population
        or None if no match found.
    """
    db = _get_db()
    if db is None:
        return None

    # Normalize inputs
    place_name = (place_name or '').strip()
    osm_key = (osm_key or '').strip().lower()
    osm_value = (osm_value or '').strip().lower()
    state = (state or '').strip()
    country_code = (country_code or '').strip().lower()

    if not place_name or not osm_key or not osm_value or not country_code:
        return None

    try:
        # Direct match query
        row = db.execute("""
            SELECT
                summary,
                wikipedia_title,
                wikivoyage_title,
                wikipedia_exists,
                wikivoyage_exists,
                wiki_population
            FROM wiki_places
            WHERE place_name = ?
              AND osm_key = ?
              AND osm_value = ?
              AND COALESCE(state, '') = ?
              AND country_code = ?
              AND wikipedia_exists = 1
            LIMIT 1
        """, (place_name, osm_key, osm_value, state, country_code)).fetchone()

        if not row:
            return None

        result = {}

        # Summary
        if row['summary']:
            result['wiki_summary'] = row['summary']

        # Wikipedia URL
        if row['wikipedia_exists'] and row['wikipedia_title'] and 'wikipedia' in _zim_books:
            base_url = _zim_books['wikipedia']
            title = row['wikipedia_title'].replace(' ', '_')
            result['wiki_url'] = f"{base_url}/A/{title}"

        # Wikivoyage URL
        if row['wikivoyage_exists'] and row['wikivoyage_title'] and 'wikivoyage' in _zim_books:
            base_url = _zim_books['wikivoyage']
            title = row['wikivoyage_title'].replace(' ', '_')
            result['wikivoyage_url'] = f"{base_url}/A/{title}"

        # Population
        if row['wiki_population']:
            result['wiki_population'] = row['wiki_population']

        return result if result else None

    except Exception as e:
        logger.warning(f"Wiki lookup error for {place_name}: {e}")
        return None


def is_available():
    """Check if the wiki index DB is available."""
    return _get_db() is not None
