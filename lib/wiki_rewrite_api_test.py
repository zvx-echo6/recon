"""Tests for the /api/wiki-rewrite endpoint (extraction #5 prep).

Plain-assert style (recon's venv has no pytest). Builds a minimal Flask app
with only wiki_rewrite_bp registered. Mocks `wiki_rewrite.check_kiwix_has_article`
to control the local-Kiwix-hit vs. fallback paths without touching Kiwix or the
wiki_cache DB. classify_wiki_link (pure regex) runs for real. Run with pytest,
or directly:  python -m lib.wiki_rewrite_api_test
"""
from flask import Flask

from lib import wiki_rewrite
from lib.wiki_rewrite_api import wiki_rewrite_bp


def _client(kiwix_hit):
    """kiwix_hit: (found_bool, url) returned by a stubbed check_kiwix_has_article."""
    wiki_rewrite.check_kiwix_has_article = lambda source_type, article_id: kiwix_hit
    app = Flask(__name__)
    app.register_blueprint(wiki_rewrite_bp)
    return app.test_client()


def test_local_kiwix_hit():
    url = "https://wiki.echo6.co/content/wikipedia/Filer,_Idaho"
    c = _client((True, url))
    resp = c.get("/api/wiki-rewrite?tag=wikipedia&value=Filer, Idaho")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["status"] == "local"
    assert d["url"] == url


def test_public_fallback_when_not_in_kiwix():
    c = _client((False, None))  # not in Kiwix -> canonical public URL
    resp = c.get("/api/wiki-rewrite?tag=wikipedia&value=Filer")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["status"] == "public"
    assert d["url"] == "https://en.wikipedia.org/wiki/Filer"


def test_unclassifiable_returns_original():
    # 'wikidata' requires a Q-id; a non-matching value -> classify None -> original.
    c = _client((False, None))
    resp = c.get("/api/wiki-rewrite?tag=wikidata&value=not-a-qid")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["status"] == "original"
    assert d["url"] == "not-a-qid"


def test_missing_value_400():
    c = _client((False, None))
    assert c.get("/api/wiki-rewrite?tag=wikipedia").status_code == 400


def test_unknown_tag_400():
    c = _client((False, None))
    assert c.get("/api/wiki-rewrite?tag=facebook&value=x").status_code == 400


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
