"""
Overture Maps enrichment layer.

Provides lookup functions against the local PostgreSQL Overture Places database.
Two strategies:
  1. find_by_osm_id — exact match via OSM cross-reference index
  2. find_by_coords_and_name — spatial + fuzzy name fallback

Connection pool is lazy-initialized on first call. If PostgreSQL is unreachable,
functions return None gracefully (feature degrades, doesn't crash).
"""
import json
import os

import psycopg2
import psycopg2.pool

from .utils import setup_logging

logger = setup_logging('recon.overture')

_pool = None
_pool_failed = False

# Map full OSM type names to single-letter codes used in Overture sources
OSM_TYPE_MAP = {
    'N': 'n', 'W': 'w', 'R': 'r',
    'node': 'n', 'way': 'w', 'relation': 'r',
    'n': 'n', 'w': 'w', 'r': 'r',
}


def _get_pool():
    """Lazy-init the connection pool. Returns None if Postgres is unreachable."""
    global _pool, _pool_failed
    if _pool is not None:
        return _pool
    if _pool_failed:
        return None

    try:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=3,
            host=os.environ.get('OVERTURE_DB_HOST', 'localhost'),
            port=int(os.environ.get('OVERTURE_DB_PORT', '5432')),
            dbname=os.environ.get('OVERTURE_DB_NAME', 'overture'),
            user=os.environ.get('OVERTURE_DB_USER', 'overture'),
            password=os.environ.get('OVERTURE_DB_PASSWORD', ''),
            connect_timeout=5,
        )
        logger.info("Overture PostgreSQL connection pool initialized")
        return _pool
    except Exception as e:
        _pool_failed = True
        logger.warning(f"Overture PostgreSQL unavailable, enrichment disabled: {e}")
        return None


def _query(sql, params):
    """Execute a query and return the first row as a dict, or None."""
    pool = _get_pool()
    if pool is None:
        return None

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
    except Exception as e:
        logger.warning(f"Overture query error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except Exception:
                pass


def _format_result(row, match_method):
    """Convert a database row dict to the enrichment result shape."""
    if not row:
        return None

    socials = row.get('socials')
    if isinstance(socials, str):
        try:
            socials = json.loads(socials)
        except (json.JSONDecodeError, TypeError):
            socials = None

    return {
        'phone': row.get('phone'),
        'website': row.get('website'),
        'socials': socials,
        'brand_name': row.get('brand_name'),
        'brand_wikidata': row.get('brand_wikidata'),
        'basic_category': row.get('basic_category'),
        'confidence': row.get('confidence'),
        'gers_id': row.get('id'),
        'match_method': match_method,
    }


def find_by_osm_id(osm_type, osm_id):
    """
    Look up an Overture place by its OSM cross-reference.

    Args:
        osm_type: OSM type — 'N', 'W', 'R', 'node', 'way', 'relation', or single letter
        osm_id: OSM numeric ID

    Returns:
        Enrichment dict or None
    """
    type_letter = OSM_TYPE_MAP.get(osm_type)
    if not type_letter:
        return None

    row = _query(
        """SELECT id, name, basic_category, confidence,
                  phone, website, socials, brand_name, brand_wikidata
           FROM places
           WHERE osm_type = %s AND osm_id = %s
           LIMIT 1""",
        (type_letter, int(osm_id))
    )
    return _format_result(row, 'osm_xref')


def find_by_coords_and_name(lat, lon, name, radius_m=100):
    """
    Look up an Overture place by spatial proximity + fuzzy name match.

    Args:
        lat: Latitude
        lon: Longitude
        name: Place name to fuzzy-match
        radius_m: Search radius in meters (default 100)

    Returns:
        Enrichment dict or None
    """
    if not name or not lat or not lon:
        return None

    row = _query(
        """SELECT id, name, basic_category, confidence,
                  phone, website, socials, brand_name, brand_wikidata,
                  similarity(name, %s) AS sim
           FROM places
           WHERE ST_DWithin(geometry::geography, ST_MakePoint(%s, %s)::geography, %s)
             AND similarity(name, %s) > 0.4
           ORDER BY sim DESC, ST_Distance(geometry::geography, ST_MakePoint(%s, %s)::geography) ASC
           LIMIT 1""",
        (name, lon, lat, radius_m, name, lon, lat)
    )
    return _format_result(row, 'coord_name_fuzzy')
