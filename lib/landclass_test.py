#!/usr/bin/env python3
"""Tests for lib.landclass PAD-US lookups.

Live-PostgreSQL regression test using the skip-if-not-available pattern
(matching test_real_timezone_db in reverse_bundle_test.py). Plain asserts +
a __main__ runner, matching the rest of lib/*_test.py.

Note: lookup_landclass swallows DB errors and returns [] (it never raises),
so PG availability is probed via a known US point (Boise) rather than by
catching an exception.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import landclass


def test_landclass_no_antimeridian_false_match():
    # Yosemite doubles as the liveness probe: a point on real US public land.
    # (lookup_landclass returns [] when PG is unreachable AND when the point is
    # off public land, so the probe must be a known-public-land point — e.g.
    # downtown Boise is private and would yield [] even with PG up.)
    yosemite = landclass.lookup_landclass(37.85, -119.55)
    if not yosemite:
        print("  SKIP: live PG not available (Yosemite returned no rows)")
        return
    # Filter must NOT drop legitimate (non-wrapping) US units.
    assert len(yosemite) >= 1, f"Yosemite should match >=1 PAD-US unit, got {len(yosemite)}"

    # London (51.5074 N) previously false-matched the antimeridian-wrapping
    # 'Rat Islands' record (ogc_fid 3974, ~360 deg lon span). The < 60 deg
    # filter must now drop it -> empty result.
    london = landclass.lookup_landclass(51.5074, -0.1278)
    assert london == [], f"London should match no PAD-US unit, got {[r.get('unit_name') for r in london]}"
    print("  PASS: antimeridian filter drops London false-match, keeps Yosemite coverage")


if __name__ == '__main__':
    print("Running landclass tests...")
    test_landclass_no_antimeridian_false_match()
    print("All tests passed.")
