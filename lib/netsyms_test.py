#!/usr/bin/env python3
"""Tests for Netsyms address database module."""

import sys
import os

# Ensure the lib directory is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import netsyms


def test_lookup_by_street_lowercase():
    results = netsyms.lookup_by_street("214", "North St", city="Filer", state="ID")
    assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"
    r = results[0]
    assert abs(r['lat'] - 42.5736) < 0.01, f"Lat mismatch: {r['lat']}"
    assert abs(r['lon'] - (-114.6066)) < 0.01, f"Lon mismatch: {r['lon']}"
    print("  PASS: lookup_by_street (lowercase)")


def test_lookup_by_street_uppercase():
    results = netsyms.lookup_by_street("214", "NORTH ST", city="FILER", state="ID")
    assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"
    r = results[0]
    assert abs(r['lat'] - 42.5736) < 0.01, f"Lat mismatch: {r['lat']}"
    print("  PASS: lookup_by_street (uppercase)")


def test_lookup_nonexistent():
    results = netsyms.lookup_by_street("999999", "Nonexistent Rd",
                                       city="Filer", state="ID")
    assert results == [], f"Expected empty list, got {len(results)} results"
    print("  PASS: lookup_by_street (nonexistent)")


def test_free_text_with_commas():
    results = netsyms.lookup_free_text("214 North St, Filer, ID")
    assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"
    r = results[0]
    assert r['city'] == 'FILER', f"City mismatch: {r['city']}"
    assert r['state'] == 'ID', f"State mismatch: {r['state']}"
    print("  PASS: lookup_free_text (commas)")


def test_free_text_no_commas():
    results = netsyms.lookup_free_text("214 North St Filer ID")
    assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"
    r = results[0]
    assert r['state'] == 'ID', f"State mismatch: {r['state']}"
    print("  PASS: lookup_free_text (no commas)")


def test_lookup_by_zipcode():
    results = netsyms.lookup_by_zipcode("83328", limit=5)
    assert len(results) == 5, f"Expected 5 results, got {len(results)}"
    for r in results:
        assert r['zipcode'] == '83328', f"Zipcode mismatch: {r['zipcode']}"
    print("  PASS: lookup_by_zipcode")


def test_health():
    h = netsyms.health()
    assert h['ok'] is True, f"Health not OK: {h}"
    assert h['row_count'] >= 159_000_000, f"Row count too low: {h['row_count']}"
    assert 'US' in h['indexed_countries'], f"US not in countries: {h['indexed_countries']}"
    assert 'CA' in h['indexed_countries'], f"CA not in countries: {h['indexed_countries']}"
    print("  PASS: health")


if __name__ == '__main__':
    print("Running Netsyms tests...")
    test_lookup_by_street_lowercase()
    test_lookup_by_street_uppercase()
    test_lookup_nonexistent()
    test_free_text_with_commas()
    test_free_text_no_commas()
    test_lookup_by_zipcode()
    test_health()
    print("All tests passed.")
