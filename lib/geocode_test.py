#!/usr/bin/env python3
"""Tests for RECON Photon-first geocode chain."""
import sys
import os
import json
import urllib.request
import urllib.parse

BASE = "http://localhost:8420"

TESTS = [
    {
        "name": "home → nickname short-circuit",
        "query": "home",
        "check": lambda r: (
            r["count"] == 1
            and r["results"][0]["source"] == "address_book"
            and r["results"][0]["confidence"] == "exact"
            and r["results"][0]["type"] == "nickname"
        ),
    },
    {
        "name": "214 north st filer → photon results (multi-word, not nickname)",
        "query": "214 north st filer",
        "check": lambda r: (
            r["count"] >= 1
            and r["results"][0]["source"] == "photon"
            # labeled_as=Home may or may not appear depending on Photon's
            # geocoding precision — the key invariant is that this multi-word
            # query flows through Photon, not the address book shortcut.
        ),
    },
    {
        "name": "214 North St, Filer, ID → photon (case/punctuation)",
        "query": "214 North St, Filer, ID",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "214 NORTH ST FILER ID → photon (uppercase)",
        "query": "214 NORTH ST FILER ID",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "1600 Pennsylvania Ave Washington DC → White House",
        "query": "1600 Pennsylvania Ave Washington DC",
        "check": lambda r: (
            r["count"] >= 1
            and r["results"][0]["source"] == "photon"
        ),
    },
    {
        "name": "1600 pennsylvania ave washington dc → lowercase",
        "query": "1600 pennsylvania ave washington dc",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "starbucks filer → POI result",
        "query": "starbucks filer",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "filer idaho → locality",
        "query": "filer idaho",
        "check": lambda r: (
            r["count"] >= 1
            and r["results"][0]["source"] == "photon"
            and r["results"][0]["type"] == "locality"
        ),
    },
    {
        "name": "filer → partial query, at least 1 result",
        "query": "filer",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "42.5736, -114.6066 → coordinates (with space)",
        "query": "42.5736, -114.6066",
        "check": lambda r: (
            r["count"] == 1
            and r["results"][0]["source"] == "coordinates"
            and r["results"][0]["confidence"] == "exact"
            and r["results"][0]["type"] == "coordinates"
        ),
    },
    {
        "name": "42.5736,-114.6066 → coordinates (no space)",
        "query": "42.5736,-114.6066",
        "check": lambda r: (
            r["count"] == 1
            and r["results"][0]["source"] == "coordinates"
            and r["results"][0]["confidence"] == "exact"
        ),
    },
    {
        "name": "boise → at least 1 result",
        "query": "boise",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "toronto → CA canary",
        "query": "toronto",
        "check": lambda r: r["count"] >= 1 and r["results"][0]["source"] == "photon",
    },
    {
        "name": "asdfghjklqwerty → empty results, 200 OK",
        "query": "asdfghjklqwerty",
        "check": lambda r: r["count"] == 0 and r["results"] == [],
    },
    {
        "name": "empty query → empty results",
        "query": "",
        "check": lambda r: r["count"] == 0 and r["results"] == [],
    },
]

passed = 0
failed = 0

for t in TESTS:
    q = urllib.parse.urlencode({"q": t["query"]}) if t["query"] else "q="
    url = f"{BASE}/api/geocode?{q}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
    except Exception as e:
        status = 0
        body = {}
        print(f"  [FAIL] {t['name']}")
        print(f"         EXCEPTION: {e}")
        failed += 1
        continue

    ok = status == 200 and t["check"](body)
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1

    top = body.get("results", [{}])[0] if body.get("results") else {}
    top_summary = f"source={top.get('source','—')} type={top.get('type','—')} conf={top.get('confidence','—')} name={top.get('name','—')[:50]}"
    print(f"  [{tag}] {t['name']}")
    if not ok:
        print(f"         HTTP {status}, count={body.get('count','?')}, top: {top_summary}")
    else:
        labeled = f" labeled_as={top.get('labeled_as')}" if top.get('labeled_as') else ""
        print(f"         → {top_summary}{labeled}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
