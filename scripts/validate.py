#!/usr/bin/env python3
"""
RECON Pipeline Validator

Checks pipeline consistency: paths, DB state, file integrity, and service connectivity.
Validates TEI, Ollama, and Qdrant are reachable. Deep mode checks every document on disk.

Usage: python3 scripts/validate.py [--deep]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.utils import get_config, setup_logging
from lib.status import StatusDB

logger = setup_logging('recon.validate')


def run_validation(deep=False):
    config = get_config()
    db = StatusDB()

    issues = []
    warnings = []

    print("=== RECON Validation ===\n")

    # Check paths
    for name, path in config['paths'].items():
        if name == 'db':
            if not os.path.exists(path):
                issues.append(f"Database not found: {path}")
        else:
            if not os.path.exists(path):
                warnings.append(f"Directory missing: {name} = {path}")

    # Check library
    if not os.path.exists(config['library_root']):
        issues.append(f"Library root not found: {config['library_root']}")

    # Check Gemini keys
    keys = config.get('gemini_keys', [])
    if not keys:
        warnings.append("No Gemini API keys configured in .env")
    else:
        print(f"  Gemini keys: {len(keys)} configured")

    # DB status counts
    counts = db.get_status_counts()
    cat = counts.get('catalogue', {})
    doc = counts.get('documents', {})

    print(f"  Catalogue: {sum(cat.values())} entries")
    print(f"  Documents: {sum(doc.values())} entries")
    print(f"  Complete: {doc.get('complete', 0)}")
    print(f"  Failed: {doc.get('failed', 0)}")

    if deep:
        print("\n--- Deep Validation ---\n")

        # Check every document in pipeline has corresponding files
        all_docs = db.get_all_documents()
        text_dir = config['paths']['text']
        concepts_dir = config['paths']['concepts']

        for d in all_docs:
            h = d['hash']
            status = d['status']

            if status in ('extracted', 'enriched', 'complete'):
                doc_text_dir = os.path.join(text_dir, h)
                if not os.path.exists(doc_text_dir):
                    issues.append(f"[{h[:8]}] {d['filename']}: text dir missing but status={status}")
                elif deep:
                    pages = [f for f in os.listdir(doc_text_dir) if f.startswith('page_')]
                    if not pages:
                        issues.append(f"[{h[:8]}] {d['filename']}: no page files in text dir")

            if status in ('enriched', 'complete'):
                doc_concepts_dir = os.path.join(concepts_dir, h)
                if not os.path.exists(doc_concepts_dir):
                    issues.append(f"[{h[:8]}] {d['filename']}: concepts dir missing but status={status}")
                elif deep:
                    windows = [f for f in os.listdir(doc_concepts_dir) if f.startswith('window_')]
                    if not windows:
                        issues.append(f"[{h[:8]}] {d['filename']}: no window files in concepts dir")
                    else:
                        for wf in windows:
                            try:
                                with open(os.path.join(doc_concepts_dir, wf)) as f:
                                    data = json.load(f)
                                if not isinstance(data, list):
                                    issues.append(f"[{h[:8]}] {wf}: not a JSON array")
                            except json.JSONDecodeError:
                                issues.append(f"[{h[:8]}] {wf}: invalid JSON")

        # Check orphaned directories
        if os.path.exists(text_dir):
            doc_hashes = {d['hash'] for d in all_docs}
            for dirname in os.listdir(text_dir):
                if dirname not in doc_hashes:
                    warnings.append(f"Orphaned text dir: {dirname}")

        if os.path.exists(concepts_dir):
            for dirname in os.listdir(concepts_dir):
                if dirname not in doc_hashes:
                    warnings.append(f"Orphaned concepts dir: {dirname}")

        print(f"  Checked {len(all_docs)} documents")

    # Connectivity checks
    print("\n--- Connectivity ---\n")

    import requests as http_requests

    # Check TEI (primary embedding backend)
    try:
        tei_url = f"http://{config['embedding']['tei_host']}:{config['embedding']['tei_port']}/info"
        resp = http_requests.get(tei_url, timeout=10)
        if resp.status_code == 200:
            print(f"  TEI: OK (bge-m3 at {config['embedding']['tei_host']}:{config['embedding']['tei_port']})")
        else:
            issues.append(f"TEI: HTTP {resp.status_code}")
    except Exception as e:
        issues.append(f"TEI: unreachable ({e})")

    # Check Ollama (fallback)
    try:
        ollama_url = f"http://{config['embedding']['ollama_host']}:{config['embedding']['ollama_port']}/api/tags"
        resp = http_requests.get(ollama_url, timeout=10)
        if resp.status_code == 200:
            print(f"  Ollama: OK (fallback at {config['embedding']['ollama_host']}:{config['embedding']['ollama_port']})")
        else:
            warnings.append(f"Ollama: HTTP {resp.status_code}")
    except Exception as e:
        warnings.append(f"Ollama: unreachable ({e}) — fallback only, not critical")

    try:
        from qdrant_client import QdrantClient
        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=10
        )
        collections = [c.name for c in qdrant.get_collections().collections]
        target = config['vector_db']['collection']
        if target in collections:
            info = qdrant.get_collection(target)
            print(f"  Qdrant: OK ({target}: {info.points_count} points)")
        else:
            issues.append(f"Qdrant: collection {target} not found")
    except Exception as e:
        issues.append(f"Qdrant: unreachable ({e})")

    # Summary
    print("\n--- Summary ---\n")

    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠ {w}")

    if issues:
        print(f"\nIssues ({len(issues)}):")
        for i in issues:
            print(f"  ✗ {i}")
        print(f"\nValidation FAILED: {len(issues)} issue(s)")
    else:
        print("Validation PASSED")


if __name__ == '__main__':
    deep = '--deep' in sys.argv
    run_validation(deep=deep)
