"""
Nav-I API Keys Admin — unified view/update/test for third-party API keys.

Manages three provider categories:
  - Gemini (multiple keys via KeyManager singleton)
  - TomTom (single key in .env)
  - Google Places (single key in .env)

All key values are masked in responses. Full values never leave the server
except as user-supplied input on update.
"""
import os
import re
import shutil
import tempfile
import time

import requests as http_requests

from .utils import setup_logging

logger = setup_logging('recon.api_keys_admin')

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')

# Key definitions: env_name → display metadata
_KEY_DEFS = {
    'TOMTOM_API_KEY': {
        'display_name': 'TomTom',
        'provider': 'tomtom',
    },
    'GOOGLE_PLACES_API_KEY': {
        'display_name': 'Google Places',
        'provider': 'google_places',
    },
}


# ── .env read/write helpers ─────────────────────────────────────────────

def _read_env():
    """Read .env file into a dict of key=value pairs, preserving order."""
    entries = []  # list of (key, value, raw_line) — preserves order and comments
    if not os.path.exists(ENV_PATH):
        return entries
    with open(ENV_PATH, 'r') as f:
        for line in f:
            raw = line.rstrip('\n')
            stripped = raw.strip()
            if not stripped or stripped.startswith('#'):
                entries.append((None, None, raw))
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', stripped)
            if m:
                entries.append((m.group(1), m.group(2).strip().strip('"').strip("'"), raw))
            else:
                entries.append((None, None, raw))
    return entries


def _write_env(entries):
    """Atomically write .env from entries list. Backs up to .env.bak first."""
    # Backup current .env
    if os.path.exists(ENV_PATH):
        bak_path = ENV_PATH + '.bak'
        shutil.copy2(ENV_PATH, bak_path)

    # Write to temp file, then rename (atomic on same filesystem)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(ENV_PATH), prefix='.env.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            for key, value, raw in entries:
                if key is not None:
                    f.write(f'{key}={value}\n')
                else:
                    f.write(raw + '\n')
        os.rename(tmp_path, ENV_PATH)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info(f"Wrote .env atomically ({len([e for e in entries if e[0]])} keys)")


def _get_env_value(name):
    """Get a single value from .env by key name."""
    for key, value, _ in _read_env():
        if key == name:
            return value
    return None


def _set_env_value(name, new_value):
    """Set a single value in .env. Adds if not present."""
    entries = _read_env()
    found = False
    for i, (key, value, raw) in enumerate(entries):
        if key == name:
            entries[i] = (name, new_value, f'{name}={new_value}')
            found = True
            break
    if not found:
        entries.append((name, new_value, f'{name}={new_value}'))
    _write_env(entries)


# ── Masking ─────────────────────────────────────────────────────────────

def _mask_key(value):
    """Mask a key: first 4 chars + '...' + last 4 chars. Never return full value."""
    if not value:
        return None
    if len(value) <= 8:
        return '****'
    return value[:4] + '...' + value[-4:]


# ── List ────────────────────────────────────────────────────────────────

def list_keys():
    """
    Return masked status of all managed API keys.

    Returns list of dicts with: name, display_name, provider, masked_value,
    is_set, count (for multi-key providers like Gemini).
    """
    result = []
    env_mtime = None
    if os.path.exists(ENV_PATH):
        env_mtime = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                  time.gmtime(os.path.getmtime(ENV_PATH)))

    # Gemini keys (via KeyManager)
    from .key_manager import get_key_manager
    km = get_key_manager()
    gemini_keys = km.get_masked_keys()
    gemini_count = len(gemini_keys)
    # Show a single summary entry for Gemini with count
    first_masked = gemini_keys[0]['masked'] if gemini_keys else None
    result.append({
        'name': 'GEMINI_KEY',
        'display_name': 'Gemini',
        'provider': 'gemini',
        'masked_value': first_masked,
        'is_set': gemini_count > 0,
        'count': gemini_count,
        'last_modified': env_mtime,
        'keys': gemini_keys,  # full list with per-key stats
    })

    # Single-value keys
    for env_name, meta in _KEY_DEFS.items():
        value = _get_env_value(env_name)
        result.append({
            'name': env_name,
            'display_name': meta['display_name'],
            'provider': meta['provider'],
            'masked_value': _mask_key(value),
            'is_set': bool(value),
            'count': 1 if value else 0,
            'last_modified': env_mtime,
        })

    return result


# ── Update ──────────────────────────────────────────────────────────────

def update_key(name, new_value):
    """
    Update a key value. For Gemini, name should be 'GEMINI_KEY' with an
    optional 'index' for replacing a specific key, or use the KeyManager API.
    For TomTom/Google Places, writes directly to .env.

    Returns dict with success status and masked value.
    """
    new_value = new_value.strip()
    if not new_value:
        return {'success': False, 'error': 'Key value cannot be empty'}

    if name == 'GEMINI_KEY':
        # Use KeyManager for Gemini
        from .key_manager import get_key_manager
        km = get_key_manager()
        try:
            idx = km.add_gemini_key(new_value)
            return {
                'success': True,
                'name': name,
                'masked_value': _mask_key(new_value),
                'action': 'added',
                'index': idx,
            }
        except ValueError as e:
            return {'success': False, 'error': str(e)}

    if name in _KEY_DEFS:
        _set_env_value(name, new_value)
        return {
            'success': True,
            'name': name,
            'masked_value': _mask_key(new_value),
            'action': 'updated',
        }

    return {'success': False, 'error': f'Unknown key: {name}'}


def update_gemini_key(index, new_value):
    """Replace a specific Gemini key by index."""
    new_value = new_value.strip()
    if not new_value:
        return {'success': False, 'error': 'Key value cannot be empty'}

    from .key_manager import get_key_manager
    km = get_key_manager()
    try:
        km.replace_gemini_key(index, new_value)
        return {
            'success': True,
            'name': 'GEMINI_KEY',
            'index': index,
            'masked_value': _mask_key(new_value),
            'action': 'replaced',
        }
    except (ValueError, IndexError) as e:
        return {'success': False, 'error': str(e)}


# ── Test ────────────────────────────────────────────────────────────────

def test_key(name, index=None):
    """
    Test a key against its provider API using the current .env value.

    Returns dict with: success, latency_ms, error, note.
    """
    if name == 'GEMINI_KEY':
        return _test_gemini(index)
    elif name == 'TOMTOM_API_KEY':
        return _test_tomtom()
    elif name == 'GOOGLE_PLACES_API_KEY':
        return _test_google_places()
    else:
        return {'success': False, 'error': f'Unknown key: {name}', 'latency_ms': 0}


def _test_gemini(index=None):
    """Test Gemini key by listing models."""
    from .key_manager import get_key_manager
    km = get_key_manager()

    if index is not None:
        key = km.get_gemini_key(index)
        if not key:
            return {'success': False, 'error': f'Gemini key index {index} not found', 'latency_ms': 0}
    else:
        key = km.get_gemini_key(0)
        if not key:
            return {'success': False, 'error': 'No Gemini keys configured', 'latency_ms': 0}

    t0 = time.time()
    try:
        resp = http_requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=10
        )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200 and 'models' in resp.text:
            return {'success': True, 'latency_ms': latency, 'error': None,
                    'note': 'Models list returned successfully'}
        elif resp.status_code == 403:
            return {'success': False, 'latency_ms': latency,
                    'error': 'Key disabled or quota exhausted'}
        elif resp.status_code == 429:
            return {'success': True, 'latency_ms': latency, 'error': None,
                    'note': 'Valid key — currently rate-limited'}
        else:
            return {'success': False, 'latency_ms': latency,
                    'error': f'HTTP {resp.status_code}'}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {'success': False, 'latency_ms': latency, 'error': str(e)}


def _test_tomtom():
    """Test TomTom key with a minimal geocode request."""
    key = _get_env_value('TOMTOM_API_KEY')
    if not key:
        return {'success': False, 'error': 'TOMTOM_API_KEY not set', 'latency_ms': 0}

    t0 = time.time()
    try:
        resp = http_requests.get(
            f"https://api.tomtom.com/search/2/geocode/Boise.json",
            params={'key': key, 'limit': 1},
            timeout=10
        )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            count = data.get('summary', {}).get('totalResults', 0)
            return {'success': True, 'latency_ms': latency, 'error': None,
                    'note': f'Geocode returned {count} result(s)'}
        elif resp.status_code == 403:
            return {'success': False, 'latency_ms': latency,
                    'error': 'Invalid or expired key'}
        else:
            return {'success': False, 'latency_ms': latency,
                    'error': f'HTTP {resp.status_code}'}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {'success': False, 'latency_ms': latency, 'error': str(e)}


def _test_google_places():
    """Test Google Places (New) API key with a minimal searchText request."""
    key = _get_env_value('GOOGLE_PLACES_API_KEY')
    if not key:
        return {'success': False, 'error': 'GOOGLE_PLACES_API_KEY not set', 'latency_ms': 0}

    t0 = time.time()
    try:
        resp = http_requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            json={'textQuery': 'Boise Idaho', 'maxResultCount': 1},
            headers={
                'X-Goog-Api-Key': key,
                'X-Goog-FieldMask': 'places.displayName',
            },
            timeout=10
        )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            count = len(data.get('places', []))
            return {'success': True, 'latency_ms': latency, 'error': None,
                    'note': f'searchText returned {count} place(s)'}
        elif resp.status_code == 403:
            return {'success': False, 'latency_ms': latency,
                    'error': 'Key not authorized for Places API (New)'}
        elif resp.status_code == 429:
            return {'success': True, 'latency_ms': latency, 'error': None,
                    'note': 'Valid key — quota exceeded'}
        else:
            body = resp.text[:200]
            return {'success': False, 'latency_ms': latency,
                    'error': f'HTTP {resp.status_code}: {body}'}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {'success': False, 'latency_ms': latency, 'error': str(e)}
