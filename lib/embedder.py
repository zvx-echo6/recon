"""
RECON Embedder

Concepts to vectors via TEI (primary, 1024-dim bge-m3, ~1,711 emb/sec)
or Ollama (fallback, ~8 emb/sec). Inserts into Qdrant on cortex:6333.

Supports hybrid dense+sparse vectors when sparse_embedding service is configured.

Dependencies: requests, qdrant-client
Config: embedding, vector_db, processing.embed_workers
"""
import json
import re
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as http_requests
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, SparseVector

from .utils import get_config, concept_id, generate_download_url, setup_logging
from .status import StatusDB
from .utils import resolve_text_dir

logger = setup_logging('recon.embedder')

# ── Classification allowlists ───────────────────────────────────────────────
from .recon_domains import VALID_DOMAINS
VALID_KNOWLEDGE_TYPES = {'foundational', 'procedural', 'operational'}
VALID_COMPLEXITIES = {'basic', 'intermediate', 'advanced'}

DOMAIN_FALLBACK = 'Foundational Skills'
KNOWLEDGE_TYPE_FALLBACK = 'foundational'
COMPLEXITY_FALLBACK = 'basic'


def _validate_classification(payload):
    """Validate domain, knowledge_type, complexity before upsert.

    Logs WARNING and applies safe fallback for any invalid values.
    Returns the payload (modified in place if needed).
    """
    title = payload.get('title', payload.get('filename', '?'))

    # ── domain ──────────────────────────────────────────────────────────
    domain = payload.get('domain')
    if isinstance(domain, list):
        valid = [d for d in domain if d in VALID_DOMAINS]
        if valid:
            payload['domain'] = valid[0]
        else:
            logger.warning(f"Invalid domain {domain} for '{title}', fallback → {DOMAIN_FALLBACK}")
            payload['domain'] = DOMAIN_FALLBACK
    elif isinstance(domain, str):
        if domain not in VALID_DOMAINS:
            logger.warning(f"Invalid domain '{domain}' for '{title}', fallback → {DOMAIN_FALLBACK}")
            payload['domain'] = DOMAIN_FALLBACK
    else:
        payload['domain'] = DOMAIN_FALLBACK

    # ── knowledge_type ──────────────────────────────────────────────────
    kt = payload.get('knowledge_type', '')
    if isinstance(kt, str):
        kt = kt.lower().strip()
    else:
        kt = ''
    if kt not in VALID_KNOWLEDGE_TYPES:
        logger.warning(f"Invalid knowledge_type '{kt}' for '{title}', fallback → {KNOWLEDGE_TYPE_FALLBACK}")
        payload['knowledge_type'] = KNOWLEDGE_TYPE_FALLBACK
    else:
        payload['knowledge_type'] = kt

    # ── complexity ──────────────────────────────────────────────────────
    cx = payload.get('complexity', '')
    if isinstance(cx, str):
        cx = cx.lower().strip()
    else:
        cx = ''
    if cx not in VALID_COMPLEXITIES:
        logger.warning(f"Invalid complexity '{cx}' for '{title}', fallback → {COMPLEXITY_FALLBACK}")
        payload['complexity'] = COMPLEXITY_FALLBACK
    else:
        payload['complexity'] = cx

    return payload


def get_embedding_single(text, config):
    """Get a single embedding — uses TEI or Ollama depending on config."""
    backend = config['embedding'].get('backend', 'ollama')

    if backend == 'tei':
        url = f"http://{config['embedding']['tei_host']}:{config['embedding']['tei_port']}/embed"
        resp = http_requests.post(url, json={"inputs": text}, timeout=120)
        resp.raise_for_status()
        return resp.json()[0]
    else:
        url = f"http://{config['embedding']['ollama_host']}:{config['embedding']['ollama_port']}/api/embed"
        resp = http_requests.post(url, json={
            "model": config['embedding']['model'],
            "input": text
        }, timeout=120)
        resp.raise_for_status()
        return resp.json()['embeddings'][0]


def get_embeddings_batch(texts, config):
    """Get embeddings for a batch of texts via TEI. Falls back to sequential on error."""
    url = f"http://{config['embedding']['tei_host']}:{config['embedding']['tei_port']}/embed"

    try:
        resp = http_requests.post(url, json={"inputs": texts}, timeout=300)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        if len(texts) <= 1:
            raise
        # Split batch in half and retry each half
        mid = len(texts) // 2
        logger.warning(f"  Batch of {len(texts)} failed ({e}), splitting in half")
        left = get_embeddings_batch(texts[:mid], config)
        right = get_embeddings_batch(texts[mid:], config)
        return left + right


def get_sparse_embeddings_batch(texts, config):
    """Get sparse embeddings from the sparse embedding service on cortex.

    Returns a list of dicts with 'indices' and 'values' keys, or None on failure.
    """
    sparse_cfg = config.get('sparse_embedding')
    if not sparse_cfg or not sparse_cfg.get('enabled', False):
        return None

    url = f"http://{sparse_cfg['host']}:{sparse_cfg['port']}/embed_sparse"

    try:
        resp = http_requests.post(url, json={"inputs": texts}, timeout=300)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"  Sparse embedding failed for batch of {len(texts)}: {e}")
        return None


def _validate_content(content):
    """Validate and normalize concept content for embedding. Returns clean string or None."""
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()
    if len(content) < 10:
        return None
    # Truncate to 8192 chars (Ollama/TEI input limit)
    if len(content) > 8192:
        content = content[:8192]
    return content


def _build_payload(doc, concept, idx, source, download_url, source_type, page_timestamps):
    """Build and validate payload for a single concept point."""
    start_page = concept.get('_start_page', 0)

    payload = {
        'doc_hash': doc.get('hash', ''),
        'filename': doc['filename'],
        'book_title': doc.get('book_title', ''),
        'book_author': doc.get('book_author', ''),
        'source': source,
        'download_url': download_url,
        'source_type': source_type,
        'verification_status': 'unverified',
        'credibility_score': 0.7,
        'language': 'en',
    }

    for field in ['content', 'summary', 'title', 'domain', 'subdomain',
                  'keywords', 'knowledge_type', 'complexity',
                  'key_facts', 'scenario_applicable',
                  'cross_domain_tags', 'chapter', 'page_ref', 'notes',
                  '_window', '_start_page']:
        if field in concept:
            payload[field] = concept[field]

    # Add video timestamp for transcript sources
    if source_type == 'transcript' and page_timestamps:
        page_key = f"page_{start_page:04d}"
        if page_key in page_timestamps:
            payload['video_timestamp'] = page_timestamps[page_key]

    # Validate classification fields before returning
    payload = _validate_classification(payload)

    return payload


def _build_point(point_id, dense_vector, sparse_vec, payload, config):
    """Build a PointStruct with dense vector and optional sparse vector."""
    sparse_cfg = config.get('sparse_embedding')
    if sparse_cfg and sparse_cfg.get('enabled', False) and sparse_vec:
        vector = {
            "": dense_vector,
            "bge-m3-sparse": SparseVector(
                indices=sparse_vec['indices'],
                values=sparse_vec['values'],
            ),
        }
    else:
        vector = {"": dense_vector}

    return PointStruct(id=point_id, vector=vector, payload=payload)


def embed_single(file_hash, db, config):
    doc = db.get_document(file_hash)
    if not doc:
        return False

    concepts_dir = os.path.join(config['paths']['concepts'], file_hash)
    if not os.path.exists(concepts_dir):
        db.mark_failed(file_hash, f"Concepts directory not found: {concepts_dir}")
        return False

    db.update_status(file_hash, 'embedding')

    try:
        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=60
        )
        collection = config['vector_db']['collection']
        qdrant_batch_size = config['processing']['embed_batch_size']
        embed_batch_size = config['embedding'].get('batch_size', 128)
        backend = config['embedding'].get('backend', 'ollama')

        window_files = sorted([
            f for f in os.listdir(concepts_dir)
            if f.startswith('window_') and f.endswith('.json')
        ])

        if not window_files:
            db.mark_failed(file_hash, "No window files found")
            return False

        all_concepts = []
        for wf in window_files:
            with open(os.path.join(concepts_dir, wf), encoding='utf-8') as f:
                concepts = json.load(f)
            if isinstance(concepts, list):
                all_concepts.extend([c for c in concepts if isinstance(c, dict)])

        if not all_concepts:
            db.update_status(file_hash, 'complete', vectors_inserted=0)
            # Tag stream docs with no concepts for reprocessing
            _cat = db._get_conn().execute(
                "SELECT source FROM catalogue WHERE hash = ?", (file_hash,)
            ).fetchone()
            if _cat and dict(_cat)['source'] == 'stream.echo6.co':
                db.set_domain_assignment(file_hash, None, 'needs_reprocess')
            logger.info(f"No concepts to embed for {doc['filename']}")
            return True

        # Look up source and path from catalogue once per doc
        cat_conn = db._get_conn()
        cat_row = cat_conn.execute(
            "SELECT source, path FROM catalogue WHERE hash = ?", (file_hash,)
        ).fetchone()
        source = dict(cat_row)['source'] if cat_row else ''
        catalogue_path = dict(cat_row)['path'] if cat_row else ''

        download_url = ''
        is_web = doc.get('path', '').startswith(('http://', 'https://'))
        source_type = 'web' if is_web else 'document'

        # Check meta.json for explicit source_type (e.g. 'transcript')
        text_dir = resolve_text_dir(file_hash, config, db)
        meta_path = os.path.join(text_dir, 'meta.json')
        page_timestamps = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as mf:
                    meta = json.load(mf)
                if meta.get('source_type'):
                    source_type = meta['source_type']
                if not download_url and meta.get('url'):
                    download_url = meta['url']
                if meta.get('page_timestamps'):
                    page_timestamps = meta['page_timestamps']
            except Exception:
                pass
        # For ZIM articles, build wiki.echo6.co URL from meta.json
        if source_type == 'zim' and meta.get('article_path'):
            from urllib.parse import quote as url_quote
            zim_name = meta.get('zim_name', '')
            if not zim_name:
                # Derive from zim_file: strip only .zim extension, keep full name
                zf = meta.get('zim_file', '')
                zim_name = zf.removesuffix('.zim')
            article_path = url_quote(meta['article_path'], safe='/:@!$&()*+,;=-._~')
            download_url = f'https://wiki.echo6.co/content/{zim_name}/{article_path}'
        elif doc.get('path'):
            download_url = generate_download_url(
                doc['path'], config.get('library_root', '/mnt/library')
            )

        # Build list of valid concepts with their indices
        valid = []
        skipped = 0
        for idx, concept in enumerate(all_concepts):
            content = _validate_content(concept.get('content', ''))
            if content is None:
                skipped += 1
                continue
            valid.append((idx, concept, content))

        if skipped > 0:
            logger.info(f"  Skipped {skipped} concepts with invalid/empty content")

        if not valid:
            db.update_status(file_hash, 'complete', vectors_inserted=0)
            if source == 'stream.echo6.co':
                db.set_domain_assignment(file_hash, None, 'needs_reprocess')
            logger.info(f"No valid concepts to embed for {doc['filename']}")
            return True

        points = []
        embedded_count = 0

        if backend == 'tei':
            # TEI: batch embedding
            for batch_start in range(0, len(valid), embed_batch_size):
                batch = valid[batch_start:batch_start + embed_batch_size]
                texts = [content for _, _, content in batch]

                try:
                    vectors = get_embeddings_batch(texts, config)
                except Exception as e:
                    logger.error(f"  Batch embedding failed at offset {batch_start}: {e}")
                    # Skip entire batch on unrecoverable error
                    continue

                # Get sparse embeddings for the same batch
                sparse_results = get_sparse_embeddings_batch(texts, config)

                for i, ((idx, concept, content), vector) in enumerate(zip(batch, vectors)):
                    start_page = concept.get('_start_page', 0)
                    point_id = concept_id(file_hash, start_page, idx)

                    payload = _build_payload(
                        doc, concept, idx, source, download_url,
                        source_type, page_timestamps
                    )

                    sparse_vec = sparse_results[i] if sparse_results and i < len(sparse_results) else None
                    points.append(_build_point(point_id, vector, sparse_vec, payload, config))
                    embedded_count += 1

                    if len(points) >= qdrant_batch_size:
                        qdrant.upsert(collection_name=collection, points=points)
                        logger.debug(f"  Upserted batch of {len(points)} points")
                        points = []

        else:
            # Ollama: one-at-a-time with retry
            for idx, concept, content in valid:
                try:
                    vector = get_embedding_single(content, config)
                except Exception as e:
                    logger.warning(f"  Embedding failed for concept {idx}: {e}")
                    time.sleep(2)
                    try:
                        vector = get_embedding_single(content, config)
                    except Exception as e2:
                        logger.error(f"  Embedding retry failed for concept {idx}: {e2}")
                        continue

                # Get sparse embedding for single text
                sparse_results = get_sparse_embeddings_batch([content], config)
                sparse_vec = sparse_results[0] if sparse_results else None

                start_page = concept.get('_start_page', 0)
                point_id = concept_id(file_hash, start_page, idx)

                payload = _build_payload(
                    doc, concept, idx, source, download_url,
                    source_type, page_timestamps
                )

                points.append(_build_point(point_id, vector, sparse_vec, payload, config))
                embedded_count += 1

                if len(points) >= qdrant_batch_size:
                    qdrant.upsert(collection_name=collection, points=points)
                    logger.debug(f"  Upserted batch of {len(points)} points")
                    points = []

        if points:
            qdrant.upsert(collection_name=collection, points=points)
            logger.debug(f"  Upserted final batch of {len(points)} points")

        db.update_status(file_hash, 'complete', vectors_inserted=embedded_count)
        logger.info(f"Embedded {doc['filename']}: {embedded_count} vectors ({skipped} skipped)")

        # Post-embed hook: assign domain for PeerTube videos
        if source == 'stream.echo6.co':
            try:
                from .domain_assigner import compute_assignment
                from .peertube_writer import push_category, extract_uuid
                from .recon_domains import DOMAIN_CATEGORY_MAP
                domain, status = compute_assignment(file_hash, db, config)
                db.set_domain_assignment(file_hash, domain, status)
                if domain and status == 'assigned':
                    cat_id = DOMAIN_CATEGORY_MAP[domain]
                    uuid = extract_uuid(catalogue_path)
                    if uuid:
                        pushed, _token = push_category(uuid, cat_id, config)
                        if pushed:
                            db.set_peertube_pushed(file_hash)
                            logger.info(f"  Domain assigned: {domain} (category {cat_id}) → PeerTube")
                        else:
                            logger.warning(f"  Domain assigned ({domain}) but PeerTube push failed for {file_hash[:12]}, will retry via --push-pending")
            except Exception as e:
                logger.warning(f"Domain assignment failed for {file_hash}: {e}")

        return True

    except Exception as e:
        logger.error(f"Embedding failed for {file_hash}: {e}\n{traceback.format_exc()}")
        db.mark_failed(file_hash, str(e))
        return False


def run_embedding(workers=None, limit=None):
    config = get_config()
    db = StatusDB()
    workers = workers or config['processing']['embed_workers']

    enriched = db.get_by_status('enriched', limit=limit)
    if not enriched:
        logger.info("No enriched documents to embed")
        return 0

    backend = config['embedding'].get('backend', 'ollama')
    sparse_cfg = config.get('sparse_embedding')
    sparse_status = "enabled" if (sparse_cfg and sparse_cfg.get('enabled')) else "disabled"
    logger.info(f"Embedding {len(enriched)} documents with {workers} workers (backend: {backend}, sparse: {sparse_status})")
    success = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(embed_single, doc['hash'], StatusDB(), config): doc
            for doc in enriched
        }
        for future in as_completed(futures):
            doc = futures[future]
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker error for {doc['hash']}: {e}")

    logger.info(f"Embedding complete: {success}/{len(enriched)} succeeded")
    return success
