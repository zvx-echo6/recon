"""
RECON Netsyms AddressDatabase2025 — SQLite-backed US+CA address lookup.

Provides 159.78M geocoded addresses as tier-2 between address book
(exact named locations) and Photon (full-text global geocoding).

Database: /mnt/nav/addresses/AddressDatabase2025.sqlite (read-only)
"""

import os
import re
import sqlite3
import threading

from .utils import setup_logging

logger = setup_logging('recon.netsyms')

_DB_PATH = '/mnt/nav/addresses/AddressDatabase2025.sqlite'

_conn = None
_lock = threading.Lock()
_cached_row_count = None

# US states + DC + territories, CA provinces, for free-text parsing
_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
    'DC', 'PR', 'VI', 'GU', 'AS', 'MP',
    # Canadian provinces
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE',
    'QC', 'SK', 'YT',
}

_NUMBER_RE = re.compile(r'^(\d+[\w-]*)(.*)$')


def _get_conn():
    """Lazy-open a read-only SQLite connection."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        uri = f'file:{_DB_PATH}?mode=ro'
        _conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        logger.info("Netsyms DB opened: %s", _DB_PATH)
        return _conn


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict with lat/lon keys."""
    return {
        'zipcode': row['zipcode'],
        'number': row['number'],
        'street': row['street'],
        'street2': row['street2'],
        'city': row['city'],
        'state': row['state'],
        'plus4': row['plus4'],
        'country': row['country'],
        'lat': float(row['latitude']),
        'lon': float(row['longitude']),
        'source': row['source'],
    }


def lookup_by_street(number, street, city=None, state=None,
                     zipcode=None, country=None, limit=20):
    """Match on number + street, with optional qualifiers."""
    conn = _get_conn()
    clauses = ['number = ?', 'street = ?']
    params = [str(number).strip().upper(), street.strip().upper()]

    if city:
        clauses.append('city = ?')
        params.append(city.strip().upper())
    if state:
        clauses.append('state = ?')
        params.append(state.strip().upper())
    if zipcode:
        clauses.append('zipcode = ?')
        params.append(zipcode.strip())
    if country:
        clauses.append('country = ?')
        params.append(country.strip().upper())

    sql = f"SELECT * FROM addresses WHERE {' AND '.join(clauses)} LIMIT ?"
    params.append(limit)

    with _lock:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.warning("Netsyms lookup_by_street error: %s", e)
            return []

    results = [_row_to_dict(r) for r in rows]
    logger.debug("lookup_by_street(%s, %s, city=%s, state=%s) → %d results",
                 number, street, city, state, len(results))
    return results


def lookup_free_text(query, country_hint=None):
    """Parse a free-text address and look it up."""
    q = query.strip()
    if not q:
        return []

    # Strip trailing zipcode if present
    zipcode = None
    zip_match = re.search(r'\b(\d{5})\s*$', q)
    if zip_match:
        zipcode = zip_match.group(1)
        q = q[:zip_match.start()].strip().rstrip(',').strip()

    # Strip trailing state
    tokens = re.split(r'[,\s]+', q)
    tokens = [t for t in tokens if t]
    if not tokens:
        return []

    state = None
    if len(tokens) >= 2 and tokens[-1].upper() in _STATE_CODES:
        state = tokens[-1].upper()
        tokens = tokens[:-1]

    # Leading digits → number
    number = None
    if tokens and re.match(r'^\d', tokens[0]):
        number = tokens[0]
        tokens = tokens[1:]

    if not tokens:
        # Only a number, or empty — try zipcode if we have one
        if zipcode:
            return lookup_by_zipcode(zipcode, limit=20)
        return []

    # If state was found and we have 2+ tokens remaining, last token is city
    city = None
    if state and len(tokens) >= 2:
        city = tokens[-1]
        tokens = tokens[:-1]

    street = ' '.join(tokens)

    if number:
        results = lookup_by_street(number, street, city=city, state=state,
                                   zipcode=zipcode, country=country_hint)
        if results:
            logger.debug("lookup_free_text(%r) → %d results via street match",
                         query, len(results))
            return results

    # Fallback: try zipcode only if available
    if zipcode:
        return lookup_by_zipcode(zipcode, limit=20)

    logger.debug("lookup_free_text(%r) → 0 results", query)
    return []


def lookup_by_zipcode(zipcode, limit=100):
    """Direct zipcode lookup."""
    conn = _get_conn()
    sql = "SELECT * FROM addresses WHERE zipcode = ? LIMIT ?"
    params = [zipcode.strip(), limit]

    with _lock:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.warning("Netsyms lookup_by_zipcode error: %s", e)
            return []

    results = [_row_to_dict(r) for r in rows]
    logger.debug("lookup_by_zipcode(%s) → %d results", zipcode, len(results))
    return results


def health():
    """Health check with cached row count."""
    global _cached_row_count

    try:
        file_size = os.path.getsize(_DB_PATH)
    except OSError:
        return {'ok': False, 'row_count': 0, 'file_size_bytes': 0,
                'indexed_countries': []}

    try:
        conn = _get_conn()
    except Exception:
        return {'ok': False, 'row_count': 0, 'file_size_bytes': file_size,
                'indexed_countries': []}

    if _cached_row_count is None:
        with _lock:
            if _cached_row_count is None:
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM addresses"
                    ).fetchone()
                    _cached_row_count = row['cnt']
                except sqlite3.Error:
                    _cached_row_count = 0

    with _lock:
        try:
            rows = conn.execute(
                "SELECT DISTINCT country FROM addresses"
            ).fetchall()
            countries = sorted(r['country'] for r in rows)
        except sqlite3.Error:
            countries = []

    return {
        'ok': True,
        'row_count': _cached_row_count,
        'file_size_bytes': file_size,
        'indexed_countries': countries,
    }
