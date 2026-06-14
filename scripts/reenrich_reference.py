#!/usr/bin/env python3
"""
reenrich_reference.py — Re-classifies all remaining Reference-tagged concepts.

Scrolls Qdrant for vectors with domain == ["Reference"] or containing "Reference",
calls Gemini with a hardened prompt that rejects Reference as a valid response,
updates both Qdrant payload and concept JSON on disk.

Usage:
  python3 /opt/recon/scripts/reenrich_reference.py [--dry-run] [--workers 16] [--limit N]
"""

import json
import time
import random
import logging
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import google.generativeai as genai
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, MatchAny, Filter

import sys
sys.path.insert(0, '/opt/recon')
from lib.utils import get_config, setup_logging

LOG_FILE = Path("/opt/recon/logs/reenrich_reference.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("reenrich_reference")

CONCEPTS_DIR = Path("/opt/recon/data/concepts")

CANONICAL_DOMAINS = {
    "Defense & Tactics", "Sustainment Systems", "Off-Grid Systems",
    "Foundational Skills", "Communications", "Medical", "Food Systems",
    "Navigation", "Logistics", "Power Systems", "Leadership",
    "Scenario Playbooks", "Water Systems", "Security", "Community Coordination"
}

# Hardened prompt — Reference explicitly forbidden, classification rules detailed
CLASSIFY_PROMPT = """\
You are a knowledge classification engine. Classify this concept into its correct domain.

VALID DOMAINS — use ONLY these exact strings:
  Defense & Tactics
  Sustainment Systems
  Off-Grid Systems
  Foundational Skills
  Communications
  Medical
  Food Systems
  Navigation
  Logistics
  Power Systems
  Leadership
  Scenario Playbooks
  Water Systems
  Security
  Community Coordination

FORBIDDEN: Do NOT output "Reference" under any circumstances. It is not a valid domain.
FORBIDDEN: Do NOT output an empty domain list.

CLASSIFICATION RULES:
- First aid, anatomy, pharmacology, herbs, veterinary, austere medicine, wound care → Medical
- Food growing, foraging, hunting, fishing, animal husbandry, livestock → Sustainment Systems
- Food preservation, canning, fermentation, food storage, dehydrating → Food Systems
- Solar, wind, hydro, batteries, generators, inverters, charge controllers → Power Systems
- Water sourcing, filtration, purification, sanitation, wells, rainwater → Water Systems
- Radio, antennas, mesh networking, SIGINT, amateur radio → Communications
- Weapons, tactics, NBC, security operations, field craft → Defense & Tactics
- Permaculture, soil science, agroforestry, composting → Sustainment Systems
- Shelter, construction, masonry, blacksmithing, woodworking, crafts → Foundational Skills
- Navigation, land nav, celestial nav, map reading, compass → Navigation
- Emergency planning, disaster prep, scenario planning → Scenario Playbooks
- Leadership, governance, community organization → Leadership
- Supply chain, transportation, inventory → Logistics
- Physical security, perimeter, surveillance → Security
- Community building, cooperation, mutual aid → Community Coordination
- Biogas, wood gasification, rocket stoves, appropriate technology → Off-Grid Systems

If uncertain between two domains, pick the most actionable one for a self-reliant household.

Concept title: {title}
Concept subdomain tags: {subdomain}
Concept content: {content}

Return ONLY valid JSON, no markdown, no explanation:
{{"domain": ["Domain Name"]}}
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

def classify(title, subdomains, content, key, attempt=0):
    """Call Gemini. Rejects Reference. Falls back to subdomain heuristic if needed."""
    prompt = CLASSIFY_PROMPT.format(
        title=title or "(untitled)",
        subdomain=", ".join(subdomains[:10]) if subdomains else "(none)",
        content=str(content)[:400] if content else "(none)",
    )
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash-lite",
        generation_config={"response_mime_type": "application/json"}
    )
    for retry in range(4):
        try:
            resp = model.generate_content(prompt)
            data = json.loads(resp.text)
            domains = [
                d for d in data.get("domain", [])
                if d in CANONICAL_DOMAINS  # strips Reference automatically
            ]
            if domains:
                return domains
            # Gemini returned Reference or empty — try once more with stronger wording
            if retry == 0:
                continue
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503", "unavailable"]):
                time.sleep(min(5 * (2 ** retry) + random.uniform(0, 3), 60))
            else:
                break

    # Last resort: subdomain keyword heuristic
    return subdomain_fallback(subdomains)

SUBDOMAIN_FALLBACK_MAP = [
    (["first aid", "trauma", "wound", "anatomy", "pharmacol", "herbal", "medicin", "veterinar", "dental", "surgery"], "Medical"),
    (["foraging", "hunting", "fishing", "livestock", "permaculture", "soil", "agroforestry", "mycolog", "mushroom"], "Sustainment Systems"),
    (["canning", "preservation", "fermentation", "food storage", "dehydrat"], "Food Systems"),
    (["solar", "battery", "generator", "inverter", "wind turbine", "photovoltaic"], "Power Systems"),
    (["water purif", "filtration", "sanitation", "well", "rainwater"], "Water Systems"),
    (["radio", "antenna", "mesh", "sigint", "amateur radio", "meshtastic"], "Communications"),
    (["weapon", "firearm", "tactic", "nbc", "chemical warfare", "ballistic"], "Defense & Tactics"),
    (["navigation", "compass", "land nav", "celestial"], "Navigation"),
    (["blacksmith", "woodwork", "masonry", "construct", "craft", "pottery"], "Foundational Skills"),
    (["biogas", "gasif", "rocket stove", "appropriate tech"], "Off-Grid Systems"),
    (["disaster", "emergency prep", "evacuation", "scenario"], "Scenario Playbooks"),
    (["leadership", "governance", "community"], "Leadership"),
    (["logistics", "supply chain", "transport"], "Logistics"),
    (["security", "perimeter", "surveillance"], "Security"),
]

def subdomain_fallback(subdomains):
    combined = " ".join(s.lower() for s in subdomains)
    for keywords, domain in SUBDOMAIN_FALLBACK_MAP:
        if any(kw in combined for kw in keywords):
            return [domain]
    return ["Foundational Skills"]  # absolute last resort

def update_concept_json(doc_hash, title, new_domains):
    """Update domain in concept JSON files on disk."""
    doc_dir = CONCEPTS_DIR / doc_hash
    if not doc_dir.exists():
        return False
    for wf in doc_dir.glob("window_*.json"):
        try:
            with open(wf, "r", encoding="utf-8") as f:
                concepts = json.load(f)
            changed = False
            for c in concepts:
                if not isinstance(c, dict):
                    continue
                if c.get("title") == title:
                    raw = c.get("domain", [])
                    if isinstance(raw, str):
                        raw = [raw]
                    if "Reference" in raw or not [d for d in raw if d in CANONICAL_DOMAINS]:
                        c["domain"] = new_domains
                        changed = True
            if changed:
                with open(wf, "w", encoding="utf-8") as f:
                    json.dump(concepts, f, indent=2, ensure_ascii=False)
                return True
        except Exception:
            pass
    return False

def process_point(point, qdrant, collection, key_rotator, dry_run):
    payload = point.payload
    title = payload.get("title", "")
    subdomains = payload.get("subdomain", [])
    if isinstance(subdomains, str):
        subdomains = [subdomains]
    content = payload.get("content", payload.get("summary", ""))
    doc_hash = payload.get("doc_hash", "")

    key = key_rotator.next()
    new_domains = classify(title, subdomains, content, key)

    if dry_run:
        return "would_classify"

    # Update Qdrant payload
    qdrant.set_payload(
        collection_name=collection,
        payload={"domain": new_domains},
        points=[point.id],
    )

    # Update JSON on disk
    if doc_hash:
        update_concept_json(doc_hash, title, new_domains)

    return "ok"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None)
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

    log.info("Scrolling Qdrant for Reference-tagged concepts...")

    # Scroll all points containing Reference in domain
    offset = None
    reference_points = []
    while True:
        results, offset = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[FieldCondition(
                    key="domain",
                    match=MatchAny(any=["Reference"])
                )]
            ),
            limit=1000,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        reference_points.extend(results)
        if offset is None:
            break
        if args.limit and len(reference_points) >= args.limit:
            reference_points = reference_points[:args.limit]
            break

    total = len(reference_points)
    log.info(f"Found {total:,} Reference-tagged vectors")
    log.info(f"Workers: {args.workers} | Keys: {len(keys)} | Dry run: {args.dry_run}")
    log.info(f"Estimated Gemini Flash cost: ~${total * 0.0004:.2f}")

    if args.dry_run:
        log.info(f"DRY RUN: would re-classify {total:,} concepts. Exiting.")
        return

    results = defaultdict(int)
    lock = threading.Lock()
    done = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(process_point, p, qdrant, collection, rotator, False): p
            for p in reference_points
        }
        for future in as_completed(futures):
            status = future.result()
            with lock:
                results[status] += 1
                done += 1
                if done % 5000 == 0:
                    elapsed = time.time() - start
                    rate = done / elapsed * 60
                    eta = (total - done) / (done / elapsed) / 60
                    log.info(f"  {done:,}/{total:,} | {rate:.0f}/min | ETA {eta:.0f}min | {dict(results)}")
            time.sleep(0.02)

    elapsed = time.time() - start
    log.info(f"\nComplete in {elapsed/60:.1f}min:")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        log.info(f"  {status:<20} {count:>10,}")

if __name__ == "__main__":
    main()
