"""Tests for the /api/wiki-enrich endpoint (extraction #5 prep).

Plain-assert style (matching the other lib *_test.py; recon's venv has no
pytest). Builds a minimal Flask app with only wiki_enrich_bp registered (avoids
importing the full recon app) and points place_detail's lazy wiki_index
connection at an in-memory fixture DB. Run with pytest, or directly:
    python -m lib.wiki_enrich_api_test
"""
import sqlite3

from flask import Flask

from lib import place_detail
from lib.wiki_enrich_api import wiki_enrich_bp


def _client():
    """Fresh in-memory wiki_index fixture + a minimal app with just the route."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE wiki_places (wikidata_id TEXT, place_name TEXT, country_code TEXT, "
        "summary TEXT, wiki_population TEXT, wikipedia_title TEXT, wikivoyage_title TEXT)"
    )
    conn.execute(
        "INSERT INTO wiki_places VALUES (?,?,?,?,?,?,?)",
        ("Q830149", "Filer", "us", "A city in Idaho.", "2508", "Filer, Idaho", "Filer"),
    )
    conn.commit()
    # Point the lazy module-level connection at the fixture so
    # _get_wiki_index_db()/lookup_wiki_index() use it (bypasses the file path).
    place_detail._wiki_index_conn = conn
    app = Flask(__name__)
    app.register_blueprint(wiki_enrich_bp)
    return app.test_client()


def test_wiki_enrich_hit_by_wikidata():
    resp = _client().get("/api/wiki-enrich?wikidata=Q830149")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["wiki_summary"] == "A city in Idaho."
    assert d["wiki_population"] == 2508  # cast to int
    assert d["wiki_url"] == "https://en.wikipedia.org/wiki/Filer,_Idaho"
    assert d["wikivoyage_url"] == "https://en.wikivoyage.org/wiki/Filer"


def test_wiki_enrich_no_match_404():
    resp = _client().get("/api/wiki-enrich?wikidata=Q9999999")
    assert resp.status_code == 404, resp.status_code


def test_wiki_enrich_name_country_fallback():
    resp = _client().get("/api/wiki-enrich?name=Filer&country=US")
    assert resp.status_code == 200, resp.status_code
    assert resp.get_json()["wiki_summary"] == "A city in Idaho."


def test_wiki_enrich_no_key_400():
    c = _client()
    assert c.get("/api/wiki-enrich").status_code == 400
    # name without country is not a usable key
    assert c.get("/api/wiki-enrich?name=Filer").status_code == 400


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {_name}: {exc!r}")
    print("OK" if failures == 0 else f"{failures} FAILED")
    raise SystemExit(1 if failures else 0)
