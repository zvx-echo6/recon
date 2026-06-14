#!/usr/bin/env python3
"""
domain_reenrich.py — Re-enriches solo-Reference concepts that domain_remap.py
couldn't fix via subdomain lookup. Reads remap_unknowns.jsonl, calls Gemini
with a lightweight classification-only prompt, updates domain in-place.

Usage:
  python3 /opt/recon/scripts/domain_reenrich.py [--workers 16] [--limit N]

Reads:  /opt/recon/data/remap_unknowns.jsonl
Writes: domain field in-place in window JSON files
Log:    /opt/recon/logs/domain_reenrich.log
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

UNKNOWNS_FILE = Path("/opt/recon/data/remap_unknowns.jsonl")
LOG_FILE = Path("/opt/recon/logs/domain_reenrich.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("domain_reenrich")

CANONICAL_DOMAINS = [
    "Defense & Tactics", "Sustainment Systems", "Off-Grid Systems",
    "Foundational Skills", "Communications", "Medical", "Food Systems",
    "Navigation", "Logistics", "Power Systems", "Leadership",
    "Scenario Playbooks", "Water Systems", "Security", "Community Coordination"
]

DOMAIN_SET = set(CANONICAL_DOMAINS)

CLASSIFY_PROMPT = """\
Classify this knowledge concept into one or more domains.

VALID DOMAINS (use ONLY these exact strings, no others):
{domains}

Concept title: {title}
Concept tags: {subdomain}
Concept preview: {content}

Return ONLY valid JSON, no markdown, no explanation:
{{"domain": ["Domain Name"]}}

Rules:
- Use only the domain strings listed above, spelled exactly
- If genuinely multi-domain assign all that apply
- Never return empty domain list — pick the closest match
- Medical content, herbs, first aid, veterinary → Medical
- Food growing, foraging, hunting, livestock → Sustainment Systems
- Food preservation, canning, storage → Food Systems
- Solar, wind, batteries, generators → Power Systems
- Water sourcing, filtration, sanitation → Water Systems
"""

def load_gemini_keys():
    env = Path("/opt/recon/.env")
    keys = []
    for line in env.read_text().splitlines():
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
        domains="\n".join(f"  {d}" for d in CANONICAL_DOMAINS),
        title=title or "(untitled)",
        subdomain=", ".join(subdomains[:10]) if subdomains else "(none)",
        content=content[:300] if content else "(none)",
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
            domains = [d for d in data.get("domain", []) if d in DOMAIN_SET]
            if domains:
                return domains
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503", "unavailable"]):
                delay = min(5 * (2 ** attempt) + random.uniform(0, 3), 60)
                time.sleep(delay)
            else:
                break
    return ["Foundational Skills"]  # last-resort fallback

def process_unknown(item, key_rotator):
    filepath = Path(item["filepath"])
    title = item.get("title", "")
    subdomains = item.get("subdomain", [])
    content = item.get("content_preview", "")

    if not filepath.exists():
        return "file_missing"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            concepts = json.load(f)
    except Exception:
        return "read_error"

    if not isinstance(concepts, list):
        return "not_list"

    # Find this concept by title and update its domain
    matched = False
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        if concept.get("title", "") == title:
            raw = concept.get("domain", [])
            if isinstance(raw, str):
                raw = [raw]
            # Only re-enrich if still stuck on Reference
            if raw == ["Reference"] or raw == []:
                key = key_rotator.next()
                new_domains = classify_concept(title, subdomains, content, key)
                concept["domain"] = new_domains
                concept["_reenriched"] = True
                matched = True
                break

    if not matched:
        return "already_fixed"

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(concepts, f, indent=2, ensure_ascii=False)
    except Exception:
        return "write_error"

    return "ok"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    keys = load_gemini_keys()
    if not keys:
        log.error("No Gemini keys found in .env")
        return
    rotator = KeyRotator(keys)

    unknowns = []
    with open(UNKNOWNS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                unknowns.append(json.loads(line))

    if args.limit:
        unknowns = unknowns[:args.limit]

    total = len(unknowns)
    log.info(f"Re-enriching {total:,} concepts | {args.workers} workers | {len(keys)} API keys")
    log.info(f"Estimated Gemini Flash cost: ~${total * 0.0004:.2f} (conservative)")

    results = defaultdict(int)
    lock = threading.Lock()
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_unknown, item, rotator): item for item in unknowns}
        for future in as_completed(futures):
            status = future.result()
            with lock:
                results[status] += 1
                done += 1
                if done % 5000 == 0:
                    pct = done / total * 100
                    log.info(f"  Progress: {done:,}/{total:,} ({pct:.1f}%) | {dict(results)}")
            time.sleep(0.05)

    log.info("── Final Results ─────────────────────────────────────────────")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        log.info(f"  {status:<25} {count:>10,}")
    log.info(f"  Total: {total:,}")

if __name__ == "__main__":
    main()
