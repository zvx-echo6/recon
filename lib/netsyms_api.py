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
