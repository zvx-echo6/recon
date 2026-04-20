#!/usr/bin/env python3
"""Tests for RECON address book module."""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import address_book

TESTS = [
    # ── Existing tests ──
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

    # ── New prefix+boundary tests ──
    ("lookup('214 north st filer') → exact (query starts with alias)",
     lambda: address_book.lookup("214 north st filer"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('214 North St Filer ID') → exact (case + trailing state)",
     lambda: address_book.lookup("214 North St Filer ID"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('214 north st, filer, id') → exact (commas stripped)",
     lambda: address_book.lookup("214 north st, filer, id"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('home today') → exact (short alias + trailing text)",
     lambda: address_book.lookup("home today"),
     lambda r: r is not None and r['confidence'] == 'exact' and r['id'] == 'home'),

    ("lookup('214') → partial (query is prefix of alias)",
     lambda: address_book.lookup("214"),
     lambda r: r is not None and r['confidence'] == 'partial'),

    ("lookup('214 n') → partial (partial prefix of alias)",
     lambda: address_book.lookup("214 n"),
     lambda r: r is not None and r['confidence'] == 'partial'),

    ("lookup('completely unrelated query') → None",
     lambda: address_book.lookup("completely unrelated query"),
     lambda r: r is None),

    ("lookup('214 north streets of filer') → None (no word boundary after st)",
     lambda: address_book.lookup("214 north streets of filer"),
     lambda r: r is None),
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
