#!/usr/bin/env python3
"""Tests for RECON address book module."""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import address_book

TESTS = [
    ("lookup('home') → exact",
     lambda: address_book.lookup("home"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('Home') → exact (case-insensitive)",
     lambda: address_book.lookup("Home"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('214 north st') → exact via alias",
     lambda: address_book.lookup("214 north st"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('214 North Street') → exact via alias",
     lambda: address_book.lookup("214 North Street"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('nonexistent place') → None",
     lambda: address_book.lookup("nonexistent place"),
     lambda r: r is None),

    ("list_all() → 1 entry",
     lambda: address_book.list_all(),
     lambda r: isinstance(r, list) and len(r) == 1 and r[0]['id'] == 'home'),
]

passed = 0
failed = 0
for name, fn, check in TESTS:
    try:
        result = fn()
        ok = check(result)
    except Exception as e:
        ok = False
        result = f"EXCEPTION: {e}"

    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  [{status}] {name}")
    if not ok:
        print(f"          got: {result}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
