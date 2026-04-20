"""
RECON Netsyms API + Geocode chain — Flask Blueprints.

GET /api/netsyms/lookup?q=<free text>&country=<optional>
GET /api/netsyms/health
GET /api/geocode?q=<query>   (full 3-tier chain: address_book → netsyms → photon)
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
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'Missing q parameter'}), 400

    result = nav_tools.geocode(q)
    if result is None:
        return jsonify({'error': 'No results', 'query': q}), 404

    return jsonify(result)
