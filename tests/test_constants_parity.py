#!/usr/bin/env python3
"""
Parity test: verify RECON domain taxonomy matches PeerTube categories.

Tests:
1. DOMAIN_CATEGORY_MAP keys match VALID_DOMAINS exactly
2. PeerTube API returns all 18 RECON categories (IDs 100-117) with correct labels

Usage:
    cd /opt/recon && source venv/bin/activate
    python3 tests/test_constants_parity.py
"""
import json
import sys
import requests

# Add parent dir to path for imports
sys.path.insert(0, '/opt/recon')
from lib.recon_domains import DOMAIN_CATEGORY_MAP, VALID_DOMAINS, CATEGORY_DOMAIN_MAP


def test_local_parity():
    """Verify DOMAIN_CATEGORY_MAP keys match VALID_DOMAINS."""
    map_keys = set(DOMAIN_CATEGORY_MAP.keys())
    assert map_keys == VALID_DOMAINS, (
        f"Mismatch: in map but not VALID_DOMAINS: {map_keys - VALID_DOMAINS}, "
        f"in VALID_DOMAINS but not map: {VALID_DOMAINS - map_keys}"
    )
    assert len(CATEGORY_DOMAIN_MAP) == len(DOMAIN_CATEGORY_MAP), "Reverse map size mismatch"
    print(f"[OK] Local parity: {len(VALID_DOMAINS)} domains, map keys match VALID_DOMAINS")


def test_peertube_categories():
    """Verify PeerTube API returns all 18 RECON categories."""
    url = "http://192.168.1.170:9000/api/v1/videos/categories"
    headers = {"Host": "stream.echo6.co"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[SKIP] PeerTube API unreachable: {e}")
        return

    categories = resp.json()  # dict of {id_str: label}

    missing = []
    wrong_label = []
    for domain, cat_id in DOMAIN_CATEGORY_MAP.items():
        cat_str = str(cat_id)
        if cat_str not in categories:
            missing.append((cat_id, domain))
        elif categories[cat_str] != domain:
            wrong_label.append((cat_id, domain, categories[cat_str]))

    if missing:
        print(f"[FAIL] Missing categories in PeerTube: {missing}")
        print("  Deploy peertube-plugin-recon-domains to CT 110 first")
        sys.exit(1)

    if wrong_label:
        print(f"[FAIL] Wrong labels in PeerTube: {wrong_label}")
        sys.exit(1)

    print(f"[OK] PeerTube parity: all {len(DOMAIN_CATEGORY_MAP)} categories present with correct labels")


if __name__ == '__main__':
    test_local_parity()
    test_peertube_categories()
    print("\nAll parity tests passed.")
