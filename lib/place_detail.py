"""
Place detail proxy — local Nominatim first, Overpass API fallback, SQLite cache.
Overture Maps enrichment layer fills sparse extratags (phone, website, brand).

Provides get_place_detail(osm_type, osm_id) which returns a cleaned dict
matching the response shape for /api/place/<osm_type>/<osm_id>.
"""
import json
import os
import sqlite3
import time

import requests as http_requests

from .osm_categories import humanize_category
from .utils import setup_logging

logger = setup_logging('recon.place_detail')

NOMINATIM_URL = "http://localhost:8010/details.php"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_UA = "Navi/1.0 (forge.echo6.co/matt/recon)"
VALID_OSM_TYPES = {"N", "W", "R"}

_db_conn = None


# ── SQLite cache ────────────────────────────────────────────────────────

def _get_db():
    """Return a module-level SQLite connection (lazy init)."""
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
        CREATE TABLE IF NOT EXISTS place_cache (
            osm_type TEXT NOT NULL,
            osm_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            source TEXT NOT NULL,
            cached_at INTEGER NOT NULL,
            PRIMARY KEY (osm_type, osm_id)
        )
    """)
    _db_conn.commit()
    logger.info(f"Place cache DB ready at {db_path}")
    return _db_conn


def cache_get(osm_type, osm_id):
    """Return cached place dict or None."""
    db = _get_db()
    row = db.execute(
        "SELECT data FROM place_cache WHERE osm_type=? AND osm_id=?",
        (osm_type, osm_id)
    ).fetchone()
    if row:
        try:
            result = json.loads(row[0])
            result['source'] = 'cache'
            return result
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def cache_put(osm_type, osm_id, data, source):
    """Store a place detail result in the cache (preserves google columns)."""
    db = _get_db()
    now = int(time.time())
    db.execute("""
        INSERT INTO place_cache (osm_type, osm_id, data, source, cached_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(osm_type, osm_id) DO UPDATE SET
            data = excluded.data,
            source = excluded.source,
            cached_at = excluded.cached_at
    """, (osm_type, osm_id, json.dumps(data), source, now))
    db.commit()


# ── Overture enrichment ─────────────────────────────────────────────────

def _enrich_with_overture(result, osm_type, osm_id):
    """
    Attempt to enrich a place result with Overture Maps data.
    Fills sparse extratags (phone, website, brand) without overwriting existing values.
    Returns the (possibly enriched) result dict.
    """
    try:
        from .deployment_config import get_deployment_config
        deploy_config = get_deployment_config()
        features = deploy_config.get('features', {})
        if not features.get('has_overture_enrichment', False):
            return result
    except Exception:
        return result

    try:
        from .overture import find_by_osm_id, find_by_coords_and_name
    except ImportError:
        logger.debug("Overture module not available")
        return result

    enrichment = None
    match_method = None

    # Strategy 1: OSM cross-reference (exact)
    enrichment = find_by_osm_id(osm_type, osm_id)
    if enrichment:
        match_method = 'osm_xref'

    # Strategy 2: Coordinate + name fuzzy (fallback)
    if not enrichment and result.get('centroid') and result.get('name'):
        centroid = result['centroid']
        if centroid.get('lat') and centroid.get('lon'):
            enrichment = find_by_coords_and_name(
                centroid['lat'], centroid['lon'], result['name']
            )
            if enrichment:
                match_method = 'coord_name_fuzzy'

    if not enrichment:
        return result

    # Fill sparse extratags (never overwrite existing non-null values)
    extratags = result.get('extratags', {})
    fill_map = [
        ('phone', 'phone'),
        ('website', 'website'),
        ('brand', 'brand_name'),
        ('brand:wikidata', 'brand_wikidata'),
    ]
    for osm_key, overture_key in fill_map:
        if not extratags.get(osm_key) and enrichment.get(overture_key):
            extratags[osm_key] = enrichment[overture_key]
    result['extratags'] = extratags

    # Add source metadata
    result['sources'] = {
        'primary': result.get('source', 'unknown'),
        'enrichment': 'overture',
        'overture_match_method': match_method,
        'overture_gers_id': enrichment.get('gers_id'),
        'overture_confidence': enrichment.get('confidence'),
        'overture_basic_category': enrichment.get('basic_category'),
    }

    logger.debug(f"Overture enrichment for {osm_type}/{osm_id}: {match_method}")
    return result



# ── Google Places enrichment (tertiary, gap-fill only) ──────────────

# Business POI classes eligible for Google enrichment
_BUSINESS_CLASSES = {'amenity', 'shop', 'tourism', 'leisure', 'office', 'craft'}

# Fields Google can fill
_GOOGLE_GAP_FIELDS = ('opening_hours', 'phone', 'website')


def _enrich_with_google(result, osm_type, osm_id):
    """
    Tertiary enrichment via Google Places (New) API.
    Only fires for business-type POIs when opening_hours, phone, or website
    are still missing after OSM + Overture enrichment.
    Fills only empty fields — never overwrites existing values.
    """
    # Check feature flag
    try:
        from .deployment_config import get_deployment_config
        deploy_config = get_deployment_config()
        features = deploy_config.get('features', {})
        if not features.get('has_google_places_enrichment', False):
            return result
    except Exception:
        return result

    # Only enrich business-type POIs
    poi_class = result.get('class', '')
    if poi_class not in _BUSINESS_CLASSES:
        return result

    # Check if any gap fields are missing
    extratags = result.get('extratags', {})
    gaps = [f for f in _GOOGLE_GAP_FIELDS if not extratags.get(f)]
    if not gaps:
        logger.debug(f"google_places: skip {osm_type}/{osm_id} — no gaps")
        return result

    try:
        from . import google_places
    except ImportError:
        logger.debug("google_places module not available")
        return result

    # Check Google cache first
    cached_pid, cached_data = google_places.cache_get_google(osm_type, osm_id)
    if cached_pid and cached_data:
        _apply_google_data(result, cached_data, gaps)
        result.setdefault('sources', {})['google_places'] = {
            'place_id': cached_pid,
            'source': 'cache',
        }
        logger.debug(f"google_places: cache hit for {osm_type}/{osm_id}")
        return result

    # Skip if already looked up and found nothing (cached_pid is None)
    if cached_pid is not None:
        return result

    # Daily cap check
    if not google_places.check_daily_cap():
        return result

    # Search for the place
    name = result.get('name', '')
    centroid = result.get('centroid', {})
    lat = centroid.get('lat')
    lon = centroid.get('lon')
    if not name or not lat or not lon:
        return result

    place_id = google_places.search_place(name, lat, lon)
    if not place_id:
        # Cache the miss to avoid repeated lookups
        google_places.cache_put_google(osm_type, osm_id, '__miss__', None)
        return result

    # Get details
    details = google_places.get_place_details(place_id)
    if not details:
        google_places.cache_put_google(osm_type, osm_id, place_id, None)
        return result

    # Cache the result
    google_places.cache_put_google(osm_type, osm_id, place_id, details)

    # Apply to result
    _apply_google_data(result, details, gaps)
    result.setdefault('sources', {})['google_places'] = {
        'place_id': place_id,
        'source': 'api',
        'daily_count': google_places.get_daily_count(),
    }

    return result


def _apply_google_data(result, google_data, gaps):
    """Apply Google Places data to fill gap fields only."""
    extratags = result.get('extratags', {})
    if 'opening_hours' in gaps:
        osm_hours = google_data.get('opening_hours')
        if osm_hours:
            extratags['opening_hours'] = osm_hours
        elif google_data.get('opening_hours_raw'):
            extratags['opening_hours_raw'] = google_data['opening_hours_raw']
    if 'phone' in gaps and google_data.get('phone_number'):
        extratags['phone'] = google_data['phone_number']
    if 'website' in gaps and google_data.get('website'):
        extratags['website'] = google_data['website']
    result['extratags'] = extratags


# ── Nominatim parsing ───────────────────────────────────────────────────

# Nominatim address array uses rank_address to indicate what each entry is.
# We map rank ranges to our flat address fields.
RANK_TO_FIELD = {
    4: 'country',
    5: 'postcode',
    6: 'state',          # rank 6 = county in US, but we try name matching
    8: 'state',
    12: 'county',
    16: 'city',
    20: 'neighbourhood',
    22: 'neighbourhood',
    26: 'road',
    28: 'house_number',
}


def _parse_nominatim_address(address_array, country_code=None):
    """Parse Nominatim's ranked address array into a flat address dict."""
    addr = {
        'house_number': None,
        'road': None,
        'neighbourhood': None,
        'city': None,
        'county': None,
        'state': None,
        'postcode': None,
        'country': None,
        'country_code': country_code,
    }

    if not address_array:
        return addr

    for entry in address_array:
        if not entry.get('isaddress', False):
            continue

        name = entry.get('localname', '')
        rank = entry.get('rank_address', 0)
        etype = entry.get('type', '')
        eclass = entry.get('class', '')

        # Explicit type-based assignments (more reliable than rank alone)
        if etype == 'country' and eclass == 'place':
            addr['country'] = name
        elif etype == 'state' or (eclass == 'boundary' and etype == 'administrative' and rank == 8):
            if not addr['state']:
                addr['state'] = name
        elif etype == 'county' or (eclass == 'boundary' and etype == 'administrative' and rank in (10, 12)):
            if not addr['county']:
                addr['county'] = name
        elif etype in ('city', 'town', 'village', 'hamlet') and eclass == 'place':
            if not addr['city']:
                addr['city'] = name
        elif eclass == 'boundary' and etype == 'administrative' and rank == 16:
            # City-level admin boundary (common in US)
            if not addr['city']:
                addr['city'] = name
        elif etype == 'postcode':
            addr['postcode'] = name
        elif eclass == 'highway' or rank == 26:
            if not addr['road']:
                addr['road'] = name
        elif etype == 'house_number' or rank == 28:
            addr['house_number'] = name
        elif rank in (20, 22) and not addr['neighbourhood']:
            addr['neighbourhood'] = name

    # Remove county from output (not in spec)
    addr.pop('county', None)

    return addr


def _parse_nominatim(data):
    """Parse a Nominatim /details response into our canonical shape."""
    osm_type = data.get('osm_type', '')
    osm_id = data.get('osm_id', 0)
    osm_class = data.get('category', '')
    osm_type_tag = data.get('type', '')

    # Centroid
    centroid_geom = data.get('centroid', {})
    coords = centroid_geom.get('coordinates', [0, 0])
    centroid = {'lat': coords[1], 'lon': coords[0]} if len(coords) >= 2 else {'lat': 0, 'lon': 0}

    # Names
    names = data.get('names', {})
    display_name = data.get('localname', '') or names.get('name', '')

    # Address
    address = _parse_nominatim_address(
        data.get('address', []),
        country_code=data.get('country_code')
    )

    # Use calculated_postcode if address parse didn't find one
    if not address.get('postcode') and data.get('calculated_postcode'):
        address['postcode'] = data['calculated_postcode']

    # Extratags
    raw_extra = data.get('extratags', {})
    extratags = {
        'opening_hours': raw_extra.get('opening_hours'),
        'phone': raw_extra.get('phone') or raw_extra.get('contact:phone'),
        'website': raw_extra.get('website') or raw_extra.get('contact:website') or raw_extra.get('url'),
        'email': raw_extra.get('email') or raw_extra.get('contact:email'),
        'wikipedia': raw_extra.get('wikipedia'),
        'wikidata': raw_extra.get('wikidata'),
        'cuisine': raw_extra.get('cuisine'),
        'operator': raw_extra.get('operator'),
        'wheelchair': raw_extra.get('wheelchair'),
        'fee': raw_extra.get('fee'),
        'takeaway': raw_extra.get('takeaway'),
    }

    # Category: use extratags.place for boundaries (e.g. "city"), else class/type
    effective_class = osm_class
    effective_type = osm_type_tag
    if osm_class == 'boundary' and osm_type_tag == 'administrative':
        place_tag = raw_extra.get('place') or raw_extra.get('linked_place')
        if place_tag:
            effective_class = 'place'
            effective_type = place_tag

    category = humanize_category(effective_class, effective_type)

    # Filter names: only include extra name tags, not the bare "name"
    extra_names = {k: v for k, v in names.items() if k != 'name'} if names else {}

    return {
        'osm_type': osm_type,
        'osm_id': osm_id,
        'name': display_name,
        'category': category,
        'class': osm_class,
        'type': osm_type_tag,
        'address': address,
        'centroid': centroid,
        'extratags': extratags,
        'names': extra_names if extra_names else None,
        'source': 'nominatim_local',
    }


# ── Overpass parsing ────────────────────────────────────────────────────

OVERPASS_TYPE_MAP = {'N': 'node', 'W': 'way', 'R': 'relation'}


def _build_overpass_query(osm_type, osm_id):
    """Build an Overpass QL query for a single element."""
    elem = OVERPASS_TYPE_MAP.get(osm_type)
    if not elem:
        return None
    return f"[out:json][timeout:10];{elem}({osm_id});out tags center;"


def _parse_overpass(data, osm_type, osm_id):
    """Parse an Overpass API response into our canonical shape."""
    elements = data.get('elements', [])
    if not elements:
        return None

    elem = elements[0]
    tags = elem.get('tags', {})

    # Centroid: Overpass returns lat/lon for nodes, center for ways/relations
    lat = elem.get('lat') or (elem.get('center', {}).get('lat'))
    lon = elem.get('lon') or (elem.get('center', {}).get('lon'))
    centroid = {'lat': lat, 'lon': lon} if lat and lon else {'lat': 0, 'lon': 0}

    # Determine class/type from tags — Overpass doesn't have a canonical class field
    # Use the first recognized class tag
    osm_class = ''
    osm_type_tag = ''
    for cls in ('amenity', 'shop', 'leisure', 'tourism', 'natural', 'highway',
                'boundary', 'place', 'building', 'waterway', 'landuse', 'historic'):
        if cls in tags:
            osm_class = cls
            osm_type_tag = tags[cls]
            break

    category = humanize_category(osm_class, osm_type_tag)

    # Address from addr:* tags
    address = {
        'house_number': tags.get('addr:housenumber'),
        'road': tags.get('addr:street'),
        'neighbourhood': tags.get('addr:suburb') or tags.get('addr:neighbourhood'),
        'city': tags.get('addr:city'),
        'state': tags.get('addr:state'),
        'postcode': tags.get('addr:postcode'),
        'country': tags.get('addr:country'),
        'country_code': tags.get('addr:country_code',
                                  tags.get('addr:country', '')).lower()[:2] or None,
    }

    # Extratags
    extratags = {
        'opening_hours': tags.get('opening_hours'),
        'phone': tags.get('phone') or tags.get('contact:phone'),
        'website': tags.get('website') or tags.get('contact:website') or tags.get('url'),
        'email': tags.get('email') or tags.get('contact:email'),
        'wikipedia': tags.get('wikipedia'),
        'wikidata': tags.get('wikidata'),
        'cuisine': tags.get('cuisine'),
        'operator': tags.get('operator'),
        'wheelchair': tags.get('wheelchair'),
        'fee': tags.get('fee'),
        'takeaway': tags.get('takeaway'),
    }

    # Names
    name = tags.get('name', '')
    extra_names = {}
    for k, v in tags.items():
        if k.startswith('name:') or k in ('alt_name', 'old_name', 'short_name', 'official_name'):
            extra_names[k] = v

    return {
        'osm_type': osm_type,
        'osm_id': osm_id,
        'name': name,
        'category': category,
        'class': osm_class,
        'type': osm_type_tag,
        'address': address,
        'centroid': centroid,
        'extratags': extratags,
        'names': extra_names if extra_names else None,
        'source': 'overpass',
    }


# ── Public API ──────────────────────────────────────────────────────────

def get_place_detail(osm_type, osm_id):
    """
    Fetch place details for an OSM element.

    Returns (dict, status_code):
      - (data, 200) on success
      - (error_dict, 404) if not found in any source
      - (error_dict, 502) if both sources error
    """
    osm_type = osm_type.upper()
    if osm_type not in VALID_OSM_TYPES:
        return {'error': f'Invalid osm_type: {osm_type}. Must be N, W, or R.'}, 400

    if osm_id <= 0:
        return {'error': 'osm_id must be a positive integer'}, 400

    # 1. Check cache
    cached = cache_get(osm_type, osm_id)
    if cached:
        logger.debug(f"Cache hit: {osm_type}/{osm_id}")
        return cached, 200

    # 2. Try local Nominatim first
    nominatim_result = None
    nominatim_error = None
    try:
        resp = http_requests.get(NOMINATIM_URL, params={
            'osmtype': osm_type,
            'osmid': osm_id,
            'format': 'json',
            'addressdetails': 1,
            'hierarchy': 0,
            'keywords': 0,
        }, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            # Nominatim returns a result even for IDs not in its DB,
            # but they'll have empty/minimal data. Check for osm_id match.
            if data.get('osm_id') == osm_id:
                nominatim_result = _parse_nominatim(data)
                logger.debug(f"Nominatim hit: {osm_type}/{osm_id}")
    except Exception as e:
        nominatim_error = str(e)
        logger.warning(f"Nominatim error for {osm_type}/{osm_id}: {e}")

    if nominatim_result:
        nominatim_result = _enrich_with_overture(nominatim_result, osm_type, osm_id)
        nominatim_result = _enrich_with_google(nominatim_result, osm_type, osm_id)
        cache_put(osm_type, osm_id, nominatim_result, 'nominatim_local')
        return nominatim_result, 200

    # 3. Fallback to Overpass
    overpass_result = None
    overpass_error = None
    try:
        query = _build_overpass_query(osm_type, osm_id)
        if query:
            resp = http_requests.post(
                OVERPASS_URL,
                data={'data': query},
                headers={'User-Agent': OVERPASS_UA},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                overpass_result = _parse_overpass(data, osm_type, osm_id)
                if overpass_result:
                    logger.debug(f"Overpass hit: {osm_type}/{osm_id}")
            elif resp.status_code == 429:
                overpass_error = "Overpass rate limited"
                logger.warning(f"Overpass 429 for {osm_type}/{osm_id}")
            else:
                overpass_error = f"Overpass HTTP {resp.status_code}"
    except Exception as e:
        overpass_error = str(e)
        logger.warning(f"Overpass error for {osm_type}/{osm_id}: {e}")

    if overpass_result:
        overpass_result = _enrich_with_overture(overpass_result, osm_type, osm_id)
        overpass_result = _enrich_with_google(overpass_result, osm_type, osm_id)
        cache_put(osm_type, osm_id, overpass_result, 'overpass')
        return overpass_result, 200

    # 4. Both failed
    if nominatim_error and overpass_error:
        logger.error(f"Both sources failed for {osm_type}/{osm_id}: "
                     f"Nominatim={nominatim_error}, Overpass={overpass_error}")
        return {'error': 'Both data sources unavailable'}, 502

    # Not found in either source (no errors, just empty results)
    return {'error': f'{osm_type}/{osm_id} not found'}, 404
