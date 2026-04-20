"""
RECON Address Book API — Flask Blueprint.

GET /api/address_book/lookup?q=<query>  — best match or 404
GET /api/address_book/list              — all entries
"""

from flask import Blueprint, request, jsonify

from . import address_book

address_book_bp = Blueprint('address_book', __name__)


@address_book_bp.route('/api/address_book/lookup')
def api_address_book_lookup():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'Missing q parameter'}), 400

    result = address_book.lookup(q)
    if result is None:
        return '', 404

    return jsonify(result)


@address_book_bp.route('/api/address_book/list')
def api_address_book_list():
    entries = address_book.list_all()
    return jsonify(entries)
