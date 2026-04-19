"""Navigation tools: geocoding via Photon and routing via Valhalla."""

import re
import requests

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
    """Geocode a place name via Photon. Returns (lat, lon, display_name) or raises."""
    coords = _parse_coords(query)
    if coords:
        return coords[0], coords[1], query

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
