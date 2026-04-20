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


def geocode(query: str, limit: int = 10):
    """Delegate to the structured geocode module. See lib/geocode.py."""
    from . import geocode as geocode_mod
    return geocode_mod.geocode(query, limit=limit)


def _geocode(query: str):
    """Internal: returns (lat, lon, display_name) tuple for route()."""
    result = geocode(query, limit=1)
    results = result.get('results', [])
    if not results:
        raise ValueError(f"Could not find location: {query}")
    top = results[0]
    return top['lat'], top['lon'], top['name']


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
