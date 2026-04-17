"""
RECON Enricher

Text to structured concepts via Gemini API. Saves JSON to data/concepts/{hash}/
BEFORE any DB operations. Uses 10-page windows, 4 API keys, 16 workers.

Resilience:
  - Exponential backoff with jitter for transient errors (429, 500, 503, timeout)
  - Permanent errors (JSON parse, auth) fail immediately without wasting retries
  - Window failures skip that window and continue — partial enrichment beats zero
  - Document marked enriched if ANY windows succeeded, failed only if ALL failed

Dependencies: google-generativeai
Config: processing.enrich_workers, processing.enrich_window_size, gemini, paths.concepts
"""
import json
import os
import random
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import google.generativeai as genai

from .utils import get_config, setup_logging
from .status import StatusDB
from .utils import resolve_text_dir

try:
    from langdetect import detect as _detect_lang
    from langdetect.lang_detect_exception import LangDetectException
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False

ALLOWED_LANGUAGES = {'en'}  # Default: English only

logger = setup_logging('recon.enricher')

# Docs stuck in "enriching" longer than this get reset to "extracted" for retry
STALE_ENRICHING_HOURS = 2

# ── Classification allowlists ───────────────────────────────────────────────
VALID_DOMAINS = {
    'Agriculture & Livestock', 'Civil Organization', 'Communications',
    'Food Systems', 'Foundational Skills', 'Logistics', 'Medical',
    'Navigation', 'Operations', 'Power Systems', 'Preservation & Storage',
    'Security', 'Shelter & Construction', 'Technology', 'Tools & Equipment',
    'Vehicles', 'Water Systems', 'Wilderness Skills',
}
VALID_KNOWLEDGE_TYPES = {'foundational', 'procedural', 'operational'}
VALID_COMPLEXITIES = {'basic', 'intermediate', 'advanced'}

DOMAIN_FALLBACK = 'Foundational Skills'
KNOWLEDGE_TYPE_FALLBACK = 'foundational'
COMPLEXITY_FALLBACK = 'basic'


def repair_json(text):
    """Attempt to repair common LLM JSON output issues including truncation."""
    # Remove control characters except newlines and tabs
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # Fix invalid JSON escape sequences (e.g. \e, \p, \c from Gemini)
    # Valid JSON escapes: \", \\, \/, \b, \f, \n, \r, \t, \uXXXX
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
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
- domain: must be exactly one of: Agriculture & Livestock, Civil Organization, Communications, Food Systems, Foundational Skills, Logistics, Medical, Navigation, Operations, Power Systems, Preservation & Storage, Security, Shelter & Construction, Technology, Tools & Equipment, Vehicles, Water Systems, Wilderness Skills — return ONLY this exact string, no variations, no new domains, no underscores, no synonyms
  CRITICAL: Medical content (first aid, anatomy, pharmacology, herbs, veterinary, austere medicine) → Medical
  CRITICAL: Food growing, farming, animal husbandry, livestock → Agriculture & Livestock
  CRITICAL: Foraging, hunting, fishing, bushcraft, wilderness survival → Wilderness Skills
  CRITICAL: Food preservation, storage, canning, dehydration, processing → Preservation & Storage
  CRITICAL: Solar, wind, hydro, batteries, generators → Power Systems
  CRITICAL: Water sourcing, filtration, sanitation, purification → Water Systems
  CRITICAL: Building, carpentry, structural construction, shelter → Shelter & Construction
  CRITICAL: Tactical operations, mission execution, combat maneuvers, search & rescue → Operations
  CRITICAL: Governance, civil administration, community leadership → Civil Organization
  CRITICAL: Electronics, IT, computing, engineering → Technology
  CRITICAL: Hand tools, power tools, equipment maintenance → Tools & Equipment
  CRITICAL: Motor vehicles, aircraft, watercraft, vehicle maintenance → Vehicles
  CRITICAL: Radio, signals, networking, comms equipment → Communications
  CRITICAL: Supply chain, transport, distribution, inventory → Logistics
  CRITICAL: Physical security, OPSEC, threat assessment → Security
  CRITICAL: Map reading, orienteering, GPS, celestial navigation → Navigation
  CRITICAL: Cooking methods, food production, recipes, nutrition → Food Systems
- subdomain: Array of specific subcategories (up to 10)
- keywords: Array of 3-30 searchable terms
- knowledge_type: foundational | procedural | operational
    foundational — concepts, definitions, theory, background knowledge, explanations of how things work
    procedural — step-by-step techniques, instructions, how-to skills, methods you execute
    operational — application under real conditions, decision-making, mission execution, judgment calls in context
    Valid values are ONLY: foundational, procedural, operational — do not use any other values
- complexity: basic | intermediate | advanced
    basic — requires little or no prior knowledge, introductory material, simple concepts
    intermediate — requires some domain familiarity, assumes foundational knowledge is in place
    advanced — requires significant experience or expertise, high-stakes or highly technical material
    Valid values are ONLY: basic, intermediate, advanced — do not use any other values
- key_facts: Array of specific extractable claims, measurements, data points

Optional (include when present):
- scenario_applicable: Array from: tuesday_prepper, month_prepper, year_prepper, multi_year, eotwawki
- cross_domain_tags: Array from: sustainment, medical, security, communications, leadership, logistics, navigation, power_systems, water_systems, food_systems, tactical_ops, community_coordination
- chapter: Chapter name if identifiable
- page_ref: Page reference
- notes: Any additional context

EXAMPLES (knowledge_type + complexity):
- "Needle chest decompression procedure" → knowledge_type: "procedural", complexity: "advanced"
- "What is soil texture and why does it matter" → knowledge_type: "foundational", complexity: "basic"
- "Coordinating a fire team withdrawal under contact" → knowledge_type: "operational", complexity: "advanced"

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
        result = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = repair_json(raw)
        result = json.loads(repaired, strict=False)
    # Filter out non-dict items (nested lists from truncated responses)
    if isinstance(result, list):
        result = [c for c in result if isinstance(c, dict)]
    return result


def _is_transient(error_str):
    """Classify whether an error is transient (worth retrying) or permanent."""
    s = error_str.lower()
    transient_signals = ['429', 'resource_exhausted', 'quota', 'rate',
                         '500', '503', 'unavailable', 'timeout',
                         'connection', 'reset by peer', 'broken pipe']
    return any(sig in s for sig in transient_signals)


def _retry_with_backoff(fn, max_retries=5, base_delay=5.0, max_delay=120.0):
    """Retry with exponential backoff + jitter for transient errors.

    Backoff: ~5s, ~10s, ~20s, ~40s, ~80s (total ~155s before giving up).
    Permanent errors (JSON parse, auth) raise immediately without retrying.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            err = str(e)
            if not _is_transient(err):
                raise  # permanent — don't waste retries
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, base_delay), max_delay)
                logger.info(f"    Transient error (attempt {attempt+1}/{max_retries}), "
                            f"retrying in {delay:.0f}s: {err[:120]}")
                time.sleep(delay)
            else:
                logger.warning(f"    Transient error, max retries exhausted: {err[:150]}")
    raise last_exc


def _reclassify_field(field_name, allowlist, concept, key, config, max_retries=3):
    """Retry Gemini up to max_retries to get a valid value for a specific field."""
    content = concept.get('content', concept.get('summary', ''))
    if isinstance(content, str):
        content = content[:400]
    else:
        content = str(content)[:400]
    title = concept.get('title', '(untitled)')
    allowlist_str = ', '.join(sorted(allowlist))

    for attempt in range(max_retries):
        try:
            prompt = (
                f"Your previous response for '{field_name}' was invalid. "
                f"You must return ONLY one of these exact strings: {allowlist_str}\n\n"
                f"Title: {title}\n"
                f"Content: {content}\n\n"
                f"Return ONLY the exact string, nothing else. No explanation, no punctuation, no quotes."
            )
            genai.configure(api_key=key)
            model = genai.GenerativeModel(
                config['gemini']['model'],
                generation_config={"response_mime_type": "text/plain"}
            )
            resp = model.generate_content(prompt)
            value = resp.text.strip().strip('"').strip("'").strip()
            if value in allowlist:
                return value
            # Try case-insensitive match for knowledge_type/complexity
            for valid in allowlist:
                if value.lower() == valid.lower():
                    return valid
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ['429', 'quota', 'rate', '503']):
                time.sleep(min(3 * (2 ** attempt) + random.uniform(0, 2), 30))
            else:
                logger.warning(f"  Reclassify retry {attempt+1} for {field_name} failed: {e}")
    return None


def validate_and_fix_concepts(concepts, key, config):
    """Validate domain, knowledge_type, complexity on each concept.

    For invalid values: retry Gemini up to 3 times, then apply safe fallback.
    """
    for concept in concepts:
        if not isinstance(concept, dict):
            continue

        # ── Validate domain ─────────────────────────────────────────────
        domain = concept.get('domain')
        if isinstance(domain, list):
            # Legacy array format — find first valid or reclassify
            valid = [d for d in domain if d in VALID_DOMAINS]
            if valid:
                concept['domain'] = valid[0]
            else:
                new_val = _reclassify_field('domain', VALID_DOMAINS, concept, key, config)
                if new_val:
                    concept['domain'] = new_val
                else:
                    logger.warning(f"Invalid domain {domain} for '{concept.get('title', '?')}', using fallback")
                    concept['domain'] = DOMAIN_FALLBACK
        elif isinstance(domain, str):
            if domain not in VALID_DOMAINS:
                new_val = _reclassify_field('domain', VALID_DOMAINS, concept, key, config)
                if new_val:
                    concept['domain'] = new_val
                else:
                    logger.warning(f"Invalid domain '{domain}' for '{concept.get('title', '?')}', using fallback")
                    concept['domain'] = DOMAIN_FALLBACK
        else:
            concept['domain'] = DOMAIN_FALLBACK

        # ── Validate knowledge_type ─────────────────────────────────────
        kt = concept.get('knowledge_type', '')
        if isinstance(kt, str):
            kt = kt.lower().strip()
        else:
            kt = ''
        if kt not in VALID_KNOWLEDGE_TYPES:
            new_val = _reclassify_field('knowledge_type', VALID_KNOWLEDGE_TYPES, concept, key, config)
            if new_val:
                concept['knowledge_type'] = new_val
            else:
                logger.warning(f"Invalid knowledge_type '{kt}' for '{concept.get('title', '?')}', using fallback")
                concept['knowledge_type'] = KNOWLEDGE_TYPE_FALLBACK
        else:
            concept['knowledge_type'] = kt

        # ── Validate complexity ─────────────────────────────────────────
        cx = concept.get('complexity', '')
        if isinstance(cx, str):
            cx = cx.lower().strip()
        else:
            cx = ''
        if cx not in VALID_COMPLEXITIES:
            new_val = _reclassify_field('complexity', VALID_COMPLEXITIES, concept, key, config)
            if new_val:
                concept['complexity'] = new_val
            else:
                logger.warning(f"Invalid complexity '{cx}' for '{concept.get('title', '?')}', using fallback")
                concept['complexity'] = COMPLEXITY_FALLBACK
        else:
            concept['complexity'] = cx

    return concepts


def _check_language(text_dir, config):
    """Check language of document text. Returns (is_allowed, detected_lang).

    Reads first 1000 chars from first page file and uses langdetect.
    Returns (True, lang) if language is allowed, (False, lang) if not.
    Falls back to (True, 'unknown') if detection fails (benefit of the doubt).
    """
    if not _HAS_LANGDETECT:
        return True, 'unknown'

    # Check if language filter is enabled in config
    pipeline_cfg = config.get('pipeline', {})
    if not pipeline_cfg.get('language_filter', True):
        return True, 'disabled'

    allowed = set(pipeline_cfg.get('allowed_languages', ['en']))

    # Read first page for detection
    page_files = sorted([f for f in os.listdir(text_dir)
                         if f.startswith('page_') and f.endswith('.txt')])
    if not page_files:
        return True, 'no_pages'

    try:
        with open(os.path.join(text_dir, page_files[0]), encoding='utf-8') as f:
            sample = f.read(1500)
        if len(sample.strip()) < 50:
            return True, 'too_short'
        lang = _detect_lang(sample)
        return (lang in allowed), lang
    except LangDetectException:
        return True, 'detection_failed'
    except Exception:
        return True, 'error'


def enrich_single(file_hash, db, config, key_rotator):
    doc = db.get_document(file_hash)
    if not doc:
        return False

    text_dir = resolve_text_dir(file_hash, config, db)
    concepts_dir = os.path.join(config['paths']['concepts'], file_hash)
    window_size = config['processing']['enrich_window_size']
    delay = config['processing']['rate_limit_delay']
    proc = config.get('processing', {})
    max_retries = proc.get('enrich_max_retries', proc.get('max_retries', 5))
    base_delay = proc.get('enrich_base_delay', 5.0)
    max_delay = proc.get('enrich_max_delay', 120.0)

    if not os.path.exists(text_dir):
        db.mark_failed(file_hash, f"Text directory not found: {text_dir}")
        return False

    # Language gate: skip non-English documents before burning Gemini quota
    lang_ok, detected_lang = _check_language(text_dir, config)
    if not lang_ok:
        logger.info(f"Skipping {file_hash[:12]}... detected language '{detected_lang}' "
                     f"(allowed: {config.get('pipeline', {}).get('allowed_languages', ['en'])})")
        db.mark_failed(file_hash, f"Language filter: detected '{detected_lang}', not in allowed list")
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
        failed_windows = []

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

            # Attempt enrichment with backoff — failures skip the window, not the doc
            try:
                key = key_rotator.next()
                concepts = _retry_with_backoff(
                    lambda k=key: enrich_window(window_text, k, config),
                    max_retries=max_retries,
                    base_delay=base_delay,
                    max_delay=max_delay,
                )
            except Exception as e:
                failed_windows.append((w_idx + 1, str(e)[:100]))
                logger.warning(f"  Window {w_idx+1}/{len(windows)} failed: {e}")
                continue  # skip this window, keep going

            if not isinstance(concepts, list):
                concepts = [concepts] if isinstance(concepts, dict) else []
            concepts = [c for c in concepts if isinstance(c, dict)]

            # Validate domain, knowledge_type, complexity — retry then fallback
            validation_key = key_rotator.next()
            concepts = validate_and_fix_concepts(concepts, validation_key, config)

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

        # Decide document status based on results
        meta = {
            'hash': file_hash,
            'total_windows': len(windows),
            'total_concepts': total_concepts,
            'failed_windows': len(failed_windows),
            'window_size': window_size,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        with open(os.path.join(concepts_dir, 'meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)

        if total_concepts > 0 or not failed_windows:
            # Some concepts extracted, or all windows were empty — mark enriched
            error_msg = None
            if total_concepts == 0 and doc.get('page_count', 0) >= 3:
                error_msg = (f"0 concepts from {doc.get('page_count', '?')} pages — "
                             f"likely image-only PDF, may need manual review")
                logger.warning(f"  {doc['filename']}: {error_msg}")
            elif failed_windows:
                wins = ', '.join(str(w) for w, _ in failed_windows[:10])
                error_msg = (f"Partial: {len(failed_windows)}/{len(windows)} "
                             f"windows failed (windows {wins})")
                logger.warning(f"  {doc['filename']}: {error_msg}")
            db.update_status(file_hash, 'enriched', concepts_extracted=total_concepts,
                             error_message=error_msg)
            fw_note = f", {len(failed_windows)} windows failed" if failed_windows else ""
            logger.info(f"Enriched {doc['filename']}: {total_concepts} concepts "
                        f"from {len(windows)} windows{fw_note}")
            return True
        else:
            # Every window failed — document truly failed
            first_err = failed_windows[0][1] if failed_windows else 'unknown'
            db.mark_failed(file_hash,
                           f"All {len(windows)} windows failed: {first_err}")
            logger.error(f"  {doc['filename']}: all {len(windows)} windows failed")
            return False

    except Exception as e:
        logger.error(f"Enrichment failed for {file_hash}: {e}\n{traceback.format_exc()}")
        db.mark_failed(file_hash, str(e))
        return False


def _recover_stale_enriching(db, max_hours=STALE_ENRICHING_HOURS):
    """Reset docs stuck in enriching back to extracted so they get retried.

    This handles the case where a previous enrichment run crashed mid-document.
    The enricher skips already-completed window files, so no work is lost.
    """
    import sqlite3
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT hash, filename FROM documents WHERE status = 'enriching'",
    ).fetchall()
    if not rows:
        return

    # Check extracted_at timestamp — if enriching started > max_hours ago, reset
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
    reset = []
    for row in rows:
        doc = db.get_document(row['hash'])
        extracted_at = doc.get('extracted_at', '')
        if not extracted_at:
            reset.append(row)
            continue
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(extracted_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours > max_hours:
                reset.append(row)
        except Exception:
            reset.append(row)

    for row in reset:
        conn.execute(
            "UPDATE documents SET status = 'extracted' WHERE hash = ?",
            (row['hash'],)
        )
        logger.warning(f"Recovered stale enriching doc: {row['filename']} ({row['hash'][:12]}...)")
    if reset:
        conn.commit()
        logger.info(f"Reset {len(reset)} stale enriching docs back to extracted")


def run_enrichment(workers=None, limit=None):
    config = get_config()
    db = StatusDB()
    workers = workers or config['processing']['enrich_workers']

    # Recover docs orphaned by previous crashed enrichment runs
    _recover_stale_enriching(db)

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
