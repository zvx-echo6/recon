#!/usr/bin/env python3
"""
migrate_skill_level.py — Replaces skill_level with knowledge_type + complexity
on all vectors in Qdrant and on-disk concept JSONs.

Scrolls entire collection, classifies each concept via Gemini Flash,
writes knowledge_type + complexity, deletes skill_level.

Crash-safe: completed point IDs tracked in checkpoint file.

Usage:
  python3 /opt/recon/scripts/migrate_skill_level.py [--dry-run] [--workers 16] [--limit N]
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

import sys
sys.path.insert(0, '/opt/recon')
from lib.utils import get_config, setup_logging

# Suppress noisy HTTP request logging from qdrant_client/httpx
import logging as _logging
_logging.getLogger("httpx").setLevel(_logging.WARNING)
_logging.getLogger("qdrant_client").setLevel(_logging.WARNING)

LOG_FILE = Path("/opt/recon/logs/migrate_skill_level.log")
CHECKPOINT_FILE = Path("/opt/recon/data/migrate_skill_level_checkpoint.json")
CONCEPTS_DIR = Path("/opt/recon/data/concepts")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("migrate_skill_level")

# ── Prompt ──────────────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are a knowledge classification engine. Given a concept, assign two fields:

knowledge_type — what KIND of knowledge this is:
  foundational — concepts, definitions, theory, background knowledge, explanations of how things work
  procedural — step-by-step techniques, instructions, how-to skills, methods you execute
  operational — application under real conditions, decision-making, mission execution, judgment calls in context

complexity — how much prior knowledge is needed:
  basic — requires little or no prior knowledge, introductory material, simple concepts
  intermediate — requires some domain familiarity, assumes foundational knowledge is in place
  advanced — requires significant experience or expertise, high-stakes or highly technical material

EXAMPLES:
- "Needle chest decompression procedure" → procedural, advanced
- "What is soil texture and why does it matter" → foundational, basic
- "Coordinating a fire team withdrawal under contact" → operational, advanced
- "How to start a campfire with a ferro rod" → procedural, basic
- "Antenna gain and radiation patterns explained" → foundational, intermediate
- "Triage decision-making in a mass casualty event" → operational, advanced
- "Step-by-step: building a Dakota fire hole" → procedural, intermediate
- "Understanding the water cycle" → foundational, basic

Concept title: {title}
Concept domain: {domain}
Concept subdomain: {subdomain}
Concept content: {content}

Return ONLY valid JSON, no markdown, no explanation:
{{"knowledge_type": "foundational|procedural|operational", "complexity": "basic|intermediate|advanced"}}
"""

VALID_KNOWLEDGE_TYPES = {"foundational", "procedural", "operational"}
VALID_COMPLEXITIES = {"basic", "intermediate", "advanced"}

# ── Key management ──────────────────────────────────────────────────────────

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

# ── Classification ──────────────────────────────────────────────────────────

def classify(title, domains, subdomains, content, key):
    """Call Gemini Flash to classify knowledge_type + complexity."""
    prompt = CLASSIFY_PROMPT.format(
        title=title or "(untitled)",
        domain=", ".join(domains[:5]) if domains else "(none)",
        subdomain=", ".join(subdomains[:10]) if subdomains else "(none)",
        content=str(content)[:400] if content else "(none)",
    )
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        "gemini-2.0-flash",
        generation_config={"response_mime_type": "application/json"}
    )
    for retry in range(4):
        try:
            resp = model.generate_content(prompt)
            data = json.loads(resp.text)
            kt = data.get("knowledge_type", "").lower().strip()
            cx = data.get("complexity", "").lower().strip()
            if kt in VALID_KNOWLEDGE_TYPES and cx in VALID_COMPLEXITIES:
                return kt, cx
            # Invalid values — retry once
            if retry == 0:
                continue
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503", "unavailable"]):
                time.sleep(min(5 * (2 ** retry) + random.uniform(0, 3), 60))
            else:
                break

    # Fallback heuristic based on old skill_level + content analysis
    return heuristic_fallback(title, subdomains, content)


def heuristic_fallback(title, subdomains, content):
    """Last-resort heuristic when Gemini fails."""
    text = f"{title} {' '.join(subdomains)} {str(content)[:200]}".lower()

    # Knowledge type heuristic
    procedural_signals = ["how to", "step-by-step", "procedure", "instructions",
                          "method", "technique", "build", "make", "construct",
                          "install", "assemble", "recipe", "prepare"]
    operational_signals = ["decision", "coordinate", "execute", "deploy",
                           "mission", "triage", "under fire", "in the field",
                           "real-world", "scenario", "assessment", "plan"]

    if any(s in text for s in operational_signals):
        kt = "operational"
    elif any(s in text for s in procedural_signals):
        kt = "procedural"
    else:
        kt = "foundational"

    # Complexity heuristic — default intermediate (safest middle ground)
    cx = "intermediate"
    basic_signals = ["introduction", "what is", "basic", "beginner", "overview",
                     "definition", "simple", "fundamentals"]
    advanced_signals = ["advanced", "expert", "complex", "critical", "high-stakes",
                        "surgery", "trauma", "tactical", "classified"]
    if any(s in text for s in basic_signals):
        cx = "basic"
    elif any(s in text for s in advanced_signals):
        cx = "advanced"

    return kt, cx

# ── Checkpoint management ───────────────────────────────────────────────────

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

# ── Concept JSON update ────────────────────────────────────────────────────

def update_concept_json(doc_hash, title, knowledge_type, complexity):
    """Update on-disk concept JSON: add knowledge_type + complexity, remove skill_level."""
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
                    c["knowledge_type"] = knowledge_type
                    c["complexity"] = complexity
                    c.pop("skill_level", None)
                    changed = True
            if changed:
                with open(wf, "w", encoding="utf-8") as f:
                    json.dump(concepts, f, indent=2, ensure_ascii=False)
                return True
        except Exception:
            pass
    return False

# ── Per-point processing ───────────────────────────────────────────────────

def process_point(point, qdrant, collection, key_rotator, checkpoint, dry_run):
    point_id = point.id
    if checkpoint.is_done(point_id):
        return "skipped"

    payload = point.payload
    title = payload.get("title", "")
    domains = payload.get("domain", [])
    if isinstance(domains, str):
        domains = [domains]
    subdomains = payload.get("subdomain", [])
    if isinstance(subdomains, str):
        subdomains = [subdomains]
    content = payload.get("content", payload.get("summary", ""))
    doc_hash = payload.get("doc_hash", "")

    key = key_rotator.next()
    knowledge_type, complexity = classify(title, domains, subdomains, content, key)

    if dry_run:
        return f"kt={knowledge_type}, cx={complexity}"

    # Write new fields
    qdrant.set_payload(
        collection_name=collection,
        payload={"knowledge_type": knowledge_type, "complexity": complexity},
        points=[point_id],
    )

    # Delete old field
    qdrant.delete_payload(
        collection_name=collection,
        keys=["skill_level"],
        points=[point_id],
    )

    # Update JSON on disk
    if doc_hash:
        update_concept_json(doc_hash, title, knowledge_type, complexity)

    checkpoint.mark_done(point_id)
    return "ok"

# ── Streaming batch processor ───────────────────────────────────────────────

SCROLL_BATCH = 5000  # vectors per scroll batch — keeps memory bounded (~50MB)


def count_collection(qdrant, collection):
    """Quick count of total vectors via collection info."""
    info = qdrant.get_collection(collection)
    return info.points_count


def stream_and_process(qdrant, collection, rotator, checkpoint, workers, limit=None):
    """Scroll in batches, process each batch with thread pool, then discard.

    Memory-bounded: only holds SCROLL_BATCH payloads at any time (~50MB).
    """
    results_agg = defaultdict(int)
    lock = threading.Lock()
    done = 0
    skipped_checkpoint = 0
    skipped_no_skill = 0
    total_estimate = count_collection(qdrant, collection)
    start = time.time()

    offset = None
    batch_num = 0

    while True:
        batch_num += 1
        scroll_results, offset = qdrant.scroll(
            collection_name=collection,
            limit=SCROLL_BATCH,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )

        # Filter to points needing migration
        pending = []
        for p in scroll_results:
            if "skill_level" not in p.payload:
                skipped_no_skill += 1
                continue
            if checkpoint.is_done(p.id):
                skipped_checkpoint += 1
                continue
            pending.append(p)

        if pending:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(process_point, p, qdrant, collection, rotator, checkpoint, False): p
                    for p in pending
                }
                for future in as_completed(futures):
                    try:
                        status = future.result()
                    except Exception as e:
                        status = f"error: {str(e)[:80]}"
                        log.error(f"Worker error: {e}")
                    with lock:
                        results_agg[status] += 1
                        done += 1
                        if done % 5000 == 0:
                            elapsed = time.time() - start
                            rate = done / elapsed * 60
                            remaining = total_estimate - done - skipped_checkpoint - skipped_no_skill
                            eta = remaining / (done / elapsed) / 60 if done > 0 else 0
                            log.info(f"  {done:,} done | {rate:.0f}/min | "
                                     f"ETA ~{eta:.0f}min | {dict(results_agg)}")
                            checkpoint.flush()
                    time.sleep(0.02)

        if limit and done >= limit:
            break
        if offset is None:
            break

    checkpoint.flush()
    return done, skipped_checkpoint, skipped_no_skill, results_agg, start


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify 20 samples without writing anything")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    config = get_config()
    keys = load_gemini_keys()
    rotator = KeyRotator(keys)

    qdrant = QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=120
    )
    collection = config['vector_db']['collection']
    checkpoint = Checkpoint(CHECKPOINT_FILE)

    total_vectors = count_collection(qdrant, collection)
    pre_checkpoint = checkpoint.count()

    log.info(f"Collection has {total_vectors:,} vectors")
    log.info(f"Checkpoint: {pre_checkpoint:,} already completed")
    log.info(f"Workers: {args.workers} | Keys: {len(keys)} | Dry run: {args.dry_run}")
    log.info(f"Estimated Gemini Flash cost: ~${(total_vectors - pre_checkpoint) * 0.0004:.2f}")
    log.info(f"Streaming in batches of {SCROLL_BATCH:,} (memory-bounded)")

    if args.dry_run:
        # Scroll one batch, classify 20 diverse samples
        log.info(f"\nDRY RUN: classifying 20 samples...\n")
        scroll_results, _ = qdrant.scroll(
            collection_name=collection,
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        samples = []
        seen_domains = set()
        for p in scroll_results:
            if "skill_level" not in p.payload:
                continue
            domains = p.payload.get("domain", [])
            if isinstance(domains, str):
                domains = [domains]
            d_key = tuple(sorted(domains[:2]))
            if d_key not in seen_domains:
                samples.append(p)
                seen_domains.add(d_key)
            if len(samples) >= 20:
                break

        for i, p in enumerate(samples, 1):
            pay = p.payload
            title = pay.get("title", "(no title)")
            domains = pay.get("domain", [])
            old_skill = pay.get("skill_level", "?")
            subdomains = pay.get("subdomain", [])
            if isinstance(subdomains, str):
                subdomains = [subdomains]
            content = pay.get("content", pay.get("summary", ""))

            key = rotator.next()
            kt, cx = classify(title, domains, subdomains, content, key)

            print(f"\n--- Sample {i}/{len(samples)} ---")
            print(f"  Title:          {title}")
            print(f"  Domain:         {domains}")
            print(f"  Old skill:      {old_skill}")
            print(f"  → knowledge_type: {kt}")
            print(f"  → complexity:     {cx}")
        est = total_vectors - pre_checkpoint
        print(f"\nDRY RUN complete. ~{est:,} vectors would be migrated.")
        print(f"Estimated Gemini Flash cost: ~${est * 0.0004:.2f}")
        return

    # ── Full migration run (streaming) ──────────────────────────────────────
    done, skipped_ckpt, skipped_no_skill, results, start = stream_and_process(
        qdrant, collection, rotator, checkpoint, args.workers, args.limit
    )

    elapsed = time.time() - start
    log.info(f"\nComplete in {elapsed/60:.1f}min:")
    log.info(f"  Processed:           {done:,}")
    log.info(f"  Skipped (checkpoint): {skipped_ckpt:,}")
    log.info(f"  Skipped (no skill):   {skipped_no_skill:,}")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        log.info(f"  {status:<30} {count:>10,}")


if __name__ == "__main__":
    main()
