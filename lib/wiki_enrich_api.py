"""Wiki-enrich API — read-only HTTP wrapper over the wiki_index lookup.

Extraction #5 prep: lets the (future) navi-places service fetch wiki enrichment
over HTTP instead of reading recon's 2.1 GB data/wiki_index.db directly. Additive
only — does not change place_detail's in-process `_enrich_with_wiki_index` path.

  GET /api/wiki-enrich?wikidata=<Qid>          (primary key)
  GET /api/wiki-enrich?name=<name>&country=<cc> (fallback key)

Public (no auth), matching /api/place/*. 400 if no usable key; 404 on no match.
"""
from flask import Blueprint, request, jsonify

from .place_detail import lookup_wiki_index

wiki_enrich_bp = Blueprint('wiki_enrich', __name__)


@wiki_enrich_bp.route('/api/wiki-enrich')
def api_wiki_enrich():
    wikidata = (request.args.get('wikidata') or '').strip() or None
    name = (request.args.get('name') or '').strip() or None
    country = (request.args.get('country') or '').strip() or None

    if not wikidata and not (name and country):
        return jsonify({'error': 'provide ?wikidata=<id> or ?name=<name>&country=<cc>'}), 400

    result = lookup_wiki_index(wikidata_id=wikidata, name=name, country_code=country)
    if result is None:
        return jsonify({'error': 'no wiki match'}), 404
    return jsonify(result)
