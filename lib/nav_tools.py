"""Navigation tools: geocoding via Photon and routing via Valhalla."""

import re
import requests

from .utils import setup_logging

logger = setup_logging('recon.nav_tools')

PHOTON_URL = "http://localhost:2322"
VALHALLA_URL = "http://localhost:8002"

_COORD_RE = re.compile(r'^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$')

VALID_MODES = {"auto", "pedestrian", "bicycle", "truck"}


def _parse_coords(text: str):
    """Return (lat, lon) if text looks like coordinates, else None."""
    m = _COORD_RE.match(text.strip())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def _geocode(query: str):
    """Geocode a place name via address book then Photon. Returns (lat, lon, display_name) or raises."""
    coords = _parse_coords(query)
    if coords:
        return coords[0], coords[1], query

    # ── Address book lookup (before Photon) ──
    try:
        from . import address_book
        match = address_book.lookup(query)
        if match and match['confidence'] == 'exact' and match.get('lat') and match.get('lon'):
            logger.info("Address book exact match: %r → %s (%s, %s)",
                        query, match['name'], match['lat'], match['lon'])
            return match['lat'], match['lon'], match.get('address') or match['name']
        elif match and match['confidence'] == 'partial':
            logger.info("Address book partial match: %r → %s (falling through to Photon)",
                        query, match['name'])
    except Exception as e:
        logger.debug("Address book lookup failed: %s", e)

    # ── Photon geocoding ──
    try:
        resp = requests.get(
            f"{PHOTON_URL}/api",
            params={"q": query, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Navigation service unavailable")

    data = resp.json()
    features = data.get("features", [])
    if not features:
        raise ValueError(f"Could not find location: {query}")

    props = features[0]["properties"]
    coords = features[0]["geometry"]["coordinates"]  # [lon, lat]
    parts = [props.get("name", "")]
    for key in ("city", "county", "state", "country"):
        v = props.get(key)
        if v and v != parts[-1]:
            parts.append(v)
    display = ", ".join(p for p in parts if p)
    return coords[1], coords[0], display  # lat, lon



def geocode(query: str):
    """
    Three-tier geocode chain returning a consistent shape.

    Chain: address_book (exact) → netsyms → photon.
    Returns dict with {name, lat, lon, source, raw} or None.
    """
    coords = _parse_coords(query)
    if coords:
        return {
            'name': query,
            'lat': coords[0],
            'lon': coords[1],
            'source': 'coordinates',
            'raw': None,
        }

    # ── Tier 1: Address book (exact match only) ──
    ab_partial = None
    try:
        from . import address_book
        match = address_book.lookup(query)
        if match and match['confidence'] == 'exact' and match.get('lat') and match.get('lon'):
            logger.info("geocode: address_book exact match: %r → %s", query, match['name'])
            return {
                'name': match.get('address') or match['name'],
                'lat': match['lat'],
                'lon': match['lon'],
                'source': 'address_book',
                'raw': match,
            }
        elif match and match['confidence'] == 'partial':
            logger.info("geocode: address_book partial match: %r → %s (continuing chain)",
                        query, match['name'])
            ab_partial = match
    except Exception as e:
        logger.debug("geocode: address_book lookup failed: %s", e)

    # ── Tier 2: Netsyms (159M US+CA addresses) ──
    netsyms_result = None
    try:
        from . import netsyms
        results = netsyms.lookup_free_text(query)
        if results:
            # Prefer results with plus4 (more precise)
            best = results[0]
            for r in results:
                if r.get('plus4') and not best.get('plus4'):
                    best = r
                    break
            addr_parts = [best['number'], best['street']]
            if best.get('street2'):
                addr_parts.append(best['street2'])
            addr_parts.extend([best['city'], best['state'], best['zipcode']])
            display = ' '.join(p for p in addr_parts if p)
            netsyms_result = {
                'name': display,
                'lat': best['lat'],
                'lon': best['lon'],
                'source': 'netsyms',
                'raw': best,
            }
            logger.info("geocode: netsyms match: %r → %s", query, display)
            return netsyms_result
    except Exception as e:
        logger.debug("geocode: netsyms lookup failed: %s", e)

    # ── Tier 3: Photon (global geocoding) ──
    try:
        resp = requests.get(
            f"{PHOTON_URL}/api",
            params={"q": query, "limit": 1},
            timeout=2,
        )
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if features:
            props = features[0]["properties"]
            coords = features[0]["geometry"]["coordinates"]  # [lon, lat]
            parts = [props.get("name", "")]
            for key in ("city", "county", "state", "country"):
                v = props.get(key)
                if v and v != parts[-1]:
                    parts.append(v)
            display = ", ".join(p for p in parts if p)
            logger.info("geocode: photon match: %r → %s", query, display)
            return {
                'name': display,
                'lat': coords[1],
                'lon': coords[0],
                'source': 'photon',
                'raw': props,
            }
    except Exception as e:
        logger.debug("geocode: photon lookup failed: %s", e)

    # ── Fallback: address book partial match ──
    if ab_partial and ab_partial.get('lat') and ab_partial.get('lon'):
        logger.info("geocode: falling back to address_book partial: %r → %s",
                    query, ab_partial['name'])
        return {
            'name': ab_partial.get('address') or ab_partial['name'],
            'lat': ab_partial['lat'],
            'lon': ab_partial['lon'],
            'source': 'address_book',
            'raw': ab_partial,
        }

    logger.info("geocode: no match for %r across all tiers", query)
    return None


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
