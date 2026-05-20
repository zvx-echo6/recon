"""
RECON Netsyms API + Geocode — Flask Blueprints.

GET /api/netsyms/lookup?q=<free text>&country=<optional>
GET /api/netsyms/health
GET /api/geocode?q=<query>&limit=<N>  (Photon-first search with ranked results)
GET /api/reverse/<lat>/<lon>          (localhost-sourced enrichment bundle for Central)
"""

import sqlite3
import threading

from cachetools import TTLCache
from flask import Blueprint, request, jsonify

from . import netsyms
from . import address_book
from . import nav_tools
from .geocode import PHOTON_URL
from .utils import setup_logging

logger = setup_logging('recon.netsyms_api')

netsyms_bp = Blueprint('netsyms', __name__)
geocode_bp = Blueprint('geocode', __name__)


@netsyms_bp.route('/api/netsyms/lookup')
def api_netsyms_lookup():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'Missing q parameter'}), 400

    country = request.args.get('country', '').strip() or None
    results = netsyms.lookup_free_text(q, country_hint=country)
    return jsonify({'results': results, 'count': len(results), 'query': q})


@netsyms_bp.route('/api/netsyms/health')
def api_netsyms_health():
    return jsonify(netsyms.health())



def _safe_float(val, lo, hi):
    """Parse val as float; return None if missing, non-numeric, or out of [lo, hi]."""
    if val is None:
        return None
    try:
        f = float(val)
        if lo <= f <= hi:
            return f
    except (ValueError, TypeError):
        pass
    return None

@geocode_bp.route('/api/geocode')
def api_geocode():
    """
    Photon-first geocoding with ranked candidates.

    GET /api/geocode?q=<query>&limit=<N>

    Always returns 200 OK with:
      {query, results: [{name, lat, lon, source, confidence, type, raw, ...}], count}

    - source: "address_book" | "coordinates" | "photon"
    - confidence: "exact" | "high" | "medium" | "low"
    - type: "nickname" | "coordinates" | "street_address" | "poi" | "locality"
    - labeled_as: present when result is within 75m of an address book entry
    - Empty results array is valid (no match). No 404s.
    """
    q = request.args.get('q', '').strip()
    limit = request.args.get('limit', '10')
    try:
        limit = max(1, min(int(limit), 20))
    except (ValueError, TypeError):
        limit = 10

    # Viewport bias parameters (optional)
    lat = _safe_float(request.args.get("lat"), -90, 90)
    lon = _safe_float(request.args.get("lon"), -180, 180)
    zoom = _safe_float(request.args.get("zoom"), 0, 22)

    result = nav_tools.geocode(q, limit=limit, lat=lat, lon=lon, zoom=zoom)
    return jsonify(result)


@geocode_bp.route('/api/reverse')
def api_reverse():
    """
    Reverse geocode coordinates via Photon.

    GET /api/reverse?lat=X&lon=Y

    Returns same shape as /api/geocode:
      {query: "lat,lon", results: [{name, lat, lon, source, type, raw, ...}], count}

    Returns 200 OK with empty results on no match. 400 on invalid coords.
    """
    try:
        lat = float(request.args.get('lat', ''))
        lon = float(request.args.get('lon', ''))
    except (ValueError, TypeError):
        return jsonify({'error': 'Missing or invalid lat/lon parameters'}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({'error': 'Coordinates out of range'}), 400

    query_str = f"{lat},{lon}"

    try:
        import requests as http_requests
        resp = http_requests.get(
            "http://localhost:2322/reverse",
            params={"lat": lat, "lon": lon, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
    except Exception:
        logger.warning("Photon reverse geocode failed for %s", query_str)
        return jsonify({'query': query_str, 'results': [], 'count': 0})

    if not features:
        return jsonify({'query': query_str, 'results': [], 'count': 0})

    from .geocode import _parse_photon_features
    results = _parse_photon_features(features, source='photon_reverse')

    return jsonify({'query': query_str, 'results': results, 'count': len(results)})


# ─────────────────────────────────────────────────────────────────────────
#  /api/reverse/<lat>/<lon> — localhost-sourced enrichment bundle (Central)
#
#  Sibling to the query-string /api/reverse above; that route is unchanged.
#  Every component is sourced from localhost only (Photon, timezones.sqlite,
#  in-process landclass/PostGIS, Valhalla). Each lookup is independent: a
#  component failure logs a warning and yields null — never a 5xx.
# ─────────────────────────────────────────────────────────────────────────

_TZ_DB_PATH = "/mnt/nav/sources/timezones.sqlite"
_VALHALLA_HEIGHT_URL = "http://localhost:8002/height"

# Full bundle cache: key=(round(lat,4), round(lon,4)) -> dict. ~10k entries, 24h TTL.
_REVERSE_BUNDLE_CACHE = TTLCache(maxsize=10_000, ttl=86_400)
_REVERSE_BUNDLE_LOCK = threading.Lock()

_BUNDLE_KEYS = ('name', 'city', 'county', 'state', 'country',
                'postal_code', 'timezone', 'landclass', 'elevation_m')


def _spatialite_blob_to_wkb(blob):
    """Recover standard WKB from a SpatiaLite geometry BLOB.

    Layout: [00][endian][srid:4][mbr:32][7C][WKB body][FE]. The body omits the
    leading byte-order marker, so we re-prepend it and drop the trailing 0xFE.
    """
    return bytes([blob[1]]) + blob[39:-1]


def _reverse_photon(lat, lon):
    """Nearest-feature admin fields from local Photon. Returns the six address
    fields (any value may be None). Mirrors the existing /api/reverse call."""
    import requests as http_requests
    resp = http_requests.get(
        f"{PHOTON_URL}/reverse",
        params={"lat": lat, "lon": lon, "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return {}
    props = features[0].get("properties", {})
    return {
        "name": props.get("name"),
        "city": props.get("city"),
        "county": props.get("county"),
        "state": props.get("state"),
        "country": props.get("country"),
        "postal_code": props.get("postcode"),
    }


def _reverse_timezone(lat, lon):
    """IANA tzid for the point from local timezones.sqlite (SpatiaLite tz_world).

    Uses the table's R-tree index for an MBR prefilter, then shapely
    point-in-polygon on the few candidates. Returns None if unresolved.
    """
    from shapely import wkb
    from shapely.geometry import Point
    con = sqlite3.connect(f"file:{_TZ_DB_PATH}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT pkid FROM idx_tz_world_geom "
            "WHERE xmin<=? AND xmax>=? AND ymin<=? AND ymax>=?",
            (lon, lon, lat, lat),
        )
        candidates = [r[0] for r in cur.fetchall()]
        if not candidates:
            return None
        pt = Point(lon, lat)
        for pk in candidates:
            row = cur.execute(
                "SELECT tzid, geom FROM tz_world WHERE pk_uid=?", (pk,)
            ).fetchone()
            if row and wkb.loads(_spatialite_blob_to_wkb(row[1])).contains(pt):
                return row[0]
        return None
    finally:
        con.close()


def _reverse_landclass(lat, lon):
    """Most-specific PAD-US land class for the point, looked up in-process.
    Returns None when there is no coverage or landclass is unavailable."""
    from .landclass import lookup_landclass, format_summary
    return format_summary(lookup_landclass(lat, lon))


def _reverse_elevation(lat, lon):
    """Elevation in metres from local Valhalla /height. None on failure."""
    import requests as http_requests
    resp = http_requests.post(
        _VALHALLA_HEIGHT_URL,
        json={"shape": [{"lat": lat, "lon": lon}]},
        timeout=10,
    )
    resp.raise_for_status()
    heights = resp.json().get("height", [])
    return heights[0] if heights else None


@geocode_bp.route('/api/reverse/<lat>/<lon>')
def api_reverse_bundle(lat, lon):
    """Localhost-sourced reverse-geocode enrichment bundle for Central.

    GET /api/reverse/<lat>/<lon>

    Always returns 200 with EXACTLY these keys (any may be null):
      name, city, county, state, country, postal_code, timezone, landclass, elevation_m

    lat/lon are parsed manually (not via Flask's <float:> converter, which
    rejects negative and integer coordinates) so out-of-range or unparseable
    input yields 400 per contract; 503 is reserved for catastrophic failure.
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return jsonify({'error': 'lat and lon must be numbers'}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({'error': 'lat must be -90..90, lon must be -180..180'}), 400

    key = (round(lat, 4), round(lon, 4))
    with _REVERSE_BUNDLE_LOCK:
        cached = _REVERSE_BUNDLE_CACHE.get(key)
    if cached is not None:
        return jsonify(cached)

    bundle = {k: None for k in _BUNDLE_KEYS}

    try:
        bundle.update(_reverse_photon(lat, lon))
    except Exception:
        logger.warning("reverse-bundle: Photon lookup failed for %s,%s", lat, lon)
    try:
        bundle['timezone'] = _reverse_timezone(lat, lon)
    except Exception:
        logger.warning("reverse-bundle: timezone lookup failed for %s,%s", lat, lon)
    try:
        bundle['landclass'] = _reverse_landclass(lat, lon)
    except Exception:
        logger.warning("reverse-bundle: landclass lookup failed for %s,%s", lat, lon)
    try:
        bundle['elevation_m'] = _reverse_elevation(lat, lon)
    except Exception:
        logger.warning("reverse-bundle: elevation lookup failed for %s,%s", lat, lon)

    with _REVERSE_BUNDLE_LOCK:
        _REVERSE_BUNDLE_CACHE[key] = bundle
    return jsonify(bundle)
