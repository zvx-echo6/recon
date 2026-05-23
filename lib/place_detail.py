"""
Wiki-index lookup for place enrichment.

Provides lookup_wiki_index(wikidata_id, name, country_code) — a pure read of the
local wiki_index.db, used by the /api/wiki-enrich endpoint (navi-places
HTTP-fetches wiki enrichment instead of reading the 2.1 GB DB directly).
"""
import os
import sqlite3

from .utils import setup_logging

logger = setup_logging('recon.place_detail')


# ── Wiki Index enrichment ───────────────────────────────────────────────

_wiki_index_conn = None

def _get_wiki_index_db():
    global _wiki_index_conn
    if _wiki_index_conn is not None:
        return _wiki_index_conn

    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wiki_index.db")
    if not os.path.exists(db_path):
        logger.debug(f"wiki_index.db not found at {db_path}")
        return None

    _wiki_index_conn = sqlite3.connect(db_path, check_same_thread=False)
    _wiki_index_conn.row_factory = sqlite3.Row
    logger.info(f"Wiki index DB ready at {db_path}")
    return _wiki_index_conn


def lookup_wiki_index(wikidata_id=None, name=None, country_code=None):
    """Standalone wiki_index lookup, extracted for the /api/wiki-enrich endpoint
    (extraction #5: navi-places HTTP-fetches wiki enrichment instead of reading
    the 2.1 GB wiki_index.db directly).

    Mirrors the lookup that `_enrich_with_wiki_index` performs in-process:
    by wikidata_id first, then a name + country_code fallback. Returns a dict of
    wiki enrichment fields (only those present), or None if there is no match or
    the wiki_index DB is unavailable. Pure DB read — no feature-flag gating
    (callers decide whether to call) and never raises.

    NOTE: additive only — `_enrich_with_wiki_index` is intentionally left
    untouched here; it can be DRY-refactored to delegate to this in a later PR.
    """
    db = _get_wiki_index_db()
    if not db:
        return None

    try:
        cur = db.cursor()
        row = None

        if wikidata_id:
            wid = wikidata_id
            if isinstance(wid, str) and wid.startswith("http"):
                wid = wid.split("/")[-1]
            cur.execute(
                "SELECT summary, wiki_population, wikipedia_title, wikivoyage_title FROM wiki_places WHERE wikidata_id = ?",
                (wid,)
            )
            row = cur.fetchone()

        if not row and name and country_code:
            cur.execute(
                "SELECT summary, wiki_population, wikipedia_title, wikivoyage_title FROM wiki_places WHERE place_name = ? AND country_code = ? LIMIT 1",
                (name, country_code.lower())
            )
            row = cur.fetchone()

        if not row:
            return None

        out = {}
        if row["summary"]:
            out["wiki_summary"] = row["summary"]
        if row["wiki_population"]:
            try:
                out["wiki_population"] = int(row["wiki_population"])
            except (ValueError, TypeError):
                out["wiki_population"] = row["wiki_population"]
        if row["wikipedia_title"]:
            title = row["wikipedia_title"].replace(" ", "_")
            out["wiki_url"] = f"https://en.wikipedia.org/wiki/{title}"
        if row["wikivoyage_title"]:
            title = row["wikivoyage_title"].replace(" ", "_")
            out["wikivoyage_url"] = f"https://en.wikivoyage.org/wiki/{title}"

        return out or None

    except Exception as e:
        logger.debug(f"wiki_index lookup error: {e}")
        return None
