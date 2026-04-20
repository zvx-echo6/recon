"""Navigation tools: geocoding via Photon and routing via Valhalla."""

import math
import re
import requests

from .utils import setup_logging

logger = setup_logging('recon.nav_tools')

PHOTON_URL = "http://localhost:2322"
VALHALLA_URL = "http://localhost:8002"

# Regional bias for Photon searches (Idaho-centric for Matt's use case).
# Adjustable — Photon uses these to rank nearby results higher.
GEOCODE_BIAS_LAT = 42.5736
GEOCODE_BIAS_LON = -114.6066
GEOCODE_BIAS_ZOOM = 10

# Distance threshold (meters) for annotating Photon results with address
# book labels.  75m covers GPS jitter + geocoder imprecision.
ADDRESS_BOOK_ANNOTATION_RADIUS_M = 75

# Coordinate regex — handles comma-separated and space-separated forms.
_COORD_RE = re.compile(
    r'^\s*(-?\d+\.\d+)\s*[,\s]\s*(-?\d+\.\d+)\s*$'
)

VALID_MODES = {"auto", "pedestrian", "bicycle", "truck"}


def _parse_coords(text: str):
    """Return (lat, lon) if text looks like coordinates with valid bounds, else None."""
    m = _COORD_RE.match(text.strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _haversine_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters between two (lat, lon) points."""
    R = 6_371_000  # Earth radius in meters
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify_photon_feature(props, index):
    """Classify a Photon feature into (type, confidence)."""
    osm_key = props.get('osm_key', '')
    osm_value = props.get('osm_value', '')
    feat_type = props.get('type', '')
    has_housenumber = bool(props.get('housenumber'))

    # Type classification
    if has_housenumber or osm_value in ('house', 'residential'):
        result_type = 'street_address'
    elif feat_type in ('city', 'town', 'village', 'hamlet', 'county', 'state', 'country'):
        result_type = 'locality'
    elif osm_key in ('amenity', 'shop', 'tourism', 'leisure') or osm_value:
        result_type = 'poi'
    else:
        result_type = 'poi'

    # Confidence — simple positional heuristic
    if index == 0:
        confidence = 'high'
    elif index <= 2:
        confidence = 'medium'
    else:
        confidence = 'low'

    return result_type, confidence


def _photon_feature_to_name(props):
    """Build a display name from a Photon feature's properties."""
    parts = []
    housenumber = props.get('housenumber')
    street = props.get('street')
    name = props.get('name', '')

    if housenumber and street:
        parts.append(f"{housenumber} {street}")
        if name and name != street:
            parts.append(name)
    elif name:
        parts.append(name)
    elif street:
        parts.append(street)

    for key in ('city', 'county', 'state', 'country'):
        v = props.get(key)
        if v and (not parts or v != parts[-1]):
            parts.append(v)

    return ', '.join(p for p in parts if p) or 'Unknown'


def _annotate_with_address_book(results):
    """Add labeled_as to results within ADDRESS_BOOK_ANNOTATION_RADIUS_M of an address book entry."""
    try:
        from . import address_book
        entries = address_book.load()
    except Exception:
        return

    for result in results:
        rlat, rlon = result.get('lat'), result.get('lon')
        if rlat is None or rlon is None:
            continue
        for entry in entries:
            elat, elon = entry.get('lat'), entry.get('lon')
            if elat is None or elon is None:
                continue
            dist = _haversine_m(rlat, rlon, elat, elon)
            if dist <= ADDRESS_BOOK_ANNOTATION_RADIUS_M:
                result['labeled_as'] = entry['name']
                break


def _geocode(query: str):
    """Geocode a place name via address book then Photon. Returns (lat, lon, display_name) or raises.

    Used internally by route() — returns a simple (lat, lon, name) tuple.
    For the full ranked-results API, use geocode() instead.
    """
    result = geocode(query, limit=1)
    results = result.get('results', [])
    if not results:
        raise ValueError(f"Could not find location: {query}")
    top = results[0]
    return top['lat'], top['lon'], top['name']



def geocode(query: str, limit: int = 10):
    """
    Photon-first geocoding with ranked results.

    Chain:
      1. Coordinate detection (pre-search)
      2. Address book nickname short-circuit (single-word queries only)
      3. Photon search (primary, biased to Idaho region)
      4. Address book proximity annotation (post-Photon, 75m radius)

    Returns dict: {query, results: [...], count: N}
    Always 200-safe — empty results list is valid, never raises.

    Netsyms is preserved at /api/netsyms/lookup for direct structured
    access.  Enrichment of Photon street-address hits with USPS plus4
    from Netsyms is a planned follow-up (not wired here).
    """
    limit = max(1, min(limit, 20))
    q = (query or '').strip()
    empty = {'query': q, 'results': [], 'count': 0}

    if not q:
        return empty

    # ── 1. Coordinate detection ──
    coords = _parse_coords(q)
    if coords:
        return {
            'query': q,
            'results': [{
                'name': q,
                'lat': coords[0],
                'lon': coords[1],
                'source': 'coordinates',
                'confidence': 'exact',
                'type': 'coordinates',
                'raw': None,
            }],
            'count': 1,
        }

    # ── 2. Address book nickname short-circuit ──
    # Only short-circuit on single-word queries ("home", "work").
    # Multi-word queries fall through to Photon for proper ranking.
    normalized_q = ' '.join(q.lower().replace(',', ' ').split())
    is_single_word = ' ' not in normalized_q
    try:
        from . import address_book
        ab_match = address_book.lookup(q)
        if (ab_match
                and ab_match['confidence'] == 'exact'
                and ab_match.get('lat') and ab_match.get('lon')
                and is_single_word):
            logger.info("geocode: nickname short-circuit %r → %s", q, ab_match['name'])
            return {
                'query': q,
                'results': [{
                    'name': ab_match.get('address') or ab_match['name'],
                    'lat': ab_match['lat'],
                    'lon': ab_match['lon'],
                    'source': 'address_book',
                    'confidence': 'exact',
                    'type': 'nickname',
                    'raw': ab_match,
                }],
                'count': 1,
            }
    except Exception as e:
        logger.debug("geocode: address_book lookup failed: %s", e)

    # ── 3. Photon search (primary) ──
    results = []
    try:
        params = {
            'q': q,
            'limit': limit,
            'lat': GEOCODE_BIAS_LAT,
            'lon': GEOCODE_BIAS_LON,
            'zoom': GEOCODE_BIAS_ZOOM,
        }
        resp = requests.get(f"{PHOTON_URL}/api", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        for i, feature in enumerate(data.get('features', [])):
            props = feature.get('properties', {})
            geom_coords = feature.get('geometry', {}).get('coordinates', [0, 0])
            result_type, confidence = _classify_photon_feature(props, i)
            name = _photon_feature_to_name(props)
            results.append({
                'name': name,
                'lat': geom_coords[1],
                'lon': geom_coords[0],
                'source': 'photon',
                'confidence': confidence,
                'type': result_type,
                'raw': props,
            })
    except requests.RequestException as e:
        logger.warning("geocode: Photon request failed: %s", e)
    except Exception as e:
        logger.warning("geocode: Photon parse error: %s", e)

    # ── 4. Address book annotation (post-Photon) ──
    _annotate_with_address_book(results)

    logger.info("geocode: %r → %d results", q, len(results))
    return {'query': q, 'results': results, 'count': len(results)}


def reverse_geocode(lat: float, lon: float) -> str:
    """Reverse geocode coordinates via Photon. Returns formatted address string."""
    try:
        resp = requests.get(
            f"{PHOTON_URL}/reverse",
            params={"lat": lat, "lon": lon, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Navigation service unavailable")

    data = resp.json()
    features = data.get("features", [])
    if not features:
        return f"{lat}, {lon}"

    props = features[0]["properties"]
    parts = []
    for key in ("name", "housenumber", "street", "city", "state", "country", "postcode"):
        v = props.get(key)
        if v:
            parts.append(v)
    return ", ".join(parts) if parts else f"{lat}, {lon}"


def route(origin: str, destination: str, mode: str = "auto") -> dict:
    """
    Get a route between two locations.

    Args:
        origin: Starting location — address, place name, or "lat,lon"
        destination: Destination — address, place name, or "lat,lon"
        mode: Travel mode — auto, pedestrian, bicycle, truck

    Returns:
        dict with summary, maneuvers, origin/destination info, and raw shape
    """
    if mode not in VALID_MODES:
        mode = "auto"

    # Geocode both endpoints
    orig_lat, orig_lon, orig_name = _geocode(origin)
    dest_lat, dest_lon, dest_name = _geocode(destination)

    # Query Valhalla
    valhalla_req = {
        "locations": [
            {"lat": orig_lat, "lon": orig_lon},
            {"lat": dest_lat, "lon": dest_lon},
        ],
        "costing": mode,
        "directions_options": {"units": "miles"},
    }

    try:
        resp = requests.post(
            f"{VALHALLA_URL}/route",
            json=valhalla_req,
            timeout=30,
        )
    except requests.RequestException:
        raise RuntimeError("Navigation service unavailable")

    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("error", "Unknown routing error")
        except Exception:
            msg = f"Routing error (HTTP {resp.status_code})"
        raise RuntimeError(f"No route found between locations: {msg}")

    data = resp.json()
    trip = data["trip"]
    summary = trip["summary"]
    leg = trip["legs"][0]

    # Build maneuver list
    maneuvers = []
    for m in leg["maneuvers"]:
        streets = m.get("street_names", [])
        maneuvers.append({
            "instruction": m["instruction"],
            "distance_miles": round(m.get("length", 0), 2),
            "street_name": streets[0] if streets else "",
            "type": m.get("type", 0),
            "verbal_succinct": m.get("verbal_succinct_transition_instruction", ""),
        })

    return {
        "origin": {"name": orig_name, "lat": orig_lat, "lon": orig_lon},
        "destination": {"name": dest_name, "lat": dest_lat, "lon": dest_lon},
        "summary": {
            "distance_miles": round(summary["length"], 1),
            "time_minutes": round(summary["time"] / 60, 1),
            "mode": mode,
        },
        "maneuvers": maneuvers,
        "shape": leg.get("shape", ""),
    }
