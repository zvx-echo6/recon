#!/usr/bin/env python3
"""
repair_corrupted.py — Repairs window files corrupted by concurrent writes.

Strategy:
  1. Read corrupted_windows.txt to get the list of bad files
  2. For each bad file, identify the parent doc hash from the path
  3. Check if the text directory still exists for that doc
  4. If yes: re-run Gemini enrichment on just that window
  5. If no text: mark as unrecoverable
  6. Report summary

Usage:
  python3 /opt/recon/scripts/repair_corrupted.py [--dry-run] [--workers 8]
"""

import json
import time
import random
import logging
import argparse
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import google.generativeai as genai

CORRUPTED_LIST = Path("/opt/recon/data/corrupted_windows.txt")
TEXT_DIR = Path("/opt/recon/data/text")
CONCEPTS_DIR = Path("/opt/recon/data/concepts")
LOG_FILE = Path("/opt/recon/logs/repair_corrupted.log")
UNRECOVERABLE_LOG = Path("/opt/recon/data/unrecoverable_windows.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("repair_corrupted")

CANONICAL_DOMAINS = [
    "Defense & Tactics", "Sustainment Systems", "Off-Grid Systems",
    "Foundational Skills", "Communications", "Medical", "Food Systems",
    "Navigation", "Logistics", "Power Systems", "Leadership",
    "Scenario Playbooks", "Water Systems", "Security", "Community Coordination"
]

ENRICH_PROMPT = """Extract knowledge concepts from this document text.

A concept is a SELF-CONTAINED piece of knowledge that can stand alone.

For each concept, provide ALL fields:

Required:
- content: Full text of the concept (complete procedure, definition, etc.)
- summary: 1-2 sentence summary
- title: Brief descriptive title
- domain: Array of 1-5 from ONLY these exact strings (no others):
    Defense & Tactics, Sustainment Systems, Off-Grid Systems, Foundational Skills,
    Communications, Medical, Food Systems, Navigation, Logistics, Power Systems,
    Leadership, Scenario Playbooks, Water Systems, Security, Community Coordination
  CRITICAL: Do NOT use "Reference". Every concept belongs somewhere specific.
- subdomain: Array of specific subcategories (up to 10)
- keywords: Array of 3-30 searchable terms
- skill_level: novice | intermediate | advanced
- key_facts: Array of specific extractable claims, measurements, data points

Optional (include when present):
- scenario_applicable: Array from: tuesday_prepper, month_prepper, year_prepper, multi_year, eotwawki
- cross_domain_tags: Array from: sustainment, medical, security, communications, leadership, logistics, navigation, power_systems, water_systems, food_systems, tactical_ops, community_coordination
- chapter: Chapter name if identifiable
- page_ref: Page reference

Return JSON array. If no extractable concepts, return [].

Document text:
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

def repair_json_truncated(text):
    """Last-ditch attempt to salvage a truncated JSON array."""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find last complete object
    last_close = -1
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False; continue
        if ch == '\\' and in_str:
            esc = True; continue
        if ch == '"' and not esc:
            in_str = not in_str; continue
        if in_str:
            continue
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                last_close = i
    if last_close > 0:
        trimmed = text[:last_close + 1].rstrip().rstrip(',')
        open_brackets = trimmed.count('[') - trimmed.count(']')
        try:
            return json.loads(trimmed + ']' * open_brackets)
        except Exception:
            pass
    return None

def enrich_window_text(text, key):
    """Call Gemini on raw window text, return concepts list."""
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash-lite",
        generation_config={"response_mime_type": "application/json"}
    )
    for attempt in range(4):
        try:
            resp = model.generate_content(ENRICH_PROMPT + text)
            raw = resp.text
            try:
                result = json.loads(raw)
            except Exception:
                result = repair_json_truncated(raw)
            if isinstance(result, list):
                return [c for c in result if isinstance(c, dict)]
            elif isinstance(result, dict):
                return [result]
            return []
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ["429", "quota", "rate", "503", "unavailable"]):
                delay = min(5 * (2 ** attempt) + random.uniform(0, 3), 60)
                time.sleep(delay)
            else:
                log.warning(f"  Non-transient error: {e}")
                break
    return None  # failed

def get_window_text(doc_hash, window_filename):
    """Reconstruct window text from page files."""
    # Window filename: window_NNNN.json -> window index is NNNN
    try:
        w_idx = int(Path(window_filename).stem.split('_')[1]) - 1
    except (IndexError, ValueError):
        return None

    text_path = TEXT_DIR / doc_hash
    if not text_path.exists():
        return None

    page_files = sorted([
        f for f in text_path.iterdir()
        if f.name.startswith('page_') and f.name.endswith('.txt')
    ])
    if not page_files:
        return None

    # Re-derive which pages this window covered (window_size=5 from config)
    window_size = 5
    start = w_idx * window_size
    window_pages = page_files[start:start + window_size]
    if not window_pages:
        return None

    parts = []
    for j, pf in enumerate(window_pages):
        try:
            text = pf.read_text(encoding='utf-8')
            parts.append(f"--- Page {start + j + 1} ---\n{text}")
        except Exception:
            pass
    return "\n\n".join(parts) if parts else None

def repair_file(corrupted_path, key_rotator, dry_run):
    """Attempt to repair a single corrupted window file."""
    path = Path(corrupted_path)

    # Sanity check -- maybe it fixed itself somehow
    try:
        with open(path) as f:
            existing = json.load(f)
        return "already_valid"
    except Exception:
        pass

    # Extract doc hash and window name from path structure
    # Expected: /opt/recon/data/concepts/{hash}/window_NNNN.json
    doc_hash = path.parent.name
    window_filename = path.name

    # Get source text for this window
    window_text = get_window_text(doc_hash, window_filename)
    if not window_text:
        return "no_source_text"

    if dry_run:
        return "would_repair"

    # Re-enrich from source text
    key = key_rotator.next()
    concepts = enrich_window_text(window_text, key)

    if concepts is None:
        return "enrichment_failed"

    # Tag concepts with metadata
    try:
        w_idx = int(Path(window_filename).stem.split('_')[1]) - 1
        window_size = 5
        start_page = w_idx * window_size + 1
    except Exception:
        w_idx = 0
        start_page = 0

    for c in concepts:
        c['_window'] = w_idx + 1
        c['_start_page'] = start_page
        c['_doc_hash'] = doc_hash
        c['_repaired'] = True

    # Write repaired file
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(concepts, f, indent=2, ensure_ascii=False)
        return "repaired"
    except Exception as e:
        return "write_error"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    if not CORRUPTED_LIST.exists():
        log.error(f"Corrupted list not found: {CORRUPTED_LIST}")
        log.error("Run Task 1 first to generate it.")
        return

    keys = load_gemini_keys()
    rotator = KeyRotator(keys)

    corrupted = []
    with open(CORRUPTED_LIST) as f:
        for line in f:
            parts = line.strip().split('\t')
            if parts:
                corrupted.append(parts[0])

    log.info(f"Repairing {len(corrupted):,} corrupted window files")
    log.info(f"Dry run: {args.dry_run} | Workers: {args.workers} | Keys: {len(keys)}")

    results = defaultdict(int)
    unrecoverable = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(repair_file, p, rotator, args.dry_run): p for p in corrupted}
        done = 0
        for future in as_completed(futures):
            path = futures[future]
            status = future.result()
            with lock:
                results[status] += 1
                if status in ("no_source_text", "enrichment_failed", "write_error"):
                    unrecoverable.append((path, status))
                done += 1
                if done % 100 == 0:
                    log.info(f"  {done:,}/{len(corrupted):,} | {dict(results)}")
            time.sleep(0.05)

    log.info("── Results ─────────────────────────────────────────────────")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        log.info(f"  {status:<25} {count:>8,}")

    if unrecoverable:
        with open(UNRECOVERABLE_LOG, 'w') as f:
            for path, reason in unrecoverable:
                f.write(f"{path}\t{reason}\n")
        log.info(f"\n  Unrecoverable: {len(unrecoverable)} — logged to {UNRECOVERABLE_LOG}")
    else:
        log.info("\n  All files repaired successfully.")

if __name__ == "__main__":
    main()
