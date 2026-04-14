import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import google.generativeai as genai

from .utils import get_config, setup_logging
from .status import StatusDB

logger = setup_logging('recon.enricher')


def repair_json(text):
    """Attempt to repair common LLM JSON output issues including truncation."""
    # Remove control characters except newlines and tabs
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # Handle truncated JSON: try to find the last complete object in the array
    try:
        json.loads(text, strict=False)
        return text
    except json.JSONDecodeError:
        pass

    # Find the last complete }, then close the array
    # Walk backward to find the last valid closing brace
    last_complete = -1
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth_brace += 1
        elif ch == '}':
            depth_brace -= 1
            if depth_brace == 0:
                last_complete = i
        elif ch == '[':
            depth_bracket += 1
        elif ch == ']':
            depth_bracket -= 1

    if last_complete > 0:
        truncated = text[:last_complete + 1].rstrip().rstrip(',')
        # Close any open arrays
        open_brackets = truncated.count('[') - truncated.count(']')
        truncated += ']' * open_brackets
        return truncated

    return text

ENRICH_PROMPT = """Extract knowledge concepts from this document text.

A concept is a SELF-CONTAINED piece of knowledge that can stand alone.

For each concept, provide ALL fields:

Required:
- content: Full text of the concept (complete procedure, definition, etc.)
- summary: 1-2 sentence summary
- title: Brief descriptive title
- domain: Array of 1-5 from: Foundational Skills, Sustainment Systems, Defense & Tactics, Off-Grid Systems, Communications, Scenario Playbooks, Reference
- subdomain: Array of specific subcategories (up to 10)
- keywords: Array of 3-30 searchable terms
- skill_level: novice | intermediate | advanced
- key_facts: Array of specific extractable claims, measurements, data points

Optional (include when present):
- scenario_applicable: Array from: tuesday_prepper, month_prepper, year_prepper, multi_year, eotwawki
- cross_domain_tags: Array from: sustainment, medical, security, communications, leadership, logistics, navigation, power_systems, water_systems, food_systems, tactical_ops, community_coordination
- chapter: Chapter name if identifiable
- page_ref: Page reference
- notes: Any additional context

Return JSON array. If no extractable concepts, return [].

Document text:
"""


class KeyRotator:
    def __init__(self, keys):
        self.keys = keys
        self.index = 0

    def next(self):
        if not self.keys:
            raise ValueError("No Gemini API keys configured")
        key = self.keys[self.index % len(self.keys)]
        self.index += 1
        return key


def enrich_window(text, key, config):
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        config['gemini']['model'],
        generation_config={"response_mime_type": config['gemini']['response_mime_type']}
    )
    response = model.generate_content(ENRICH_PROMPT + text)
    raw = response.text
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = repair_json(raw)
        return json.loads(repaired, strict=False)


def enrich_single(file_hash, db, config, key_rotator):
    doc = db.get_document(file_hash)
    if not doc:
        return False

    text_dir = os.path.join(config['paths']['text'], file_hash)
    concepts_dir = os.path.join(config['paths']['concepts'], file_hash)
    window_size = config['processing']['enrich_window_size']
    delay = config['processing']['rate_limit_delay']
    max_retries = config['processing']['max_retries']

    if not os.path.exists(text_dir):
        db.mark_failed(file_hash, f"Text directory not found: {text_dir}")
        return False

    db.update_status(file_hash, 'enriching')

    try:
        os.makedirs(concepts_dir, exist_ok=True)

        page_files = sorted([f for f in os.listdir(text_dir) if f.startswith('page_') and f.endswith('.txt')])
        if not page_files:
            db.mark_failed(file_hash, "No page files found")
            return False

        pages_text = []
        for pf in page_files:
            with open(os.path.join(text_dir, pf), encoding='utf-8') as f:
                pages_text.append(f.read())

        windows = []
        for i in range(0, len(pages_text), window_size):
            window_pages = pages_text[i:i + window_size]
            combined = "\n\n".join(f"--- Page {i + j + 1} ---\n{t}" for j, t in enumerate(window_pages))
            windows.append((i, combined))

        total_concepts = 0
        for w_idx, (start_page, window_text) in enumerate(windows):
            window_file = os.path.join(concepts_dir, f"window_{w_idx+1:04d}.json")

            if os.path.exists(window_file):
                with open(window_file, encoding='utf-8') as f:
                    existing = json.load(f)
                total_concepts += len(existing)
                logger.debug(f"  Window {w_idx+1} already exists, skipping")
                continue

            if len(window_text.strip()) < 50:
                with open(window_file, 'w') as f:
                    json.dump([], f)
                continue

            concepts = None
            for attempt in range(max_retries):
                try:
                    key = key_rotator.next()
                    concepts = enrich_window(window_text, key, config)
                    break
                except Exception as e:
                    logger.warning(f"  Window {w_idx+1} attempt {attempt+1} failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1) * 2)

            if concepts is None:
                db.mark_failed(file_hash, f"All retries failed for window {w_idx+1}")
                return False

            if not isinstance(concepts, list):
                concepts = [concepts] if isinstance(concepts, dict) else []

            for c_idx, concept in enumerate(concepts):
                concept['_window'] = w_idx + 1
                concept['_start_page'] = start_page + 1
                concept['_doc_hash'] = file_hash

            # JSON FIRST: save before anything else
            with open(window_file, 'w', encoding='utf-8') as f:
                json.dump(concepts, f, indent=2, ensure_ascii=False)

            total_concepts += len(concepts)
            logger.debug(f"  Window {w_idx+1}/{len(windows)}: {len(concepts)} concepts")
            time.sleep(delay)

        meta = {
            'hash': file_hash,
            'total_windows': len(windows),
            'total_concepts': total_concepts,
            'window_size': window_size,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        with open(os.path.join(concepts_dir, 'meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)

        db.update_status(file_hash, 'enriched', concepts_extracted=total_concepts)
        logger.info(f"Enriched {doc['filename']}: {total_concepts} concepts from {len(windows)} windows")
        return True

    except Exception as e:
        logger.error(f"Enrichment failed for {file_hash}: {e}\n{traceback.format_exc()}")
        db.mark_failed(file_hash, str(e))
        return False


def run_enrichment(workers=None, limit=None):
    config = get_config()
    db = StatusDB()
    workers = workers or config['processing']['enrich_workers']

    keys = config.get('gemini_keys', [])
    if not keys:
        logger.error("No Gemini API keys configured in .env")
        return 0

    key_rotator = KeyRotator(keys)

    extracted = db.get_by_status('extracted', limit=limit)
    if not extracted:
        logger.info("No extracted documents to enrich")
        return 0

    logger.info(f"Enriching {len(extracted)} documents with {workers} workers, {len(keys)} API key(s)")
    success = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(enrich_single, doc['hash'], StatusDB(), config, key_rotator): doc
            for doc in extracted
        }
        for future in as_completed(futures):
            doc = futures[future]
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker error for {doc['hash']}: {e}")

    logger.info(f"Enrichment complete: {success}/{len(extracted)} succeeded")
    return success
