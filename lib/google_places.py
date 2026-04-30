"""
Google Places (New) API client for tertiary enrichment.

Searches for business POIs and fetches details (opening hours, phone, website)
when OSM + Overture data is incomplete. Uses field masks to minimize cost.

API docs: https://developers.google.com/maps/documentation/places/web-service
"""
import json
import os
import sqlite3
import time
from datetime import date, timezone, datetime

import requests

from .utils import setup_logging

logger = setup_logging('recon.google_places')

API_BASE = 'https://places.googleapis.com/v1'
DEFAULT_DAILY_CAP = 500
REQUEST_TIMEOUT = 3  # seconds

# Google day index → OSM abbreviation
_DAY_ABBR = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']

_db_conn = None


def _get_db():
    """Return a module-level SQLite connection (lazy init)."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    db_path = os.path.join(db_dir, 'place_cache.db')
    _db_conn = sqlite3.connect(db_path, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    # Ensure google_api_calls table exists
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS google_api_calls (
            call_date TEXT PRIMARY KEY,
            call_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    _db_conn.commit()
    # Schema migration: add Google columns to place_cache if missing
    for col, coldef in [
        ('google_place_id', 'TEXT'),
        ('google_data', 'TEXT'),
        ('google_fetched_at', 'INTEGER'),
    ]:
        try:
            _db_conn.execute(f'ALTER TABLE place_cache ADD COLUMN {col} {coldef}')
            logger.info(f'Added column {col} to place_cache')
        except sqlite3.OperationalError:
            pass  # Column already exists
    _db_conn.commit()
    return _db_conn


def _get_api_key():
    """Return the Google Places API key from environment."""
    key = os.environ.get('GOOGLE_PLACES_API_KEY')
    if not key:
        logger.error("GOOGLE_PLACES_API_KEY not set in environment")
    return key


def _get_daily_cap():
    """Return the daily API call cap (configurable via deployment config)."""
    try:
        from .deployment_config import get_deployment_config
        config = get_deployment_config()
        return config.get('google_places', {}).get('daily_cap', DEFAULT_DAILY_CAP)
    except Exception:
        return DEFAULT_DAILY_CAP


# ── Daily call counter ──────────────────────────────────────────────────

def check_daily_cap():
    """Return True if under daily cap, False if limit reached."""
    db = _get_db()
    today = date.today().isoformat()
    row = db.execute(
        "SELECT call_count FROM google_api_calls WHERE call_date = ?", (today,)
    ).fetchone()
    current = row[0] if row else 0
    cap = _get_daily_cap()
    if current >= cap:
        logger.info(f"google_places: daily_cap_reached count={current} cap={cap}")
        return False
    return True


def get_daily_count():
    """Return today's API call count."""
    db = _get_db()
    today = date.today().isoformat()
    row = db.execute(
        "SELECT call_count FROM google_api_calls WHERE call_date = ?", (today,)
    ).fetchone()
    return row[0] if row else 0


def increment_call_counter():
    """Atomically increment today's API call counter."""
    db = _get_db()
    today = date.today().isoformat()
    db.execute("""
        INSERT INTO google_api_calls (call_date, call_count) VALUES (?, 1)
        ON CONFLICT(call_date) DO UPDATE SET call_count = call_count + 1
    """, (today,))
    db.commit()


def _set_daily_count_to_cap():
    """Set today's counter to the cap value (soft-stop on quota error)."""
    db = _get_db()
    today = date.today().isoformat()
    cap = _get_daily_cap()
    db.execute("""
        INSERT INTO google_api_calls (call_date, call_count) VALUES (?, ?)
        ON CONFLICT(call_date) DO UPDATE SET call_count = ?
    """, (today, cap, cap))
    db.commit()


# ── Google Places cache (on place_cache table) ─────────────────────────

def cache_get_google(osm_type, osm_id):
    """Return (google_place_id, google_data_dict) or (None, None)."""
    db = _get_db()
    row = db.execute(
        "SELECT google_place_id, google_data FROM place_cache WHERE osm_type=? AND osm_id=?",
        (osm_type, osm_id)
    ).fetchone()
    if row and row[0]:
        data = None
        if row[1]:
            try:
                data = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                pass
        return row[0], data
    return None, None


def cache_put_google(osm_type, osm_id, place_id, data):
    """Store Google Places data for a cache entry (UPSERT on google columns)."""
    db = _get_db()
    now = int(time.time())
    db.execute("""
        INSERT INTO place_cache (osm_type, osm_id, data, source, cached_at, google_place_id, google_data, google_fetched_at)
        VALUES (?, ?, '', 'pending', 0, ?, ?, ?)
        ON CONFLICT(osm_type, osm_id) DO UPDATE SET
            google_place_id = excluded.google_place_id,
            google_data = excluded.google_data,
            google_fetched_at = excluded.google_fetched_at
    """, (osm_type, osm_id, place_id, json.dumps(data) if data else None, now))
    db.commit()


# ── API calls ───────────────────────────────────────────────────────────

def search_place(name, lat, lon, radius_m=200):
    """
    Search Google Places (New) for a business by name + location.
    Returns the Google Place ID of the best match, or None.
    """
    key = _get_api_key()
    if not key:
        return None

    if not check_daily_cap():
        return None

    try:
        resp = requests.post(
            f'{API_BASE}/places:searchText',
            headers={
                'Content-Type': 'application/json',
                'X-Goog-Api-Key': key,
                'X-Goog-FieldMask': 'places.id,places.displayName,places.location',
            },
            json={
                'textQuery': name,
                'locationBias': {
                    'circle': {
                        'center': {'latitude': lat, 'longitude': lon},
                        'radius': float(radius_m),
                    }
                },
                'maxResultCount': 1,
            },
            timeout=REQUEST_TIMEOUT,
        )

        increment_call_counter()

        if resp.status_code == 429:
            logger.warning("google_places: action=search place=%s result=rate_limited", name)
            _set_daily_count_to_cap()
            return None

        if resp.status_code == 403:
            logger.error("google_places: action=search place=%s result=forbidden (invalid key?)", name)
            return None

        if resp.status_code != 200:
            logger.warning("google_places: action=search place=%s result=error status=%d", name, resp.status_code)
            return None

        data = resp.json()
        places = data.get('places', [])
        if not places:
            logger.info("google_places: action=search place=%s result=miss", name)
            return None

        place_id = places[0].get('id')
        display = places[0].get('displayName', {}).get('text', '?')
        logger.info("google_places: action=search place=%s result=hit google_name=%s id=%s", name, display, place_id)
        return place_id

    except requests.exceptions.Timeout:
        logger.warning("google_places: action=search place=%s result=timeout", name)
        return None
    except Exception as e:
        logger.error("google_places: action=search place=%s result=error err=%s", name, e)
        return None


def get_place_details(place_id):
    """
    Fetch details for a Google Place ID.
    Returns dict with {opening_hours, phone_number, website} or None.
    """
    key = _get_api_key()
    if not key:
        return None

    if not check_daily_cap():
        return None

    try:
        resp = requests.get(
            f'{API_BASE}/places/{place_id}',
            headers={
                'X-Goog-Api-Key': key,
                'X-Goog-FieldMask': 'regularOpeningHours,internationalPhoneNumber,websiteUri',
            },
            timeout=REQUEST_TIMEOUT,
        )

        increment_call_counter()

        if resp.status_code == 429:
            logger.warning("google_places: action=details id=%s result=rate_limited", place_id)
            _set_daily_count_to_cap()
            return None

        if resp.status_code != 200:
            logger.warning("google_places: action=details id=%s result=error status=%d", place_id, resp.status_code)
            return None

        data = resp.json()
        result = {
            'opening_hours': None,
            'opening_hours_raw': None,
            'phone_number': None,
            'website': None,
        }

        # Phone
        phone = data.get('internationalPhoneNumber')
        if phone:
            result['phone_number'] = phone.replace(' ', '').replace('-', '')

        # Website
        result['website'] = data.get('websiteUri')

        # Opening hours
        hours = data.get('regularOpeningHours')
        if hours:
            # Try OSM-compatible format from periods
            periods = hours.get('periods', [])
            if periods:
                osm_str = _periods_to_osm(periods)
                if osm_str:
                    result['opening_hours'] = osm_str

            # Fallback: weekday descriptions (human-readable)
            if not result['opening_hours']:
                descriptions = hours.get('weekdayDescriptions')
                if descriptions:
                    result['opening_hours_raw'] = descriptions

        logger.info("google_places: action=details id=%s result=hit hours=%s phone=%s website=%s",
                     place_id,
                     'yes' if result['opening_hours'] or result['opening_hours_raw'] else 'no',
                     'yes' if result['phone_number'] else 'no',
                     'yes' if result['website'] else 'no')
        return result

    except requests.exceptions.Timeout:
        logger.warning("google_places: action=details id=%s result=timeout", place_id)
        return None
    except Exception as e:
        logger.error("google_places: action=details id=%s result=error err=%s", place_id, e)
        return None


# ── Opening hours conversion ────────────────────────────────────────────

def _periods_to_osm(periods):
    """
    Convert Google Places periods array to OSM opening_hours string.

    Google periods: [{"open": {"day": 0-6, "hour": H, "minute": M},
                      "close": {"day": 0-6, "hour": H, "minute": M}}, ...]
    Where day 0 = Sunday.

    OSM format: "Mo-Fr 06:00-23:00; Sa-Su 07:00-23:00"
    """
    if not periods:
        return None

    # Check for 24/7: single period with no close, or open 00:00 close 00:00 next day
    if len(periods) == 1:
        p = periods[0]
        o = p.get('open', {})
        c = p.get('close')
        if c is None and o.get('hour', 0) == 0 and o.get('minute', 0) == 0:
            return '24/7'

    # Build a map: day_index → "HH:MM-HH:MM"
    day_hours = {}  # day_index → time_range string
    for p in periods:
        o = p.get('open', {})
        c = p.get('close', {})
        day = o.get('day', 0)
        open_time = f"{o.get('hour', 0):02d}:{o.get('minute', 0):02d}"

        if c:
            close_time = f"{c.get('hour', 0):02d}:{c.get('minute', 0):02d}"
            # Handle midnight closing (00:00 means end of day)
            if close_time == '00:00':
                close_time = '24:00'
        else:
            close_time = '24:00'

        time_range = f"{open_time}-{close_time}"

        # A day can have multiple periods (e.g., lunch break)
        if day in day_hours:
            day_hours[day] = day_hours[day] + ',' + time_range
        else:
            day_hours[day] = time_range

    if not day_hours:
        return None

    # Check if all 7 days have same hours
    unique_ranges = set(day_hours.values())
    if len(day_hours) == 7 and len(unique_ranges) == 1:
        hours = unique_ranges.pop()
        if hours == '00:00-24:00':
            return '24/7'
        return hours  # implicit "every day"

    # Group consecutive days with same hours
    # Reorder to OSM convention: Mo(1) Tu(2) We(3) Th(4) Fr(5) Sa(6) Su(0)
    osm_day_order = [1, 2, 3, 4, 5, 6, 0]
    groups = []
    current_days = []
    current_hours = None

    for day_idx in osm_day_order:
        hours = day_hours.get(day_idx)
        if hours == current_hours:
            current_days.append(day_idx)
        else:
            if current_days and current_hours:
                groups.append((current_days, current_hours))
            current_days = [day_idx]
            current_hours = hours

    if current_days and current_hours:
        groups.append((current_days, current_hours))

    if not groups:
        return None

    # Format each group
    parts = []
    for days, hours in groups:
        if len(days) == 1:
            day_str = _DAY_ABBR[days[0]]
        elif len(days) == 2:
            day_str = f"{_DAY_ABBR[days[0]]},{_DAY_ABBR[days[1]]}"
        else:
            day_str = f"{_DAY_ABBR[days[0]]}-{_DAY_ABBR[days[-1]]}"
        parts.append(f"{day_str} {hours}")

    return '; '.join(parts)
