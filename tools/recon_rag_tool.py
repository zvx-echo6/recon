"""
title: RECON Knowledge Base
author: Echo6
version: 5.0.0
description: RAG filter with three-tier cascade: Qdrant (domain knowledge) → Kiwix (offline wiki) → SearXNG (web search). Supports intent-based metadata filtering, FlashRank neural reranking with MMR diversity, Ollama-powered query expansion, transcript source boosting, semantic query routing with inline navigation, and address book place resolution.
"""

import logging
import json
import math
import re
import threading
import html
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Callable, Awaitable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote

import requests
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Module-level source store: keyed by chat_id so inlet/outlet share state
# even if OWI instantiates separate Filter objects per call.
_SOURCE_STORE: dict[str, list] = {}

# ── CASCADE CONFIGURATION (v5.0.0) ───────────────────────────────────────────
# FlashRank score threshold for Tier 1 (Qdrant). Below this, fall through to Tier 2.
# Based on calibration: RECON queries cluster at 0.95-1.0, misses below 0.3.
# 0.5 is conservative - will let more through to Kiwix than strictly necessary.
CASCADE_CONFIDENCE_THRESHOLD = 0.5

# Kiwix-serve configuration
KIWIX_BASE_URL = "http://localhost:8430"
KIWIX_SEARCH_TIMEOUT = 5  # seconds
KIWIX_ARTICLE_TIMEOUT = 5  # seconds
KIWIX_MAX_RESULTS = 3

# SearXNG configuration
SEARXNG_URL = "http://192.168.1.102:8080"
SEARXNG_TIMEOUT = 5  # seconds
SEARXNG_MAX_RESULTS = 5

# Cascade logging
CASCADE_LOG_PATH = Path("/opt/recon/logs/cascade.jsonl")

# ── Semantic Query Router (v4.3.0) ───────────────────────────────────────────
ROUTE_EXAMPLES = {
    "nav_route": [
        "how do I get to Boise",
        "directions to Twin Falls",
        "how do I get from Buhl to Boise",
        "drive from Jerome to Sun Valley",
        "route from Boise to McCall",
        "what's the fastest way to Sun Valley",
        "how far is it to Twin Falls",
        "take me to Shoshone",
        "navigate to the airport",
        "how do I drive to Salt Lake City",
        "walking directions to the park",
        "bike route to downtown",
    ],
    "nav_reverse_geocode": [
        "what town is at 42.5, -114.7",
        "where am I right now",
        "what is at coordinates 43.6, -116.2",
        "what location is 42.574, -114.607",
        "where is this place 44.0, -114.3",
        "what city is near 42.7, -114.5",
        "reverse geocode 43.0, -115.0",
        "what's at this location 42.9, -114.8",
    ],
    "direct_answer": [
        "hello",
        "hey aurora",
        "good morning",
        "thanks",
        "thank you",
        "what's your name",
        "who are you",
        "tell me a joke",
        "how are you",
        "hi there",
    ],
    "rag_search": [
        "what does the survival manual say about water",
        "how to purify water in the field",
        "how to treat a gunshot wound",
        "what is the ranger handbook chapter on patrolling",
        "field manual water purification",
        "how to build a shelter in the wilderness",
        "tactical combat casualty care procedures",
        "what does FM 21-76 say about fire starting",
    ],
}

_ROUTE_CENTROIDS: dict | None = None
_ROUTER_LOCK = threading.Lock()


def _embed_batch_router(texts: list[str], tei_url: str) -> list[list[float]]:
    resp = requests.post(tei_url, json={"inputs": texts}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _compute_centroid(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    dim = len(vectors[0])
    centroid = [0.0] * dim
    for vec in vectors:
        for i in range(dim):
            centroid[i] += vec[i]
    for i in range(dim):
        centroid[i] /= n
    return centroid


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
        norm_a += a[i] * a[i]
        norm_b += b[i] * b[i]
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0:
        return 0.0
    return dot / denom


def _ensure_centroids(tei_url: str) -> dict[str, list[float]]:
    global _ROUTE_CENTROIDS
    if _ROUTE_CENTROIDS is not None:
        return _ROUTE_CENTROIDS
    with _ROUTER_LOCK:
        if _ROUTE_CENTROIDS is not None:
            return _ROUTE_CENTROIDS
        all_texts = []
        route_ranges: dict[str, tuple[int, int]] = {}
        offset = 0
        for route, examples in ROUTE_EXAMPLES.items():
            route_ranges[route] = (offset, offset + len(examples))
            all_texts.extend(examples)
            offset += len(examples)
        all_vectors = _embed_batch_router(all_texts, tei_url)
        centroids = {}
        for route, (start, end) in route_ranges.items():
            centroids[route] = _compute_centroid(all_vectors[start:end])
        _ROUTE_CENTROIDS = centroids
        return _ROUTE_CENTROIDS


def _classify_query(
    query: str,
    tei_url: str,
    threshold: float = 0.45,
) -> tuple[str, float]:
    """Classify query intent. Returns ("rag_search", 0.0) on any failure."""
    try:
        centroids = _ensure_centroids(tei_url)
        vecs = _embed_batch_router([query], tei_url)
        query_vec = vecs[0]
        best_route = "rag_search"
        best_score = 0.0
        for route, centroid in centroids.items():
            sim = _cosine_similarity(query_vec, centroid)
            if sim > best_score:
                best_score = sim
                best_route = route
        if best_score < threshold:
            return ("rag_search", best_score)
        return (best_route, best_score)
    except Exception as e:
        log.warning(f"Router classification failed: {e}")
        return ("rag_search", 0.0)


# ── Navigation handlers (v4.3.0) ─────────────────────────────────────────────
_COORD_RE = re.compile(r'^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$')
_FROM_TO_RE = re.compile(r'from\s+(.+?)\s+to\s+(.+?)(?:\s+by\s+\w+)?$', re.IGNORECASE)
_TO_RE = re.compile(r'(?:to|towards?)\s+(?:the\s+)?(.+?)$', re.IGNORECASE)
_COORD_IN_TEXT_RE = re.compile(r'(-?\d+\.?\d+)\s*,\s*(-?\d+\.?\d+)')
_MODE_MAP = {
    "walk": "pedestrian", "walking": "pedestrian", "foot": "pedestrian", "pedestrian": "pedestrian",
    "bike": "bicycle", "cycling": "bicycle", "bicycle": "bicycle", "cycle": "bicycle",
    "truck": "truck", "lorry": "truck",
    "drive": "auto", "driving": "auto", "car": "auto", "auto": "auto",
}


def _detect_mode(query: str) -> str:
    q = query.lower()
    for keyword, mode in _MODE_MAP.items():
        if keyword in q:
            return mode
    return "auto"


def _clean_place(text: str) -> str:
    """Clean a place string for geocoding: strip articles, punctuation, normalize 'in' to comma."""
    s = text.strip().rstrip('?.,!')
    # Strip leading articles
    s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.IGNORECASE)
    # "214 North St in Filer ID" → "214 North St, Filer, ID"
    s = re.sub(r'\s+in\s+', ', ', s, count=1, flags=re.IGNORECASE)
    return s.strip()


def _parse_nav_query(query: str) -> tuple[str, str, str] | None:
    mode = _detect_mode(query)
    m = _FROM_TO_RE.search(query)
    if m:
        return (_clean_place(m.group(1)), _clean_place(m.group(2)), mode)
    m = _TO_RE.search(query)
    if m:
        dest = _clean_place(m.group(1))
        if dest:
            return (None, dest, mode)
    return None


def _geocode(query: str, photon_url: str, address_book_url: str = "") -> tuple[float, float, str] | tuple[None, None, None]:
    m = _COORD_RE.match(query.strip())
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return lat, lon, query
    # Address book lookup (before Photon)
    ab = _address_book_lookup(query, address_book_url)
    if ab:
        return ab['lat'], ab['lon'], ab.get('address') or ab['name']
    resp = requests.get(
        f"{photon_url}/api",
        params={"q": query, "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return None, None, None
    props = features[0]["properties"]
    coords = features[0]["geometry"]["coordinates"]
    parts = [props.get("name", "")]
    for key in ("city", "state", "country"):
        v = props.get(key)
        if v and v != parts[-1]:
            parts.append(v)
    return coords[1], coords[0], ", ".join(p for p in parts if p)


def _route_valhalla(
    orig: tuple[float, float],
    dest: tuple[float, float],
    mode: str,
    valhalla_url: str,
) -> str | None:
    try:
        resp = requests.post(
            f"{valhalla_url}/route",
            json={
                "locations": [
                    {"lat": orig[0], "lon": orig[1]},
                    {"lat": dest[0], "lon": dest[1]},
                ],
                "costing": mode,
                "directions_options": {"units": "miles"},
            },
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    trip = resp.json()["trip"]
    summary = trip["summary"]
    legs = trip["legs"][0]["maneuvers"]
    miles = round(summary["length"], 1)
    minutes = round(summary["time"] / 60, 1)
    lines = [f"Distance: {miles} miles | Time: {minutes} minutes", ""]
    for i, m in enumerate(legs, 1):
        inst = m["instruction"]
        dist = m.get("length", 0)
        if dist > 0:
            lines.append(f"{i}. {inst} — {round(dist, 1)} mi")
        else:
            lines.append(f"{i}. {inst}")
    return "\n".join(lines)


def _handle_nav_route(
    query: str,
    photon_url: str,
    valhalla_url: str,
    default_origin: str,
    address_book_url: str = "",
) -> str | None:
    parsed = _parse_nav_query(query)
    if not parsed:
        return None
    origin_str, dest_str, mode = parsed
    if not origin_str:
        origin_str = default_origin
    orig_lat, orig_lon, orig_name = _geocode(origin_str, photon_url, address_book_url)
    if orig_lat is None:
        return None
    dest_lat, dest_lon, dest_name = _geocode(dest_str, photon_url, address_book_url)
    if dest_lat is None:
        return None
    directions = _route_valhalla(
        (orig_lat, orig_lon), (dest_lat, dest_lon), mode, valhalla_url
    )
    if not directions:
        return None
    return f"Directions from {orig_name} to {dest_name} ({mode}):\n{directions}"


def _handle_reverse_geocode(query: str, photon_url: str) -> str | None:
    m = _COORD_IN_TEXT_RE.search(query)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    try:
        resp = requests.get(
            f"{photon_url}/reverse",
            params={"lat": lat, "lon": lon, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return f"No location found near coordinates ({lat}, {lon})"
        props = features[0]["properties"]
        parts = []
        for key in ("name", "city", "state", "country"):
            v = props.get(key)
            if v and v not in parts:
                parts.append(v)
        display = ", ".join(parts) if parts else "Unknown location"
        return f"Location: {display} ({lat}, {lon})"
    except Exception:
        return None


def _inject_nav_context(body: dict, context: str):
    messages = body.get("messages", [])
    nav_block = (
        "\n\n---NAVIGATION RESULT---\n\n"
        f"{context}\n\n"
        "---END NAVIGATION RESULT---\n\n"
        "Present these directions to the user exactly as provided. "
        "Do not summarize or omit steps. You may add brief contextual notes."
    )
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    if system_msg:
        system_msg["content"] = system_msg["content"] + nav_block
    else:
        body["messages"].insert(0, {"role": "system", "content": nav_block})



def _address_book_lookup(query: str, address_book_url: str) -> dict | None:
    """Check RECON address book for exact place match. Returns dict with lat/lon or None."""
    if not address_book_url:
        return None
    try:
        resp = requests.get(
            f"{address_book_url}/api/address_book/lookup",
            params={"q": query},
            timeout=2,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("confidence") == "exact" and data.get("lat") and data.get("lon"):
                log.info(f"Address book hit: {query!r} → {data['name']} ({data['lat']}, {data['lon']})")
                return data
        return None
    except Exception:
        return None


# ── End router/nav code ──────────────────────────────────────────────────────

# ── Kiwix Search Helpers (v5.0.0) ────────────────────────────────────────────

class _KiwixResultParser(HTMLParser):
    """Parse Kiwix search results HTML to extract articles."""
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_results = False
        self._in_li = False
        self._in_cite = False
        self._in_info = False
        self._current = {}
        self._capture_text = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "div" and "results" in attrs_dict.get("class", ""):
            self._in_results = True
        elif self._in_results and tag == "li":
            self._in_li = True
            self._current = {"title": "", "url": "", "snippet": "", "word_count": ""}
        elif self._in_li and tag == "a" and not self._current.get("url"):
            self._current["url"] = attrs_dict.get("href", "")
            self._capture_text = True
        elif self._in_li and tag == "cite":
            self._in_cite = True
            self._capture_text = True
        elif self._in_li and tag == "div" and "informations" in attrs_dict.get("class", ""):
            self._in_info = True
            self._capture_text = True

    def handle_endtag(self, tag):
        if tag == "div" and self._in_results and not self._in_li:
            self._in_results = False
        elif tag == "li" and self._in_li:
            if self._current.get("url"):
                self.results.append(self._current)
            self._current = {}
            self._in_li = False
        elif tag == "a" and self._capture_text and not self._in_cite:
            self._capture_text = False
        elif tag == "cite":
            self._in_cite = False
            self._capture_text = False
        elif tag == "div" and self._in_info:
            self._in_info = False
            self._capture_text = False

    def handle_data(self, data):
        if self._capture_text and self._in_li:
            text = data.strip()
            if self._in_cite:
                self._current["snippet"] += text + " "
            elif self._in_info:
                self._current["word_count"] = text
            elif not self._current.get("title"):
                self._current["title"] = text


def _strip_html_tags(html_content: str) -> str:
    """Simple HTML to plain text conversion using stdlib."""
    # Remove script and style elements
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = html.unescape(text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _fetch_kiwix_books() -> list[str]:
    """Fetch list of available books from kiwix-serve catalog."""
    try:
        resp = requests.get(
            f"{KIWIX_BASE_URL}/catalog/v2/entries",
            timeout=KIWIX_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        # Extract book names from href attributes
        books = re.findall(r'href="/content/([^"]+)"', resp.text)
        return list(set(books))  # dedupe
    except Exception as e:
        log.warning(f"Failed to fetch Kiwix book list: {e}")
        return []


def _search_kiwix_book(book: str, query: str, limit: int = 5) -> list[dict]:
    """Search a single Kiwix book and return results."""
    try:
        resp = requests.get(
            f"{KIWIX_BASE_URL}/search",
            params={"content": book, "pattern": query, "limit": limit},
            timeout=KIWIX_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        parser = _KiwixResultParser()
        parser.feed(resp.text)

        # Add book name to results
        for r in parser.results:
            r["book"] = book

        return parser.results
    except Exception as e:
        log.warning(f"Kiwix search failed for {book}: {e}")
        return []


def _fetch_kiwix_article(url_path: str) -> str:
    """Fetch and extract text content from a Kiwix article."""
    try:
        resp = requests.get(
            f"{KIWIX_BASE_URL}{url_path}",
            timeout=KIWIX_ARTICLE_TIMEOUT,
        )
        resp.raise_for_status()

        # Extract main content - try to find article body
        content = resp.text

        # Try to extract just the main content area
        main_match = re.search(r'<main[^>]*>(.*?)</main>', content, re.DOTALL | re.IGNORECASE)
        if main_match:
            content = main_match.group(1)
        else:
            # Try article tag
            article_match = re.search(r'<article[^>]*>(.*?)</article>', content, re.DOTALL | re.IGNORECASE)
            if article_match:
                content = article_match.group(1)
            else:
                # Try body content div
                body_match = re.search(r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>', content, re.DOTALL | re.IGNORECASE)
                if body_match:
                    content = body_match.group(1)

        return _strip_html_tags(content)[:4000]  # Limit to 4000 chars
    except Exception as e:
        log.warning(f"Failed to fetch Kiwix article {url_path}: {e}")
        return ""


def _search_kiwix(query: str, books: list[str]) -> list[dict]:
    """Search Kiwix across specified books and return merged results."""
    all_results = []

    # Prioritize English Wikipedia and other English content
    priority_books = []
    other_books = []
    for book in books:
        if "wikipedia_en" in book or "_en_" in book or "_eng_" in book:
            priority_books.append(book)
        elif not any(lang in book for lang in ["_af_", "_de_", "_fr_", "_es_"]):
            other_books.append(book)

    # Search priority books first
    for book in priority_books[:3]:  # Limit to top 3 priority books
        results = _search_kiwix_book(book, query, limit=5)
        all_results.extend(results)

    # If not enough results, try other books
    if len(all_results) < KIWIX_MAX_RESULTS:
        for book in other_books[:2]:
            results = _search_kiwix_book(book, query, limit=3)
            all_results.extend(results)

    return all_results[:KIWIX_MAX_RESULTS * 2]  # Return up to 6 for further filtering


# ── SearXNG Search Helpers (v5.0.0) ──────────────────────────────────────────

def _search_searxng(query: str) -> list[dict]:
    """Search SearXNG and return results. Returns empty list on failure."""
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            timeout=SEARXNG_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning(f"SearXNG returned status {resp.status_code}")
            return []

        data = resp.json()
        results = data.get("results", [])

        # Format results
        formatted = []
        for r in results[:SEARXNG_MAX_RESULTS]:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "engines": r.get("engines", []),
                "score": r.get("score", 0),
            })

        return formatted
    except requests.Timeout:
        log.warning("SearXNG request timed out (offline or slow)")
        return []
    except requests.ConnectionError:
        log.warning("SearXNG connection failed (offline)")
        return []
    except Exception as e:
        log.warning(f"SearXNG search failed: {e}")
        return []


# ── Cascade Logging (v5.0.0) ─────────────────────────────────────────────────

def _log_cascade_decision(
    query: str,
    router_intent: str,
    top_1_score: float,
    tier_used: int,
    num_results: int,
):
    """Log cascade decision to JSONL file for threshold tuning."""
    try:
        CASCADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "query": query,
            "router_intent": router_intent,
            "top_1_score": round(top_1_score, 4),
            "tier_used": tier_used,
            "num_results": num_results,
        }
        with open(CASCADE_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Failed to log cascade decision: {e}")


# ── End cascade helpers ──────────────────────────────────────────────────────

# Subdomains excluded from Medical results when tactical context detected
_OBSTETRIC_SUBDOMAINS = [
    "Obstetrics", "Midwifery", "Pregnancy", "Pregnancy Care",
    "High-Risk Pregnancy", "Childbirth", "Postpartum Care",
    "Family Planning", "Contraception", "Breastfeeding",
    "Labor Complications", "Twin Delivery",
]

# Query intent patterns — compiled once at import time
_PROCEDURAL_RE = re.compile(
    r"^(how\s+(do|can|should|would|to)\b|steps?\s+(to|for)\b|procedure\s+for\b|technique\s+for\b|way\s+to\b|method\s+(to|for)\b|guide\s+(to|for)\b|instructions?\s+for\b)",
    re.IGNORECASE,
)
_FOUNDATIONAL_RE = re.compile(
    r"^(what\s+(is|are|does|was|were)\b|explain\b|define\b|why\s+(does|do|is|are|did)\b|describe\b|meaning\s+of\b|difference\s+between\b)",
    re.IGNORECASE,
)

# Tactical keyword patterns for obstetric subdomain exclusion
_TACTICAL_RE = re.compile(
    r"\b(tactical|combat|tccc|casevac|medevac|casualty|triage|tourniquet|hemorrhage|wound packing|chest seal|care under fire|point of injury|far forward|buddy aid|self aid|field care|9-line|march algorithm)\b",
    re.IGNORECASE,
)


def _rerank_by_keyword_overlap(query: str, results: list) -> list:
    """Rerank results by boosting those with query term overlap in content/summary/key_facts.

    Adds a boost of up to 0.15 based on the fraction of query tokens found in the result text.
    Results are re-sorted by boosted score.
    """
    q_tokens = set(re.findall(r'[a-z0-9][-a-z0-9]{2,}', query.lower()))
    if not q_tokens:
        return results

    reranked = []
    for r in results:
        p = r.get("payload", {})
        score = r.get("score", 0)

        # Build searchable text from content, summary, and key_facts
        parts = []
        content = p.get("content", "")
        if content:
            parts.append(content[:2000].lower())
        summary = p.get("summary", "")
        if summary:
            parts.append(summary.lower())
        key_facts = p.get("key_facts", [])
        if isinstance(key_facts, list):
            parts.append(" ".join(str(f) for f in key_facts).lower())
        searchable = " ".join(parts)

        # Count how many query tokens appear in the result
        if searchable:
            matches = sum(1 for t in q_tokens if t in searchable)
            overlap_ratio = matches / len(q_tokens)
        else:
            overlap_ratio = 0

        # Boost: up to 0.15 for perfect overlap
        boosted_score = score + (overlap_ratio * 0.15)
        reranked.append({**r, "score": boosted_score})

    reranked.sort(key=lambda x: -x["score"])
    return reranked


class Filter:
    class Valves(BaseModel):
        tei_url: str = Field(
            default="http://100.64.0.14:8090/embed",
            description="TEI embedding endpoint",
        )
        qdrant_url: str = Field(
            default="http://100.64.0.14:6333",
            description="Qdrant REST API base URL",
        )
        collection: str = Field(
            default="recon_knowledge_hybrid",
            description="Qdrant collection name",
        )
        top_k: int = Field(
            default=8,
            description="Number of results to retrieve",
        )
        score_threshold: float = Field(
            default=0.3,
            description="Minimum similarity score to include a result",
        )
        fallback_min: int = Field(
            default=3,
            description="Minimum filtered results before falling back to unfiltered search",
        )
        candidate_limit: int = Field(
            default=50,
            description="Initial retrieval pool size for reranking",
        )
        rerank_top_n: int = Field(
            default=20,
            description="Keep top N after FlashRank reranking",
        )
        mmr_diversity: float = Field(
            default=0.3,
            description="MMR diversity 0-1 (0=pure relevance, 1=max diversity)",
        )
        enabled: bool = Field(
            default=True,
            description="Enable/disable RECON RAG augmentation",
        )
        priority: int = Field(
            default=0,
            description="Filter execution priority (lower = earlier)",
        )
        router_enabled: bool = Field(
            default=True,
            description="Enable semantic query routing",
        )
        router_threshold: float = Field(
            default=0.45,
            description="Min confidence for route classification",
        )
        photon_url: str = Field(
            default="http://100.64.0.24:2322",
            description="Photon geocoder URL",
        )
        valhalla_url: str = Field(
            default="http://100.64.0.24:8002",
            description="Valhalla routing URL",
        )
        address_book_url: str = Field(
            default="http://100.64.0.24:8420",
            description="RECON address book API base URL",
        )
        cascade_enabled: bool = Field(
            default=True,
            description="Enable three-tier cascade (Qdrant → Kiwix → SearXNG)",
        )
        cascade_threshold: float = Field(
            default=0.5,
            description="FlashRank score threshold for cascade fallthrough",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._expansion_cache: dict[str, list[str]] = {}
        self._ranker = None
        self._kiwix_books: list[str] | None = None

    def _get_kiwix_books(self) -> list[str]:
        """Get cached list of Kiwix books, fetching on first use."""
        if self._kiwix_books is None:
            self._kiwix_books = _fetch_kiwix_books()
            log.info(f"Loaded {len(self._kiwix_books)} Kiwix books")
        return self._kiwix_books

    def _embed_query(self, text: str) -> list:
        """Embed a query string using TEI."""
        resp = requests.post(
            self.valves.tei_url,
            json={"inputs": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()[0]

    def _get_ranker(self):
        """Lazy-load FlashRank neural reranker."""
        if self._ranker is None:
            from flashrank import Ranker
            self._ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank")
        return self._ranker

    def _rerank_flashrank(self, query: str, results: list) -> list:
        """Rerank results using FlashRank neural reranker.

        Takes Qdrant REST API result dicts (with 'payload' and 'score' keys).
        Returns reranked list with updated scores, trimmed to rerank_top_n.
        """
        from flashrank import RerankRequest

        ranker = self._get_ranker()

        passages = []
        for i, r in enumerate(results):
            p = r.get("payload", {})
            text = p.get("content", "")
            if not text:
                text = p.get("summary", "")
            passages.append({"id": i, "text": text[:2048]})

        if not passages:
            return results

        request = RerankRequest(query=query, passages=passages)
        ranked = ranker.rerank(request)

        reranked = []
        for item in ranked[:self.valves.rerank_top_n]:
            idx = item["id"]
            result_copy = dict(results[idx])
            result_copy["score"] = float(item["score"])
            reranked.append(result_copy)

        return reranked

    def _mmr_select(self, candidates: list, final_k: int) -> list:
        """Select final_k results using Maximal Marginal Relevance.

        Penalizes redundancy: same book_title (0.6), same domain (0.3), same source_type (0.1).
        Works with Qdrant REST API result dicts.
        """
        if len(candidates) <= final_k:
            return candidates

        selected = [candidates[0]]
        remaining = list(candidates[1:])

        while len(selected) < final_k and remaining:
            best_score = -999
            best_idx = 0

            for i, candidate in enumerate(remaining):
                relevance = candidate.get("score", 0)
                cp = candidate.get("payload", {})

                max_overlap = 0
                for sel in selected:
                    sp = sel.get("payload", {})
                    overlap = 0

                    c_title = cp.get("book_title", "")
                    s_title = sp.get("book_title", "")
                    if c_title and s_title and c_title == s_title:
                        overlap += 0.6

                    c_domain = cp.get("domain", "")
                    s_domain = sp.get("domain", "")
                    if c_domain and s_domain and c_domain == s_domain:
                        overlap += 0.3

                    c_src = cp.get("source_type", "")
                    s_src = sp.get("source_type", "")
                    if c_src and s_src and c_src == s_src:
                        overlap += 0.1

                    max_overlap = max(max_overlap, overlap)

                diversity = self.valves.mmr_diversity
                mmr_score = (1 - diversity) * relevance - diversity * max_overlap

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i

            selected.append(remaining.pop(best_idx))

        return selected

    @staticmethod
    def _detect_intent(query: str) -> Optional[list]:
        """Detect query intent and return preferred knowledge_types, or None for unfiltered."""
        q = query.strip()
        if _PROCEDURAL_RE.search(q):
            return ["procedural", "operational"]
        if _FOUNDATIONAL_RE.search(q):
            return ["foundational"]
        return None

    def _search_qdrant(
        self,
        vector: list,
        limit: int,
        knowledge_types: Optional[list] = None,
        domain: Optional[str] = None,
        exclude_subdomains: Optional[list] = None,
    ) -> list:
        """Search Qdrant for similar vectors, optionally filtered by knowledge_type and/or domain."""
        url = f"{self.valves.qdrant_url}/collections/{self.valves.collection}/points/search"
        payload = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "score_threshold": self.valves.score_threshold,
        }

        must_clauses = []
        must_not_clauses = []
        should_clauses = []

        if domain:
            must_clauses.append({"key": "domain", "match": {"value": domain}})

        if knowledge_types:
            for kt in knowledge_types:
                should_clauses.append({"key": "knowledge_type", "match": {"value": kt}})

        if exclude_subdomains:
            for sd in exclude_subdomains:
                must_not_clauses.append({"key": "subdomain", "match": {"value": sd}})

        if must_clauses or should_clauses or must_not_clauses:
            filter_obj = {}
            if must_clauses:
                filter_obj["must"] = must_clauses
            if should_clauses:
                filter_obj["should"] = should_clauses
            if must_not_clauses:
                filter_obj["must_not"] = must_not_clauses
            payload["filter"] = filter_obj

        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("result", [])

    def _boost_transcripts(self, results: list, factor: float = 1.10) -> list:
        """Boost transcript source scores to surface video content alongside documents."""
        for r in results:
            p = r.get("payload", {})
            if p.get("source_type") == "transcript":
                r["score"] = r.get("score", 0) * factor
        return results

    def _fetch_guaranteed_transcripts(self, vector: list, domain: str = "Medical", limit: int = 3, exclude_subdomains: Optional[list] = None) -> list:
        """Fetch top transcript results for a domain regardless of score threshold."""
        url = f"{self.valves.qdrant_url}/collections/{self.valves.collection}/points/search"
        filter_obj = {
            "must": [
                {"key": "source_type", "match": {"value": "transcript"}},
                {"key": "domain", "match": {"value": domain}},
            ],
        }
        if exclude_subdomains:
            filter_obj["must_not"] = [
                {"key": "subdomain", "match": {"value": sd}} for sd in exclude_subdomains
            ]
        payload = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "filter": filter_obj,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as e:
            log.warning(f"Guaranteed transcript fetch failed: {e}")
            return []

    def _expand_query_ollama(self, query: str) -> list[str]:
        """Generate alternative search terms via Ollama. Cached, 10s timeout, fail-safe."""
        if query in self._expansion_cache:
            return self._expansion_cache[query]
        try:
            resp = requests.post(
                "http://100.64.0.14:11434/api/generate",
                json={
                    "model": "goekdenizguelmez/JOSIEFIED-Qwen3:8b",
                    "prompt": (
                        f'Given this search query for a military/survival/preparedness knowledge base: "{query}"\n'
                        "Generate 3 specific technical search terms that would find TCCC, tactical medicine, "
                        "or field craft content. Focus on specific procedures, equipment, or doctrine terms "
                        "— not generic descriptions. Return only the terms, one per line, no numbering, no explanations."
                    ),
                    "stream": False,
                },
                timeout=10,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
            terms = [
                t for t in (
                    line.strip().lstrip("0123456789.-)*# ")
                    for line in text.strip().split("\n")
                    if line.strip()
                )
                if t and len(t) >= 3
            ][:3]
            self._expansion_cache[query] = terms
            log.info(f"Query expansion: {query!r} → {terms}")
            return terms
        except Exception as e:
            log.warning(f"Query expansion failed (proceeding without): {e}")
            self._expansion_cache[query] = []
            return []

    def _search_expanded_terms(
        self,
        terms: list[str],
        intent_types: Optional[list],
        limit: int,
        exclude_subdomains: Optional[list] = None,
    ) -> list:
        """Embed and search expanded query terms in parallel."""
        if not terms:
            return []

        def embed_and_search(term: str) -> list:
            vec = self._embed_query(term)
            return self._search_qdrant(vec, limit, knowledge_types=intent_types, exclude_subdomains=exclude_subdomains)

        results = []
        with ThreadPoolExecutor(max_workers=min(len(terms), 3)) as pool:
            futures = {pool.submit(embed_and_search, t): t for t in terms}
            for future in as_completed(futures):
                term = futures[future]
                try:
                    results.extend(future.result())
                except Exception as e:
                    log.warning(f"Expanded search for {term!r} failed: {e}")
        return results

    def _format_context(self, results: list, tier_tag: str = "DOMAIN_KNOWLEDGE") -> str:
        """Format search results into a context block for the system prompt."""
        if not results:
            return ""

        blocks = []
        for i, r in enumerate(results, 1):
            p = r.get("payload", {})
            score = r.get("score", 0)

            # Build citation line
            book = p.get("book_title") or p.get("filename", "Unknown")
            page = p.get("page_ref", "")
            if page:
                page_str = str(page)
                if not page_str.startswith("p"):
                    page_str = f"p. {page_str}"
                citation = f"{book}, {page_str}"
            else:
                citation = book

            # Summary or truncated content
            summary = p.get("summary", "")
            if not summary:
                content = p.get("content", "")
                summary = content[:500] + "..." if len(content) > 500 else content

            # Key facts
            key_facts = p.get("key_facts", [])
            facts_str = ""
            if key_facts and isinstance(key_facts, list):
                facts_str = "\nKey facts: " + "; ".join(str(f) for f in key_facts[:5])

            # Domain
            domains = p.get("domain", [])
            subdomains = p.get("subdomain", [])
            domain_str = ""
            if domains:
                d = ", ".join(domains) if isinstance(domains, list) else str(domains)
                if subdomains:
                    s = ", ".join(subdomains) if isinstance(subdomains, list) else str(subdomains)
                    domain_str = f"\nDomain: {d} > {s}"
                else:
                    domain_str = f"\nDomain: {d}"

            # Download URL
            dl = p.get("download_url", "")
            source_type = p.get("source_type", "document")
            if dl:
                if source_type == "transcript":
                    dl_str = f"\nSource Video: {dl}"
                elif source_type == "web":
                    dl_str = f"\nSource URL: {dl}"
                else:
                    dl_str = f"\nSource PDF: {dl}"
            else:
                dl_str = ""

            block = f"[{tier_tag}:{i}] {citation} (relevance: {score:.2f})\n{summary}{facts_str}{domain_str}{dl_str}"
            blocks.append(block)

        return "\n\n".join(blocks)

    def _format_kiwix_context(self, results: list[dict]) -> str:
        """Format Kiwix search results into a context block."""
        if not results:
            return ""

        blocks = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "Unknown")
            snippet = r.get("snippet", "").strip()
            book = r.get("book", "")
            url_path = r.get("url", "")

            # Build wiki URL
            if url_path:
                # Extract article path from /content/book/path
                path_match = re.search(r'/content/[^/]+/(.+)$', url_path)
                if path_match:
                    article_path = path_match.group(1)
                    wiki_url = f"https://wiki.echo6.co/viewer#{book}/{article_path}"
                else:
                    wiki_url = f"https://wiki.echo6.co/viewer#{book}"
            else:
                wiki_url = ""

            # Fetch article content if available
            content = ""
            if url_path:
                content = _fetch_kiwix_article(url_path)
                if content:
                    content = content[:1500]  # Limit per article

            if not content:
                content = snippet

            block = f"[OFFLINE_WIKI:{i}] {title}\n{content}"
            if wiki_url:
                block += f"\nSource: {wiki_url}"
            blocks.append(block)

        return "\n\n".join(blocks)

    def _format_searxng_context(self, results: list[dict]) -> str:
        """Format SearXNG search results into a context block."""
        if not results:
            return ""

        blocks = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "Unknown")
            snippet = r.get("snippet", "")
            url = r.get("url", "")
            engines = r.get("engines", [])

            engine_str = f" (via {', '.join(engines[:2])})" if engines else ""

            block = f"[WEB_SEARCH:{i}] {title}{engine_str}\n{snippet}"
            if url:
                block += f"\nSource: {url}"
            blocks.append(block)

        return "\n\n".join(blocks)

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
    ) -> dict:
        if not self.valves.enabled:
            return body

        # Get the latest user message
        messages = body.get("messages", [])
        user_messages = [m for m in messages if m.get("role") == "user"]
        if not user_messages:
            return body

        query = user_messages[-1].get("content", "")
        if not query or len(query.strip()) < 3:
            return body

        router_intent = "rag_search"

        # ── ROUTER GATE (v4.3.0) ─────────────────────────────────────────
        if self.valves.router_enabled:
            route, confidence = _classify_query(
                query, self.valves.tei_url, self.valves.router_threshold
            )
            router_intent = route
            log.info(f"Router: {query!r} → {route} ({confidence:.3f})")

            if route == "direct_answer":
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "Direct response", "done": True}}
                    )
                return body

            if route == "nav_route":
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "Getting directions...", "done": False}}
                    )
                result = _handle_nav_route(
                    query,
                    self.valves.photon_url,
                    self.valves.valhalla_url,
                    "Buhl, Idaho",
                    self.valves.address_book_url,
                )
                if result:
                    _inject_nav_context(body, result)
                    if __event_emitter__:
                        await __event_emitter__(
                            {"type": "status", "data": {"description": "Directions ready", "done": True}}
                        )
                    return body
                # Fall through to RAG if nav handling fails

            if route == "nav_reverse_geocode":
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "Looking up location...", "done": False}}
                    )
                result = _handle_reverse_geocode(query, self.valves.photon_url)
                if result:
                    _inject_nav_context(body, result)
                    if __event_emitter__:
                        await __event_emitter__(
                            {"type": "status", "data": {"description": "Location found", "done": True}}
                        )
                    return body
                # Fall through to RAG if reverse geocode fails

            # route == "rag_search" or nav fallthrough → continue existing pipeline

        # ── EXISTING RAG PIPELINE ─────────────────────────────────────────
        # Emit status
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "Searching RECON knowledge base...",
                        "done": False,
                    },
                }
            )

        tier_used = 1
        top_1_score = 0.0
        final_context = ""
        final_results = []

        try:
            vector = self._embed_query(query)

            # Detect intent (knowledge_type filter)
            intent_types = self._detect_intent(query)

            # Exclude obstetric/midwifery content when tactical context detected
            exclude_subs = _OBSTETRIC_SUBDOMAINS if _TACTICAL_RE.search(query) else None

            # Start query expansion in background (runs concurrently with main search)
            expansion_executor = ThreadPoolExecutor(max_workers=1)
            expansion_future = expansion_executor.submit(self._expand_query_ollama, query)

            # Search Qdrant — unfiltered semantic search, optionally narrowed by knowledge_type
            pool_size = self.valves.candidate_limit
            if intent_types:
                results = self._search_qdrant(vector, pool_size, knowledge_types=intent_types,
                                              exclude_subdomains=exclude_subs)
                if len(results) < self.valves.fallback_min:
                    results = self._search_qdrant(vector, pool_size, exclude_subdomains=exclude_subs)
            else:
                results = self._search_qdrant(vector, pool_size, exclude_subdomains=exclude_subs)

            # Collect expansion results and merge with main search
            try:
                expanded_terms = expansion_future.result(timeout=12)
            except Exception:
                expanded_terms = []
            expansion_executor.shutdown(wait=False)

            if expanded_terms:
                expanded_results = self._search_expanded_terms(
                    expanded_terms, intent_types, pool_size,
                    exclude_subdomains=exclude_subs,
                )
                if expanded_results:
                    combined = list(results) + expanded_results
                    seen: dict[str, dict] = {}
                    for r in combined:
                        pid = str(r.get("id", ""))
                        if pid not in seen or (r.get("score") or 0) > (seen[pid].get("score") or 0):
                            seen[pid] = r
                    results = sorted(seen.values(), key=lambda x: -(x.get("score") or 0))

            # Guaranteed transcript inclusion for medical queries
            if _TACTICAL_RE.search(query) or any(
                kw in query.lower() for kw in ("medical", "medicine", "wound", "trauma", "tourniquet",
                                                "hemorrhage", "bleeding", "fracture", "burn", "cpr",
                                                "first aid", "triage", "casualty")
            ):
                transcript_results = self._fetch_guaranteed_transcripts(vector, domain="Medical", limit=3, exclude_subdomains=exclude_subs)
                if transcript_results:
                    combined = list(results) + transcript_results
                    seen: dict[str, dict] = {}
                    for r in combined:
                        pid = str(r.get("id", ""))
                        if pid not in seen or (r.get("score") or 0) > (seen[pid].get("score") or 0):
                            seen[pid] = r
                    results = sorted(seen.values(), key=lambda x: -(x.get("score") or 0))

            # Boost transcript sources across all retrieval paths
            results = self._boost_transcripts(results)

            # Neural reranking via FlashRank, then MMR diversity selection
            try:
                results = self._rerank_flashrank(query, results)
                results = self._mmr_select(results, self.valves.top_k)
            except Exception as e:
                log.warning(f"FlashRank reranking failed, falling back to keyword overlap: {e}")
                results = _rerank_by_keyword_overlap(query, results)
                results = results[:self.valves.top_k]

            # Get top-1 score for cascade decision
            top_1_score = results[0]["score"] if results else 0.0

            # ── CASCADE DECISION POINT (v5.0.0) ──────────────────────────────
            if self.valves.cascade_enabled and top_1_score < self.valves.cascade_threshold:
                # Tier 1 score too low, try Tier 2 (Kiwix)
                log.info(f"Cascade: Tier 1 score {top_1_score:.3f} < {self.valves.cascade_threshold}, trying Kiwix")

                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "Searching offline encyclopedia...", "done": False}}
                    )

                kiwix_results = _search_kiwix(query, self._get_kiwix_books())

                if kiwix_results:
                    tier_used = 2
                    final_context = self._format_kiwix_context(kiwix_results[:KIWIX_MAX_RESULTS])
                    log.info(f"Cascade: Tier 2 (Kiwix) returned {len(kiwix_results)} results")
                else:
                    # Tier 2 failed, try Tier 3 (SearXNG)
                    log.info("Cascade: Tier 2 empty, trying SearXNG")

                    if __event_emitter__:
                        await __event_emitter__(
                            {"type": "status", "data": {"description": "Searching the web...", "done": False}}
                        )

                    searxng_results = _search_searxng(query)

                    if searxng_results:
                        tier_used = 3
                        final_context = self._format_searxng_context(searxng_results)
                        log.info(f"Cascade: Tier 3 (SearXNG) returned {len(searxng_results)} results")
                    else:
                        # All tiers exhausted, fall back to whatever Tier 1 had
                        log.info("Cascade: All tiers exhausted, using Tier 1 results")
                        tier_used = 1
                        final_context = self._format_context(results, "DOMAIN_KNOWLEDGE")
                        final_results = results
            else:
                # Tier 1 score good enough, use Qdrant results
                tier_used = 1
                final_context = self._format_context(results, "DOMAIN_KNOWLEDGE")
                final_results = results

            # Store results for outlet citations (only for Tier 1)
            if tier_used == 1:
                chat_id = body.get("chat_id", body.get("metadata", {}).get("chat_id", ""))
                if chat_id:
                    _SOURCE_STORE[chat_id] = final_results

            # Log cascade decision
            _log_cascade_decision(
                query=query,
                router_intent=router_intent,
                top_1_score=top_1_score,
                tier_used=tier_used,
                num_results=len(results) if tier_used == 1 else (len(kiwix_results) if tier_used == 2 else len(searxng_results) if tier_used == 3 else 0),
            )

            # Build the RAG prompt with tier-appropriate instructions
            if final_context:
                if tier_used == 1:
                    rag_prompt = (
                        "You have access to the RECON knowledge base — a curated library of military field manuals, "
                        "survival guides, preparedness literature, and video transcripts. Answer the user's question using "
                        "the reference material below. Reference sources using [DOMAIN_KNOWLEDGE:1], [DOMAIN_KNOWLEDGE:2], etc.\n\n"
                        "If the reference material doesn't adequately answer the question, say so explicitly rather "
                        "than filling gaps with general knowledge.\n\n"
                        "---REFERENCE MATERIAL---\n\n"
                        f"{final_context}\n\n"
                        "---END REFERENCE MATERIAL---"
                    )
                elif tier_used == 2:
                    rag_prompt = (
                        "The RECON domain knowledge base did not have high-confidence results for this query. "
                        "The following information comes from offline Wikipedia/encyclopedia sources (Kiwix). "
                        "Reference sources using [OFFLINE_WIKI:1], [OFFLINE_WIKI:2], etc.\n\n"
                        "Note: This is general encyclopedia content, not domain-specific preparedness material.\n\n"
                        "---OFFLINE WIKI CONTENT---\n\n"
                        f"{final_context}\n\n"
                        "---END OFFLINE WIKI CONTENT---"
                    )
                else:  # tier_used == 3
                    rag_prompt = (
                        "Neither the RECON knowledge base nor offline encyclopedias had relevant content. "
                        "The following information comes from a live web search. Reference sources using [WEB_SEARCH:1], etc.\n\n"
                        "Note: Web search results may be less reliable than curated sources. Verify important information.\n\n"
                        "---WEB SEARCH RESULTS---\n\n"
                        f"{final_context}\n\n"
                        "---END WEB SEARCH RESULTS---"
                    )
            else:
                rag_prompt = (
                    "You have access to the RECON knowledge base, but no relevant reference material was "
                    "found for this query in any tier (domain knowledge, offline wiki, or web search). "
                    "Answer from your general knowledge and clearly flag that your response is NOT backed by references."
                )

            # Add source priority instruction
            rag_prompt += (
                "\n\nSource priority: When sources overlap, prefer DOMAIN_KNOWLEDGE over OFFLINE_WIKI over WEB_SEARCH. "
                "Always cite which tier your information came from."
            )

            # Inject into system message
            system_msg = next(
                (m for m in messages if m.get("role") == "system"), None
            )
            if system_msg:
                system_msg["content"] = system_msg["content"] + "\n\n" + rag_prompt
            else:
                body["messages"].insert(
                    0, {"role": "system", "content": rag_prompt}
                )

            # Emit final status
            if __event_emitter__:
                tier_names = {1: "RECON", 2: "Kiwix", 3: "Web"}
                status_msg = f"Found results from {tier_names.get(tier_used, 'unknown')} (Tier {tier_used})"
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": status_msg,
                            "done": True,
                        },
                    }
                )

        except Exception as e:
            log.warning(f"RECON RAG search failed: {e}")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "RECON search unavailable, proceeding without references",
                            "done": True,
                        },
                    }
                )

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
    ) -> dict:
        if not self.valves.enabled or not __event_emitter__:
            return body

        # Retrieve sources from module-level store (survives instance recreation)
        chat_id = body.get("chat_id", "")
        sources = _SOURCE_STORE.pop(chat_id, [])
        if not sources:
            return body

        # Emit citations for each source used
        for r in sources:
            try:
                if not isinstance(r, dict):
                    continue
                p = r.get("payload") or {}
                if not isinstance(p, dict):
                    p = {}

                # Build citation — every field defensively None-checked
                book = p.get("book_title") or p.get("filename") or "Unknown Source"
                page = p.get("page_ref")
                if page is not None and str(page).strip():
                    page_str = str(page).strip()
                    if not page_str.startswith("p"):
                        page_str = f"p. {page_str}"
                    citation_name = f"{book}, {page_str}"
                else:
                    citation_name = str(book)

                download_url = str(p.get("download_url") or "")

                # Safe summary extraction — handle None/missing without raising
                summary = str(p.get("summary") or "")
                if not summary:
                    content = str(p.get("content") or "")
                    summary = content[:300] if content else ""

                # Safe score formatting
                score = r.get("score")
                try:
                    relevance = f"{float(score):.2f}"
                except (TypeError, ValueError):
                    relevance = "0.00"

                author = str(p.get("book_author") or "")

                await __event_emitter__(
                    {
                        "type": "source",
                        "data": {
                            "document": [summary],
                            "metadata": [
                                {
                                    "source": citation_name,
                                    "url": download_url,
                                    "author": author,
                                    "relevance": relevance,
                                }
                            ],
                            "source": {
                                "name": citation_name,
                                "url": download_url,
                            },
                        },
                    }
                )
            except Exception as e:
                pid = r.get("id", "?") if isinstance(r, dict) else "?"
                log.warning(f"Failed to emit citation (id={pid}): {e}")

        return body


# ── TEST BLOCK ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    # Test queries for each tier
    TEST_QUERIES = [
        ("tourniquet application steps", "Should hit Tier 1 (RECON)"),
        ("population of Ukraine", "Should hit Tier 2 (Kiwix)"),
        ("history of the Winter War between Finland and Russia", "Should hit Tier 2 (Kiwix)"),
        ("latest iPhone reviews 2026", "Should hit Tier 3 (SearXNG)"),
        ("compass declination adjustment", "Should hit Tier 1 (RECON)"),
        ("what is the Coriolis effect", "Could go either way"),
    ]

    async def run_tests():
        f = Filter()
        results = []

        print("=" * 70)
        print("CASCADE TEST RESULTS")
        print("=" * 70)

        for query, expected in TEST_QUERIES:
            print(f"\n{'─' * 70}")
            print(f"Query: {query}")
            print(f"Expected: {expected}")
            print("─" * 70)

            # Simulate a request body
            body = {
                "messages": [
                    {"role": "user", "content": query}
                ],
                "chat_id": f"test_{hash(query)}",
            }

            try:
                # Run through inlet
                result_body = await f.inlet(body)

                # Extract what was injected
                system_msg = next(
                    (m for m in result_body.get("messages", []) if m.get("role") == "system"),
                    None
                )

                if system_msg:
                    content = system_msg.get("content", "")

                    # Determine tier used
                    if "[DOMAIN_KNOWLEDGE:" in content:
                        tier = 1
                    elif "[OFFLINE_WIKI:" in content:
                        tier = 2
                    elif "[WEB_SEARCH:" in content:
                        tier = 3
                    else:
                        tier = 0

                    print(f"Tier Used: {tier}")

                    # Get first 200 chars of context
                    context_start = content.find("---")
                    if context_start > 0:
                        context_preview = content[context_start:context_start+300]
                        print(f"Context Preview: {context_preview[:200]}...")

                    results.append({
                        "query": query,
                        "expected": expected,
                        "tier": tier,
                    })
                else:
                    print("No system message injected")
                    results.append({
                        "query": query,
                        "expected": expected,
                        "tier": None,
                    })

            except Exception as e:
                print(f"ERROR: {e}")
                results.append({
                    "query": query,
                    "expected": expected,
                    "tier": None,
                    "error": str(e),
                })

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for r in results:
            tier_str = f"Tier {r['tier']}" if r.get('tier') else "ERROR"
            print(f"  {r['query'][:40]:<40} → {tier_str}")

        return results

    asyncio.run(run_tests())
