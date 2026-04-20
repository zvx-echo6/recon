"""
RECON Netsyms API + Geocode — Flask Blueprints.

GET /api/netsyms/lookup?q=<free text>&country=<optional>
GET /api/netsyms/health
GET /api/geocode?q=<query>&limit=<N>  (Photon-first search with ranked results)
"""

from flask import Blueprint, request, jsonify

from . import netsyms
from . import address_book
from . import nav_tools
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

    result = nav_tools.geocode(q, limit=limit)
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
