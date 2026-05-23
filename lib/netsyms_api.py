"""
RECON Netsyms API — Flask Blueprint.

GET /api/netsyms/lookup?q=<free text>&country=<optional>
GET /api/netsyms/health
"""

from flask import Blueprint, request, jsonify

from . import netsyms
from . import address_book
from .utils import setup_logging

logger = setup_logging('recon.netsyms_api')

netsyms_bp = Blueprint('netsyms', __name__)


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
