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


def _enrich_reverse_result_with_wiki(result):
    """
    Add wiki data to a reverse geocode result if available.
    Only runs when has_kiwix_wiki is enabled.
    """
    try:
        from .deployment_config import get_deployment_config
        deploy_config = get_deployment_config()
        features = deploy_config.get('features', {})
        if not features.get('has_kiwix_wiki', False):
            return result
    except Exception:
        return result

    try:
        from . import wiki_index
    except ImportError:
        return result

    if not wiki_index.is_available():
        return result

    # Extract match criteria from Photon raw props
    raw = result.get('raw', {})
    place_name = raw.get('name', '')
    osm_key = raw.get('osm_key', '')
    osm_value = raw.get('osm_value', '')
    state = raw.get('state', '')
    country = raw.get('country', '')

    # Extract country code (Photon uses full country name, we need code)
    country_code = raw.get('countrycode', '').lower()
    if not country_code:
        country_lower = country.lower() if country else ''
        if 'united states' in country_lower or country_lower == 'usa':
            country_code = 'us'
        elif 'canada' in country_lower:
            country_code = 'ca'

    if not place_name or not osm_key or not osm_value or not country_code:
        return result

    # Look up wiki data
    wiki_data = wiki_index.lookup_wiki(place_name, osm_key, osm_value, state, country_code)
    if wiki_data:
        # Add wiki fields to result (additive only)
        if 'wiki_summary' in wiki_data:
            result['wiki_summary'] = wiki_data['wiki_summary']
        if 'wiki_url' in wiki_data:
            result['wiki_url'] = wiki_data['wiki_url']
        if 'wikivoyage_url' in wiki_data:
            result['wikivoyage_url'] = wiki_data['wikivoyage_url']
        if 'wiki_population' in wiki_data:
            result['wiki_population'] = wiki_data['wiki_population']

    return result



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

    # Enrich results with wiki data
    results = [_enrich_reverse_result_with_wiki(r) for r in results]

    return jsonify({'query': query_str, 'results': results, 'count': len(results)})
