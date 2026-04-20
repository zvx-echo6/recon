"""
RECON Address Book — YAML-backed saved-location lookup.

Provides named locations (home, work, etc.) that short-circuit Photon
geocoding when an exact alias match is found.

Config: /opt/recon/config/address_book.yaml
"""

import os
import threading

import yaml

from .utils import setup_logging

logger = setup_logging('recon.address_book')

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'config', 'address_book.yaml',
)

_lock = threading.Lock()
_entries: list[dict] = []
_mtime: float = 0.0


def _reload_if_changed():
    """Reload the YAML file if its mtime has changed."""
    global _entries, _mtime
    try:
        st = os.stat(_CONFIG_PATH)
    except FileNotFoundError:
        logger.warning("Address book not found: %s", _CONFIG_PATH)
        _entries = []
        _mtime = 0.0
        return

    if st.st_mtime == _mtime:
        return

    with _lock:
        # Double-check after acquiring lock
        try:
            st = os.stat(_CONFIG_PATH)
        except FileNotFoundError:
            _entries = []
            _mtime = 0.0
            return
        if st.st_mtime == _mtime:
            return

        with open(_CONFIG_PATH, 'r') as f:
            data = yaml.safe_load(f) or {}

        raw = data.get('entries', [])
        loaded = []
        for entry in raw:
            # Normalise aliases to lowercase for matching
            aliases = [a.lower() for a in entry.get('aliases', [])]
            loaded.append({
                'id': entry.get('id', ''),
                'name': entry.get('name', ''),
                'aliases': aliases,
                'address': entry.get('address', ''),
                'lat': entry.get('lat'),
                'lon': entry.get('lon'),
                'tags': entry.get('tags', []),
            })
        _entries = loaded
        _mtime = st.st_mtime
        logger.info("Address book loaded: %d entries from %s", len(_entries), _CONFIG_PATH)


def load():
    """Ensure the address book is loaded (and refreshed if the file changed)."""
    _reload_if_changed()
    return _entries


def lookup(query: str):
    """
    Look up a query against name and aliases.

    Returns dict with the matching entry plus a 'confidence' field:
      - "exact": full name or alias match
      - "partial": query is a substring of an alias or name (or vice versa)
      - None if no match
    """
    _reload_if_changed()
    q = query.strip().lower()
    if not q:
        return None

    best = None
    best_confidence = None

    for entry in _entries:
        # Exact match on name
        if q == entry['name'].lower():
            return {**entry, 'confidence': 'exact'}

        # Exact match on any alias
        if q in entry['aliases']:
            return {**entry, 'confidence': 'exact'}

        # Partial: query is substring of name/alias, or name/alias is substring of query
        name_lower = entry['name'].lower()
        if q in name_lower or name_lower in q:
            if best is None:
                best = entry
                best_confidence = 'partial'
            continue

        for alias in entry['aliases']:
            if q in alias or alias in q:
                if best is None:
                    best = entry
                    best_confidence = 'partial'
                break

    if best is not None:
        return {**best, 'confidence': best_confidence}

    return None


def list_all():
    """Return all address book entries."""
    _reload_if_changed()
    return list(_entries)
