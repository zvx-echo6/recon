#!/usr/bin/env python3
"""
cleanup_outliers.py — Three-pass cleanup of RECON concept data.

Pass 1: Remap ~160 non-canonical domain strings in concept JSONs + Qdrant payloads
Pass 2: Re-enrich 434 concepts with empty domain arrays via Gemini
Pass 3: Purge junk/noise URLs from Qdrant + SQLite DB

Usage:
  python3 /opt/recon/scripts/cleanup_outliers.py [--dry-run] [--skip-pass N]
"""

import json
import time
import random
import logging
import argparse
import threading
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import google.generativeai as genai
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, MatchAny, Filter

import sys, os
sys.path.insert(0, '/opt/recon')
from lib.utils import get_config, setup_logging

LOG_FILE = Path("/opt/recon/logs/cleanup_outliers.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("cleanup_outliers")

CONCEPTS_DIR = Path("/opt/recon/data/concepts")
DB_PATH = Path("/opt/recon/data/recon.db")

CANONICAL_DOMAINS = {
    "Defense & Tactics", "Sustainment Systems", "Off-Grid Systems",
    "Foundational Skills", "Communications", "Medical", "Food Systems",
    "Navigation", "Logistics", "Power Systems", "Leadership",
    "Scenario Playbooks", "Water Systems", "Security", "Community Coordination"
}

# Non-canonical → canonical remap
OUTLIER_MAP = {
    "Zoology":                  "Sustainment Systems",
    "Botany":                   "Sustainment Systems",
    "Nature Lore":              "Sustainment Systems",
    "Ecology":                  "Sustainment Systems",
    "Navigational Astronomy":   "Navigation",
    "Troubleshooting":          "Foundational Skills",
    "Chemistry":                "Foundational Skills",
    "Metallurgy":               "Foundational Skills",
    "Weird Science":            "Foundational Skills",
    "Philosophy of physics":    "Foundational Skills",
    "Physics":                  "Foundational Skills",
    "Cell biology":             "Foundational Skills",
    "Economics":                "Leadership",
    "Business":                 "Leadership",
    "Safety":                   "Security",
    "Law Enforcement":          "Security",
    "Security & Intelligence":  "Security",
    "Fire Weather":             "Scenario Playbooks",
    "Legal":                    "Leadership",
    # Discard — replace with closest real domain
    "Site News":                "Foundational Skills",
    "Paleogeography":           "Foundational Skills",
    "Chemical Manipulation":    "Foundational Skills",
}

# Junk URL patterns — pages with no knowledge value
JUNK_URL_PATTERNS = [
    # rocketstoves.com nav/template garbage
    "rocketstoves.com/favicon",
    "rocketstoves.com/cropped-favicon",
    "rocketstoves.com/layouts/",
    "rocketstoves.com/sample",
    "rocketstoves.com/templates/",
    "rocketstoves.com/hello-world",
    "rocketstoves.com/blog-forthcoming",
    "rocketstoves.com/contact",
    "rocketstoves.com/acknowledgements",
    "rocketstoves.com/ja3",
    "rocketstoves.com/juxtapositions",
    "rocketstoves.com/no-name-soi",
    "rocketstoves.com/big4",
    "rocketstoves.com/roof",
    "rocketstoves.com/rmh_dloadcover",
    "rocketstoves.com/pedcover",
    "rocketstoves.com/laundry-to-landscape",
    "rocketstoves.com/barreloven",
    # NRCS calendar/event noise
    "nrcs.usda.gov/events/",
    "nrcs.usda.gov/state-offices/massachusetts",
    "nrcs.usda.gov/state-offices/nebraska",
    "nrcs.usda.gov/state-offices/oklahoma",
    "nrcs.usda.gov/state-offices/utah",
    "nrcs.usda.gov/conservation-basics/natural-resource-concerns/soil/western-call-for-abstracts",
    # deeranddeerhunting trophy hunt videos (no knowledge value)
    "deeranddeerhunting.com/trophy-whitetails-exclusive-videos/",
    # eattheweeds non-content pages
    "eattheweeds.com/media-interviews-with-green-deane",
    "eattheweeds.com/motorcycles-and-mushrooms",
    "eattheweeds.com/sunny-savage",
    # foragersharvest nav pages
    "foragersharvest.com/contact",
    "foragersharvest.com/podcasts",
    # motherearthnews classifieds/nav
    "motherearthnews.com/classifieds/",
    "motherearthnews.com/biographies/",
]

CLASSIFY_PROMPT = """\
Classify this knowledge concept into one or more domains.

VALID DOMAINS (use ONLY these exact strings):
  Defense & Tactics, Sustainment Systems, Off-Grid Systems, Foundational Skills,
  Communications, Medical, Food Systems, Navigation, Logistics, Power Systems,
  Leadership, Scenario Playbooks, Water Systems, Security, Community Coordination

Concept title: {title}
Concept tags: {subdomain}
Concept preview: {content}

Return ONLY valid JSON, no markdown:
{{"domain": ["Domain Name"]}}

Rules:
- Never return empty domain list
- Medical content, herbs, first aid, veterinary → Medical
- Food growing, foraging, hunting, livestock → Sustainment Systems
- Food preservation, canning, storage → Food Systems
- Solar, wind, batteries, generators → Power Systems
- Water sourcing, filtration, sanitation → Water Systems
"""

def load_gemini_keys():
    keys = []
    for line in Path("/opt/recon/.env").read_text().splitlines():
        if line.startswith("GEMINI_KEY_"):
            keys.append(line.split("=", 1)[1].strip())
    return keys

class KeyRotator:
    def __init__(self, keys):
        self.keys = keys
        self._i = 0
        self._lock = threading.Lock()
    def next(self):
        with self._lock:
            key = self.keys[self._i % len(self.keys)]
            self._i += 1
            return key

def classify_concept(title, subdomains, content, key):
    prompt = CLASSIFY_PROMPT.format(
        title=title or "(untitled)",
        subdomain=", ".join(subdomains[:10]) if subdomains else "(none)",
        content=str(content)[:300] if content else "(none)",
    )
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash-lite",
        generation_config={"response_mime_type": "application/json"}
    )
    for attempt in range(4):
        try:
            resp = model.generate_content(prompt)
            data = json.loads(resp.text)
            domains = [d for d in data.get("domain", []) if d in CANONICAL_DOMAINS]
            if domains:
                return domains
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503"]):
                time.sleep(min(5 * (2 ** attempt) + random.uniform(0, 3), 60))
            else:
                break
    return ["Foundational Skills"]

# ── PASS 1: Remap outlier domains ────────────────────────────────────────────

def remap_concept_domains(domains):
    """Remap any outlier domain names in a domain list."""
    result = set()
    changed = False
    for d in domains:
        if d in CANONICAL_DOMAINS:
            result.add(d)
        elif d in OUTLIER_MAP:
            result.add(OUTLIER_MAP[d])
            changed = True
        else:
            changed = True  # drop unknown
    return list(result), changed

def pass1_remap_outliers(qdrant, collection, dry_run):
    log.info("=== PASS 1: Remapping non-canonical outlier domains ===")
    outlier_names = list(OUTLIER_MAP.keys())
    stats = defaultdict(int)

    # Scroll through Qdrant finding affected vectors
    offset = None
    affected_points = []

    while True:
        results, offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[FieldCondition(
                    key="domain",
                    match=MatchAny(any=outlier_names)
                )]
            ),
            limit=500,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        affected_points.extend(results)
        if offset is None:
            break

    log.info(f"Found {len(affected_points)} Qdrant points with outlier domains")

    for point in affected_points:
        payload = point.payload
        old_domains = payload.get("domain", [])
        if isinstance(old_domains, str):
            old_domains = [old_domains]

        new_domains, changed = remap_concept_domains(old_domains)
        if not new_domains:
            new_domains = ["Foundational Skills"]

        if changed:
            stats["qdrant_updated"] += 1
            if not dry_run:
                qdrant.set_payload(
                    collection_name=collection,
                    payload={"domain": new_domains},
                    points=[point.id],
                )

    # Also fix concept JSON files on disk
    json_fixed = 0
    for window_file in CONCEPTS_DIR.rglob("window_*.json"):
        try:
            with open(window_file, "r", encoding="utf-8") as f:
                concepts = json.load(f)
        except Exception:
            continue

        if not isinstance(concepts, list):
            continue

        file_changed = False
        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            raw = concept.get("domain", [])
            if isinstance(raw, str):
                raw = [raw]
            new, changed = remap_concept_domains(raw)
            if changed:
                concept["domain"] = new if new else ["Foundational Skills"]
                file_changed = True

        if file_changed:
            json_fixed += 1
            if not dry_run:
                with open(window_file, "w", encoding="utf-8") as f:
                    json.dump(concepts, f, indent=2, ensure_ascii=False)

    log.info(f"Pass 1 complete: {stats['qdrant_updated']} Qdrant points updated, {json_fixed} JSON files updated")
    return stats

# ── PASS 2: Re-enrich empty domain concepts ──────────────────────────────────

def pass2_empty_domains(qdrant, collection, key_rotator, dry_run):
    log.info("=== PASS 2: Re-enriching empty domain concepts ===")
    stats = defaultdict(int)

    # Find empty domain points in Qdrant
    offset = None
    empty_points = []
    while True:
        results, offset = qdrant.scroll(
            collection_name=collection,
            limit=500,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for r in results:
            d = r.payload.get("domain", [])
            if not d or d == [] or d == [""]:
                empty_points.append(r)
        if offset is None:
            break

    log.info(f"Found {len(empty_points)} points with empty domains")

    for point in empty_points:
        payload = point.payload
        title = payload.get("title", "")
        subdomains = payload.get("subdomain", [])
        content = payload.get("content", payload.get("summary", ""))

        key = key_rotator.next()
        new_domains = classify_concept(title, subdomains, content, key)
        stats["classified"] += 1

        if not dry_run:
            qdrant.set_payload(
                collection_name=collection,
                payload={"domain": new_domains},
                points=[point.id],
            )

        # Also update the concept JSON on disk
        doc_hash = payload.get("doc_hash", "")
        if doc_hash:
            doc_concepts_dir = CONCEPTS_DIR / doc_hash
            if doc_concepts_dir.exists():
                for wf in doc_concepts_dir.glob("window_*.json"):
                    try:
                        with open(wf, "r", encoding="utf-8") as f:
                            concepts = json.load(f)
                        changed = False
                        for c in concepts:
                            if isinstance(c, dict) and c.get("title") == title:
                                d = c.get("domain", [])
                                if not d or d == []:
                                    c["domain"] = new_domains
                                    changed = True
                        if changed and not dry_run:
                            with open(wf, "w", encoding="utf-8") as f:
                                json.dump(concepts, f, indent=2, ensure_ascii=False)
                    except Exception:
                        pass

        time.sleep(0.05)

    log.info(f"Pass 2 complete: {stats['classified']} concepts re-classified")
    return stats

# ── PASS 3: Purge junk URLs ──────────────────────────────────────────────────

def is_junk_url(url):
    url_lower = url.lower()
    return any(pattern.lower() in url_lower for pattern in JUNK_URL_PATTERNS)

def pass3_purge_junk(qdrant, collection, dry_run):
    log.info("=== PASS 3: Purging junk URLs ===")
    stats = defaultdict(int)

    # Scroll all web-source points and find junk
    offset = None
    junk_point_ids = []
    junk_doc_hashes = set()

    while True:
        results, offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="source_type", match=MatchAny(any=["web"]))]
            ),
            limit=500,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for r in results:
            filename = r.payload.get("filename", "")
            doc_hash = r.payload.get("doc_hash", "")
            if is_junk_url(filename):
                junk_point_ids.append(r.id)
                if doc_hash:
                    junk_doc_hashes.add(doc_hash)
        if offset is None:
            break

    log.info(f"Found {len(junk_point_ids)} junk vectors across {len(junk_doc_hashes)} documents")

    if not dry_run and junk_point_ids:
        # Delete in batches
        batch_size = 500
        for i in range(0, len(junk_point_ids), batch_size):
            batch = junk_point_ids[i:i + batch_size]
            qdrant.delete(collection_name=collection, points_selector=batch)
        log.info(f"Deleted {len(junk_point_ids)} junk vectors from Qdrant")

        # Mark junk docs as skipped in SQLite
        conn = sqlite3.connect(str(DB_PATH))
        for doc_hash in junk_doc_hashes:
            conn.execute(
                "UPDATE documents SET status = 'skipped', error_message = 'junk content purged' WHERE hash = ?",
                (doc_hash,)
            )
        conn.commit()
        conn.close()
        log.info(f"Marked {len(junk_doc_hashes)} documents as skipped in DB")

    stats["junk_vectors"] = len(junk_point_ids)
    stats["junk_docs"] = len(junk_doc_hashes)
    log.info(f"Pass 3 complete: {stats['junk_vectors']} vectors, {stats['junk_docs']} docs purged")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-pass", type=int, action="append", default=[])
    args = parser.parse_args()

    config = get_config()
    keys = load_gemini_keys()
    rotator = KeyRotator(keys)

    qdrant = QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=60
    )
    collection = config['vector_db']['collection']

    log.info(f"Starting cleanup | dry_run={args.dry_run} | skipping passes: {args.skip_pass}")

    if 1 not in args.skip_pass:
        pass1_remap_outliers(qdrant, collection, args.dry_run)

    if 2 not in args.skip_pass:
        pass2_empty_domains(qdrant, collection, rotator, args.dry_run)

    if 3 not in args.skip_pass:
        pass3_purge_junk(qdrant, collection, args.dry_run)

    log.info("All passes complete.")


if __name__ == "__main__":
    main()
