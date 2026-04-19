"""Tests for nav_tools — run against live Photon + Valhalla services."""

import sys
import json

from nav_tools import route, reverse_geocode


def test_route_named():
    """route("Buhl Idaho", "Boise Idaho", "auto") returns maneuvers."""
    print("TEST 1: route('Buhl Idaho', 'Boise Idaho', 'auto')")
    r = route("Buhl Idaho", "Boise Idaho", "auto")
    assert r["summary"]["distance_miles"] > 50, f"Expected >50 mi, got {r['summary']['distance_miles']}"
    assert r["summary"]["time_minutes"] > 60, f"Expected >60 min, got {r['summary']['time_minutes']}"
    assert len(r["maneuvers"]) > 5, f"Expected >5 maneuvers, got {len(r['maneuvers'])}"
    assert r["shape"], "Missing polyline shape"
    print(f"  OK — {r['summary']['distance_miles']} mi, {r['summary']['time_minutes']} min, {len(r['maneuvers'])} maneuvers")
    print(f"  Origin: {r['origin']['name']}")
    print(f"  Destination: {r['destination']['name']}")
    print(f"  First maneuver: {r['maneuvers'][0]['instruction']}")


def test_route_coords():
    """route with raw lat,lon coordinates."""
    print("\nTEST 2: route('42.5991,-114.7636', '43.615,-116.2023', 'auto')")
    r = route("42.5991,-114.7636", "43.615,-116.2023", "auto")
    assert r["summary"]["distance_miles"] > 100, f"Expected >100 mi, got {r['summary']['distance_miles']}"
    assert len(r["maneuvers"]) > 3, f"Expected >3 maneuvers"
    print(f"  OK — {r['summary']['distance_miles']} mi, {r['summary']['time_minutes']} min")


def test_route_pedestrian():
    """route with pedestrian mode."""
    print("\nTEST 3: route('Buhl Idaho', 'Boise Idaho', 'pedestrian')")
    r = route("Buhl Idaho", "Boise Idaho", "pedestrian")
    assert r["summary"]["mode"] == "pedestrian"
    assert r["summary"]["time_minutes"] > r["summary"]["distance_miles"], "Walking should take more min than miles"
    print(f"  OK — {r['summary']['distance_miles']} mi, {r['summary']['time_minutes']} min (pedestrian)")


def test_reverse_geocode():
    """reverse_geocode near Buhl, Idaho."""
    print("\nTEST 4: reverse_geocode(42.5991, -114.7636)")
    result = reverse_geocode(42.5991, -114.7636)
    assert "Buhl" in result or "Twin Falls" in result or "Idaho" in result, f"Expected Buhl/Idaho, got: {result}"
    print(f"  OK — {result}")


def test_route_bad_origin():
    """route with nonexistent place returns clean error."""
    print("\nTEST 5: route('nonexistent place xyz123abc', 'Boise Idaho')")
    try:
        r = route("nonexistent place xyz123abc", "Boise Idaho")
        print(f"  FAIL — expected error, got result: {r['summary']}")
        return False
    except ValueError as e:
        print(f"  OK — clean error: {e}")
    except RuntimeError as e:
        print(f"  OK — runtime error: {e}")


if __name__ == "__main__":
    passed = 0
    failed = 0
    tests = [test_route_named, test_route_coords, test_route_pedestrian, test_reverse_geocode, test_route_bad_origin]

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL — {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
