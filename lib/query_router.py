"""Semantic query router for Aurora.

Classifies user queries into routes (nav_route, nav_reverse_geocode,
direct_answer, rag_search) by comparing query embeddings against
pre-computed route centroids from example queries.

TEI endpoint: http://100.64.0.14:8090/embed (cortex via Tailscale)
"""

import math
import threading
import requests

# ── Route examples ────────────────────────────────────────────────────────────
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

# ── Module-level cache ────────────────────────────────────────────────────────
_ROUTE_CENTROIDS: dict | None = None
_LOCK = threading.Lock()


def _embed_batch(texts: list[str], tei_url: str) -> list[list[float]]:
    """Embed a batch of texts via TEI."""
    resp = requests.post(tei_url, json={"inputs": texts}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _compute_centroid(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of vectors."""
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
    """Cosine similarity between two vectors (pure Python)."""
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
    """Lazy-init: embed all examples in one batch, compute centroids, cache."""
    global _ROUTE_CENTROIDS
    if _ROUTE_CENTROIDS is not None:
        return _ROUTE_CENTROIDS

    with _LOCK:
        if _ROUTE_CENTROIDS is not None:
            return _ROUTE_CENTROIDS

        # Flatten all examples into one batch
        all_texts = []
        route_ranges: dict[str, tuple[int, int]] = {}
        offset = 0
        for route, examples in ROUTE_EXAMPLES.items():
            route_ranges[route] = (offset, offset + len(examples))
            all_texts.extend(examples)
            offset += len(examples)

        all_vectors = _embed_batch(all_texts, tei_url)

        centroids = {}
        for route, (start, end) in route_ranges.items():
            centroids[route] = _compute_centroid(all_vectors[start:end])

        _ROUTE_CENTROIDS = centroids
        return _ROUTE_CENTROIDS


def classify(
    query: str,
    tei_url: str = "http://100.64.0.14:8090/embed",
    threshold: float = 0.45,
) -> tuple[str, float]:
    """Classify a query into a route.

    Returns (route_name, confidence). If no route exceeds the threshold,
    returns ("rag_search", best_score) as the safe default.
    """
    centroids = _ensure_centroids(tei_url)

    # Embed the query
    vecs = _embed_batch([query], tei_url)
    query_vec = vecs[0]

    # Compare against all centroids
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
