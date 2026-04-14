"""
RECON Qdrant Rebuilder — patched for headless parallel execution

Deletes and recreates the Qdrant collection, then re-embeds ALL concept JSONs
from disk using parallel workers. Pass --confirm to skip interactive prompt.

Usage:
  python3 scripts/rebuild_qdrant.py --confirm [--workers 8]
"""

import json
import os
import sys
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests as http_requests
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

from lib.utils import get_config, concept_id, setup_logging
from lib.status import StatusDB

logger = setup_logging('recon.rebuild')


def embed_content(config, content):
    try:
        tei_url = f"http://{config['embedding']['tei_host']}:{config['embedding']['tei_port']}/embed"
        resp = http_requests.post(tei_url, json={"inputs": content}, timeout=120)
        resp.raise_for_status()
        return resp.json()[0]
    except Exception as tei_err:
        logger.debug(f"TEI failed, trying Ollama: {tei_err}")

    ollama_url = f"http://{config['embedding']['ollama_host']}:{config['embedding']['ollama_port']}/api/embed"
    resp = http_requests.post(ollama_url, json={
        "model": config['embedding']['model'],
        "input": content
    }, timeout=120)
    resp.raise_for_status()
    return resp.json()['embeddings'][0]


def process_doc(doc_hash, config, db, qdrant, collection):
    """Embed and upsert all concepts for a single document. Returns (inserted, failed)."""
    doc_dir = os.path.join(config['paths']['concepts'], doc_hash)
    doc = db.get_document(doc_hash)
    filename = doc['filename'] if doc else doc_hash[:8]

    window_files = sorted([
        f for f in os.listdir(doc_dir)
        if f.startswith('window_') and f.endswith('.json')
    ])

    all_concepts = []
    for wf in window_files:
        path = os.path.join(doc_dir, wf)
        try:
            with open(path, encoding='utf-8') as f:
                concepts = json.load(f)
            if isinstance(concepts, list):
                all_concepts.extend(concepts)
        except Exception as e:
            logger.warning(f"Skipping corrupted window {wf} in {doc_hash}: {e}")

    if not all_concepts:
        return 0, 0

    is_web = doc.get('path', '').startswith(('http://', 'https://')) if doc else False

    # Check meta.json for explicit source_type (e.g. 'transcript')
    source_type = 'web' if is_web else 'document'
    text_dir = os.path.join(config['paths']['text'], doc_hash)
    meta_path = os.path.join(text_dir, 'meta.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as mf:
                meta = json.load(mf)
            if meta.get('source_type'):
                source_type = meta['source_type']
        except Exception:
            pass

    points = []
    failed = 0
    batch_size = config['processing']['embed_batch_size']

    for idx, concept in enumerate(all_concepts):
        content = concept.get('content', '')
        if not content or len(content.strip()) < 10:
            continue
        try:
            vector = embed_content(config, content)
        except Exception as e:
            logger.warning(f"Embedding failed {doc_hash}:{idx}: {e}")
            failed += 1
            continue

        start_page = concept.get('_start_page', 0)
        point_id = concept_id(doc_hash, start_page, idx)

        payload = {
            'doc_hash': doc_hash,
            'filename': filename,
            'book_title': doc.get('book_title', '') if doc else '',
            'book_author': doc.get('book_author', '') if doc else '',
            'source_type': source_type,
            'verification_status': 'unverified',
            'credibility_score': 0.7,
            'language': 'en',
        }
        for field in ['content', 'summary', 'title', 'domain', 'subdomain',
                      'keywords', 'skill_level', 'key_facts', 'scenario_applicable',
                      'cross_domain_tags', 'chapter', 'page_ref', 'notes',
                      '_window', '_start_page']:
            if field in concept:
                payload[field] = concept[field]

        points.append(PointStruct(id=point_id, vector=vector, payload=payload))

        if len(points) >= batch_size:
            qdrant.upsert(collection_name=collection, points=points)
            points = []

    if points:
        qdrant.upsert(collection_name=collection, points=points)

    inserted = len(all_concepts) - failed
    if doc:
        db.update_status(doc_hash, 'complete', vectors_inserted=inserted)

    return inserted, failed


def run_rebuild(workers=8):
    config = get_config()
    db = StatusDB()

    qdrant = QdrantClient(
        host=config['vector_db']['host'],
        port=config['vector_db']['port'],
        timeout=60
    )
    collection = config['vector_db']['collection']

    # Delete and recreate
    try:
        qdrant.delete_collection(collection)
        logger.info(f"Deleted collection: {collection}")
    except Exception:
        pass

    qdrant.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(
            size=config['embedding']['dimensions'],
            distance=Distance.COSINE
        )
    )
    logger.info(f"Created collection: {collection} ({config['embedding']['dimensions']}d, Cosine)")

    concepts_root = config['paths']['concepts']
    doc_dirs = sorted([
        d for d in os.listdir(concepts_root)
        if os.path.isdir(os.path.join(concepts_root, d))
    ])
    logger.info(f"Found {len(doc_dirs)} document concept directories | {workers} workers")

    total_inserted = 0
    total_failed = 0
    done = 0
    lock = threading.Lock()
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(process_doc, h, config, StatusDB(), qdrant, collection): h
            for h in doc_dirs
        }
        for future in as_completed(futures):
            doc_hash = futures[future]
            try:
                inserted, failed = future.result()
            except Exception as e:
                logger.error(f"Worker error {doc_hash}: {e}")
                inserted, failed = 0, 0

            with lock:
                total_inserted += inserted
                total_failed += failed
                done += 1
                if done % 500 == 0:
                    elapsed = time.time() - start
                    rate = total_inserted / elapsed if elapsed > 0 else 0
                    remaining = (len(doc_dirs) - done) / (done / elapsed) if elapsed > 0 else 0
                    logger.info(
                        f"  [{done}/{len(doc_dirs)}] "
                        f"{total_inserted:,} vectors | "
                        f"{rate:.0f}/sec | "
                        f"ETA {remaining/60:.0f}min"
                    )

    elapsed = time.time() - start
    logger.info(f"\nRebuild complete in {elapsed/60:.1f} min: "
                f"{total_inserted:,} inserted, {total_failed:,} failed")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm', action='store_true', help='Skip interactive prompt')
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()

    if not args.confirm:
        print("WARNING: This will DELETE and RECREATE the Qdrant collection.")
        confirm = input("Type 'REBUILD' to proceed: ")
        if confirm != 'REBUILD':
            print("Aborted.")
            sys.exit(0)

    run_rebuild(workers=args.workers)
