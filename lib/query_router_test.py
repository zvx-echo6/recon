#!/usr/bin/env python3
"""Test suite for the semantic query router."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.query_router import classify

TEST_QUERIES = [
    ("how do I get from Buhl to Boise", "nav_route"),
    ("what does the survival manual say about water", "rag_search"),
    ("what town is at 42.5, -114.7", "nav_reverse_geocode"),
    ("hey aurora", "direct_answer"),
    ("what's the fastest way to Sun Valley", "nav_route"),
    ("how to purify water in the field", "rag_search"),
    ("good morning", "direct_answer"),
]


def main():
    print("Query Router Test Suite")
    print("=" * 70)

    passed = 0
    failed = 0

    for query, expected in TEST_QUERIES:
        route, confidence = classify(query)
        status = "PASS" if route == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {query!r}")
        print(f"         → {route} ({confidence:.3f})  expected={expected}")

    print("=" * 70)
    print(f"Results: {passed}/{passed + failed} passed")
    if failed:
        print(f"  {failed} FAILED")
        sys.exit(1)
    else:
        print("  All tests passed!")


if __name__ == "__main__":
    main()
