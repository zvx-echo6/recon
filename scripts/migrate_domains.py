#!/usr/bin/env python3
"""
migrate_domains.py — Reclassify 5 legacy domains via Gemini Flash.

Targets: Sustainment Systems, Off-Grid Systems, Defense & Tactics,
         Community Coordination, Leadership

Maps each to one of the 18 approved domains. 16 parallel workers,
checkpoint file, crash-safe, incremental saves, progress every 5,000.

Usage:
  python3 /tmp/migrate_domains.py [--dry-run] [--workers 16] [--limit N]
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
from qdrant_client.models import FieldCondition, MatchValue, Filter

# Suppress noisy HTTP logs
import logging as _logging
_logging.getLogger("httpx").setLevel(_logging.WARNING)
_logging.getLogger("qdrant_client").setLevel(_logging.WARNING)

LOG_FILE = Path("/opt/recon/logs/migrate_domains.log")
CHECKPOINT_FILE = Path("/opt/recon/data/migrate_domains_checkpoint.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("migrate_domains")

# ── Constants ───────────────────────────────────────────────────────────────

VALID_DOMAINS = {
    'Agriculture & Livestock', 'Civil Organization', 'Communications',
    'Food Systems', 'Foundational Skills', 'Logistics', 'Medical',
    'Navigation', 'Operations', 'Power Systems', 'Preservation & Storage',
    'Security', 'Shelter & Construction', 'Technology', 'Tools & Equipment',
    'Vehicles', 'Water Systems', 'Wilderness Skills',
}

SOURCE_DOMAINS = {
    'Sustainment Systems', 'Off-Grid Systems', 'Defense & Tactics',
    'Community Coordination', 'Leadership',
}

DOMAIN_LIST_STR = ', '.join(sorted(VALID_DOMAINS))

CLASSIFY_PROMPT = """\
Classify this knowledge concept into exactly one domain from this list:
Agriculture & Livestock, Civil Organization, Communications, Food Systems, Foundational Skills, Logistics, Medical, Navigation, Operations, Power Systems, Preservation & Storage, Security, Shelter & Construction, Technology, Tools & Equipment, Vehicles, Water Systems, Wilderness Skills

Return ONLY the exact domain string, nothing else. No explanation, no punctuation, no quotes.

Content: {content}
Summary: {summary}
Subdomain: {subdomain}
"""

DOMAIN_FALLBACK = 'Foundational Skills'

# ── Key management ──────────────────────────────────────────────────────────

def load_gemini_keys():
    keys = []
    env_path = Path("/opt/recon/.env")
    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} not found")
    for line in env_path.read_text().splitlines():
        if line.startswith("GEMINI_KEY_"):
            keys.append(line.split("=", 1)[1].strip())
    if not keys:
        raise ValueError("No GEMINI_KEY_* found in .env")
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


# ── Classification ──────────────────────────────────────────────────────────

def classify_domain(content, summary, subdomains, key):
    """Call Gemini Flash to classify into one of 18 domains."""
    prompt = CLASSIFY_PROMPT.format(
        content=str(content)[:400] if content else "(none)",
        summary=str(summary)[:200] if summary else "(none)",
        subdomain=", ".join(subdomains[:10]) if subdomains else "(none)",
    )
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash-lite",
        generation_config={"response_mime_type": "text/plain"}
    )

    for retry in range(4):
        try:
            resp = model.generate_content(prompt)
            value = resp.text.strip().strip('"').strip("'").strip()
            if value in VALID_DOMAINS:
                return value
            # Try case-insensitive match
            for valid in VALID_DOMAINS:
                if value.lower() == valid.lower():
                    return valid
            # Partial match — Gemini sometimes returns with trailing period
            clean = value.rstrip('.')
            if clean in VALID_DOMAINS:
                return clean
            # Invalid — retry with stricter prompt
            if retry < 3:
                prompt = (
                    f"Your previous response '{value}' was invalid. "
                    f"You must return ONLY one of these exact strings: {DOMAIN_LIST_STR}\n\n"
                    f"Content: {str(content)[:300]}\n"
                    f"Return ONLY the exact domain string."
                )
                continue
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503", "unavailable"]):
                time.sleep(min(5 * (2 ** retry) + random.uniform(0, 3), 60))
            else:
                log.warning(f"Gemini error (attempt {retry+1}): {e}")
                if retry >= 2:
                    break

    return heuristic_fallback(content, summary, subdomains)


def heuristic_fallback(content, summary, subdomains):
    """Last-resort heuristic when Gemini fails or returns invalid."""
    text = f"{summary or ''} {' '.join(subdomains or [])} {str(content or '')[:200]}".lower()

    mapping = [
        (["farming", "agriculture", "livestock", "animal husbandry", "poultry",
          "cattle", "crop", "soil fertility", "irrigation for crops"], "Agriculture & Livestock"),
        (["foraging", "hunting", "fishing", "bushcraft", "wilderness", "survival skill",
          "fire starting", "shelter building", "trapping", "tracking"], "Wilderness Skills"),
        (["food preservation", "canning", "dehydration", "smoking", "pickling",
          "fermentation", "food storage", "freeze dry"], "Preservation & Storage"),
        (["cooking", "recipe", "nutrition", "food preparation", "baking",
          "food production", "meal"], "Food Systems"),
        (["first aid", "medical", "trauma", "surgery", "anatomy", "pharmacology",
          "wound", "triage", "diagnosis", "disease", "infection", "veterinary",
          "herbal medicine", "medicinal plant"], "Medical"),
        (["radio", "antenna", "ham radio", "communication", "signal",
          "networking", "meshtastic", "comms"], "Communications"),
        (["solar", "battery", "generator", "wind turbine", "hydroelectric",
          "power grid", "inverter", "photovoltaic", "electricity"], "Power Systems"),
        (["water purification", "water filter", "well", "rainwater",
          "sanitation", "water treatment", "desalination"], "Water Systems"),
        (["navigation", "compass", "map reading", "gps", "celestial",
          "orienteering", "land nav"], "Navigation"),
        (["security", "opsec", "perimeter", "surveillance", "threat",
          "intrusion detection", "physical security"], "Security"),
        (["vehicle", "engine", "motor", "aircraft", "boat", "motorcycle",
          "truck", "maintenance", "diesel", "transmission"], "Vehicles"),
        (["tool", "equipment", "wrench", "saw", "drill", "hammer",
          "hand tool", "power tool", "blade", "sharpening"], "Tools & Equipment"),
        (["construction", "building", "shelter", "carpentry", "masonry",
          "roofing", "concrete", "framing", "plumbing"], "Shelter & Construction"),
        (["electronics", "computer", "software", "circuit", "programming",
          "technology", "digital", "engineering"], "Technology"),
        (["supply chain", "logistics", "transport", "distribution",
          "inventory", "supply", "stockpile"], "Logistics"),
        (["governance", "civil", "community", "administration", "organization",
          "council", "democratic", "municipal"], "Civil Organization"),
        (["tactics", "combat", "military", "mission", "patrol", "ambush",
          "defensive position", "fire team", "maneuver", "engagement",
          "search and rescue", "sar", "reconnaissance"], "Operations"),
    ]

    for keywords, domain in mapping:
        if any(kw in text for kw in keywords):
            return domain

    return DOMAIN_FALLBACK


# ── Checkpoint ──────────────────────────────────────────────────────────────

class Checkpoint:
    """Thread-safe checkpoint tracker for crash recovery."""
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._completed = set()
        self._dirty = 0
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._completed = set(data.get("completed", []))
                log.info(f"Loaded checkpoint: {len(self._completed):,} completed points")
            except Exception:
                self._completed = set()

    def is_done(self, point_id):
        return point_id in self._completed

    def mark_done(self, point_id):
        with self._lock:
            self._completed.add(point_id)
            self._dirty += 1
            if self._dirty >= 1000:
                self._flush()

    def _flush(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({"completed": list(self._completed)}))
        tmp.rename(self.path)
        self._dirty = 0

    def flush(self):
        with self._lock:
            self._flush()

    def count(self):
        return len(self._completed)


# ── Per-point processing ───────────────────────────────────────────────────

def process_point(point, qdrant, collection, key_rotator, checkpoint, dry_run, stats):
    point_id = point.id
    if checkpoint.is_done(point_id):
        return "skipped"

    payload = point.payload
    content = payload.get("content", payload.get("summary", ""))
    summary = payload.get("summary", "")
    subdomains = payload.get("subdomain", [])
    if isinstance(subdomains, str):
        subdomains = [subdomains]
    old_domain = payload.get("domain", [])
    if isinstance(old_domain, list):
        old_domain_str = old_domain[0] if old_domain else "(empty)"
    else:
        old_domain_str = str(old_domain)

    key = key_rotator.next()
    new_domain = classify_domain(content, summary, subdomains, key)

    # Track the mapping
    stats_key = f"{old_domain_str} -> {new_domain}"
    stats[stats_key] = stats.get(stats_key, 0) + 1

    if dry_run:
        return f"would: {old_domain_str} -> {new_domain}"

    # Write new domain as single string
    qdrant.set_payload(
        collection_name=collection,
        payload={"domain": new_domain},
        points=[point_id],
    )

    checkpoint.mark_done(point_id)
    return "ok"


# ── Main loop ───────────────────────────────────────────────────────────────

SCROLL_BATCH = 5000


def count_source_domains(qdrant, collection):
    """Count vectors with source domains."""
    counts = {}
    for domain in SOURCE_DOMAINS:
        result = qdrant.count(
            collection_name=collection,
            count_filter=Filter(
                must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
            ),
            exact=True,
        )
        counts[domain] = result.count
    return counts


def stream_and_process(qdrant, collection, rotator, checkpoint, workers, limit=None, dry_run=False):
    """Scroll source domains in batches, process with thread pool."""
    lock = threading.Lock()
    done = 0
    skipped_checkpoint = 0
    start = time.time()
    stats = {}  # shared mapping stats

    for source_domain in sorted(SOURCE_DOMAINS):
        log.info(f"\n--- Processing domain: {source_domain} ---")
        offset = None
        domain_done = 0

        while True:
            scroll_results, offset = qdrant.scroll(
                collection_name=collection,
                limit=SCROLL_BATCH,
                with_payload=True,
                with_vectors=False,
                offset=offset,
                scroll_filter=Filter(
                    must=[FieldCondition(key="domain", match=MatchValue(value=source_domain))]
                ),
            )

            if not scroll_results:
                if offset is None:
                    break
                continue

            # Filter already checkpointed
            pending = [p for p in scroll_results if not checkpoint.is_done(p.id)]
            skipped_checkpoint += len(scroll_results) - len(pending)

            if pending:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {
                        ex.submit(process_point, p, qdrant, collection, rotator,
                                  checkpoint, dry_run, stats): p
                        for p in pending
                    }
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            log.error(f"Worker error: {e}")
                        with lock:
                            done += 1
                            domain_done += 1
                            if done % 5000 == 0:
                                elapsed = time.time() - start
                                rate = done / elapsed * 60
                                log.info(f"  {done:,} done | {rate:.0f}/min | "
                                         f"elapsed {elapsed/60:.1f}min")
                                checkpoint.flush()
                        time.sleep(0.02)

            if limit and done >= limit:
                break
            if offset is None:
                break

        log.info(f"  {source_domain}: {domain_done:,} vectors processed")

        if limit and done >= limit:
            break

    checkpoint.flush()
    return done, skipped_checkpoint, stats, start


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify 20 samples without writing")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    keys = load_gemini_keys()
    rotator = KeyRotator(keys)

    qdrant = QdrantClient(host="localhost", port=6333, timeout=120)
    collection = "recon_knowledge"
    checkpoint = Checkpoint(CHECKPOINT_FILE)

    # Count source domains
    counts = count_source_domains(qdrant, collection)
    total_source = sum(counts.values())
    pre_checkpoint = checkpoint.count()

    log.info(f"Source domain counts:")
    for domain, count in sorted(counts.items(), key=lambda x: -x[1]):
        log.info(f"  {domain:30s} {count:>10,}")
    log.info(f"  {'TOTAL':30s} {total_source:>10,}")
    log.info(f"Checkpoint: {pre_checkpoint:,} already completed")
    log.info(f"Workers: {args.workers} | Keys: {len(keys)}")

    # Cost estimate
    remaining = total_source - pre_checkpoint
    input_tokens = remaining * 200
    output_tokens = remaining * 5
    input_cost = input_tokens / 1_000_000 * 0.10
    output_cost = output_tokens / 1_000_000 * 0.40
    total_cost = input_cost + output_cost
    log.info(f"\nEstimated Gemini 2.0 Flash cost:")
    log.info(f"  Vectors to process: {remaining:,}")
    log.info(f"  Input:  ~{input_tokens/1_000_000:.1f}M tokens = ${input_cost:.2f}")
    log.info(f"  Output: ~{output_tokens/1_000_000:.1f}M tokens = ${output_cost:.2f}")
    log.info(f"  TOTAL:  ~${total_cost:.2f}")

    if args.dry_run:
        log.info(f"\nDRY RUN: classifying 20 samples...\n")
        for source_domain in sorted(SOURCE_DOMAINS):
            scroll_results, _ = qdrant.scroll(
                collection_name=collection,
                limit=5,
                with_payload=True,
                with_vectors=False,
                scroll_filter=Filter(
                    must=[FieldCondition(key="domain", match=MatchValue(value=source_domain))]
                ),
            )
            for p in scroll_results[:4]:
                pay = p.payload
                title = pay.get("title", "(no title)")
                content = pay.get("content", pay.get("summary", ""))
                summary = pay.get("summary", "")
                subdomains = pay.get("subdomain", [])
                if isinstance(subdomains, str):
                    subdomains = [subdomains]

                key = rotator.next()
                new_domain = classify_domain(content, summary, subdomains, key)

                old = pay.get("domain", [])
                if isinstance(old, list):
                    old = old[0] if old else "?"
                print(f"  [{old:25s}] -> [{new_domain:25s}]  {title[:60]}")

        print(f"\nDRY RUN complete. ~{remaining:,} vectors would be migrated.")
        print(f"Estimated cost: ~${total_cost:.2f}")
        return

    # ── Full migration ──────────────────────────────────────────────────
    log.info(f"\nStarting full migration...")

    done, skipped_ckpt, stats, start = stream_and_process(
        qdrant, collection, rotator, checkpoint, args.workers, args.limit
    )

    elapsed = time.time() - start
    log.info(f"\n{'='*70}")
    log.info(f"MIGRATION COMPLETE in {elapsed/60:.1f}min:")
    log.info(f"  Processed:            {done:,}")
    log.info(f"  Skipped (checkpoint): {skipped_ckpt:,}")
    log.info(f"  Rate:                 {done/elapsed*60:.0f}/min")
    log.info(f"\nMapping distribution:")
    for mapping, count in sorted(stats.items(), key=lambda x: -x[1])[:30]:
        log.info(f"  {mapping:<55s} {count:>8,}")


if __name__ == "__main__":
    main()
