"""
RECON Intel Ingester

ARGUS intelligence feed intake. Embeds intel JSON and inserts into Qdrant
with source_type='intel_feed'.

Dependencies: requests, qdrant-client
Config: embedding, vector_db
"""
import json
import os
import time
import traceback

import requests as http_requests
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from .utils import get_config, setup_logging
from .status import StatusDB

logger = setup_logging('recon.ingester')


def ingest_intel(intel_data, config=None):
    if config is None:
        config = get_config()

    db = StatusDB()

    required = ['source', 'category', 'content']
    for field in required:
        if field not in intel_data:
            logger.error(f"Missing required field: {field}")
            return None

    try:
        conn = db._get_conn()
        cursor = conn.execute(
            """INSERT INTO intel (source, timestamp, region, category, content,
               summary, key_facts, credibility_score, verification_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                intel_data.get('source', 'unknown'),
                intel_data.get('timestamp', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())),
                intel_data.get('region', 'unknown'),
                intel_data['category'],
                intel_data['content'],
                intel_data.get('summary', ''),
                json.dumps(intel_data.get('key_facts', [])),
                intel_data.get('credibility_score', 0.5),
                intel_data.get('verification_status', 'unverified'),
            )
        )
        intel_id = cursor.lastrowid
        conn.commit()

        url = f"http://{config['embedding']['host']}:{config['embedding']['port']}/api/embed"
        resp = http_requests.post(url, json={
            "model": config['embedding']['model'],
            "input": intel_data['content']
        }, timeout=120)
        resp.raise_for_status()
        vector = resp.json()['embeddings'][0]

        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=60
        )

        point_id = intel_id + 2**60

        payload = {
            'source_type': 'intel_feed',
            'intel_id': intel_id,
            'source': intel_data.get('source', 'unknown'),
            'region': intel_data.get('region', 'unknown'),
            'category': intel_data['category'],
            'content': intel_data['content'],
            'summary': intel_data.get('summary', ''),
            'key_facts': intel_data.get('key_facts', []),
            'credibility_score': intel_data.get('credibility_score', 0.5),
            'verification_status': intel_data.get('verification_status', 'unverified'),
            'timestamp': intel_data.get('timestamp', ''),
            'language': 'en',
        }

        qdrant.upsert(
            collection_name=config['vector_db']['collection'],
            points=[PointStruct(id=point_id, vector=vector, payload=payload)]
        )

        conn.execute("UPDATE intel SET vector_id = ? WHERE id = ?", (point_id, intel_id))
        conn.commit()

        logger.info(f"Ingested intel #{intel_id} from {intel_data.get('source', 'unknown')}")
        return intel_id

    except Exception as e:
        logger.error(f"Intel ingestion failed: {e}\n{traceback.format_exc()}")
        return None


def ingest_file(filepath, config=None):
    if config is None:
        config = get_config()

    try:
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, list):
            results = []
            for item in data:
                result = ingest_intel(item, config)
                results.append(result)
            success = sum(1 for r in results if r is not None)
            logger.info(f"Ingested {success}/{len(data)} items from {filepath}")
            return results
        else:
            return [ingest_intel(data, config)]

    except Exception as e:
        logger.error(f"Failed to ingest file {filepath}: {e}")
        return []


def run_ingestion(directory=None):
    config = get_config()
    intel_dir = directory or config['paths']['intel']

    if not os.path.exists(intel_dir):
        logger.info(f"Intel directory does not exist: {intel_dir}")
        return 0

    json_files = sorted([
        f for f in os.listdir(intel_dir)
        if f.endswith('.json') and not f.startswith('.')
    ])

    if not json_files:
        logger.info("No intel files to ingest")
        return 0

    total = 0
    for jf in json_files:
        filepath = os.path.join(intel_dir, jf)
        results = ingest_file(filepath, config)
        ingested = sum(1 for r in results if r is not None)
        total += ingested

        if ingested > 0:
            done_dir = os.path.join(intel_dir, 'processed')
            os.makedirs(done_dir, exist_ok=True)
            os.rename(filepath, os.path.join(done_dir, jf))

    logger.info(f"Intel ingestion complete: {total} items ingested")
    return total
