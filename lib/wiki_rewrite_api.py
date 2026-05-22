"""Wiki-rewrite API — read-only HTTP wrapper over wiki_rewrite.rewrite_wiki_link.

Extraction #5 prep: lets the (future) navi-places service rewrite OSM wiki tags
to local Kiwix URLs over HTTP instead of importing recon's wiki_rewrite module
(which talks to Kiwix and the wiki_cache table in /opt/recon/data/place_cache.db).
Additive only — does not change place_detail's in-process `_enrich_wiki_links`.

  GET /api/wiki-rewrite?tag=<wikipedia|wikidata|wikivoyage|appropedia>&value=<raw>

Public (no auth), matching /api/place/* and /api/wiki-enrich. 400 on missing
value or unknown tag. No 404 — an unclassifiable value returns the original
value with status "original" (mirrors rewrite_wiki_link).
"""
from flask import Blueprint, request, jsonify

from .wiki_rewrite import rewrite_wiki_link

wiki_rewrite_bp = Blueprint('wiki_rewrite', __name__)

_KNOWN_TAGS = {'wikipedia', 'wikidata', 'wikivoyage', 'appropedia'}


@wiki_rewrite_bp.route('/api/wiki-rewrite')
def api_wiki_rewrite():
    tag = (request.args.get('tag') or '').strip().lower()
    value = (request.args.get('value') or '').strip()

    if not value:
        return jsonify({'error': 'value is required'}), 400
    if tag not in _KNOWN_TAGS:
        return jsonify({'error': f"tag must be one of {sorted(_KNOWN_TAGS)}"}), 400

    url, status = rewrite_wiki_link(tag, value)
    return jsonify({'url': url, 'status': status})
