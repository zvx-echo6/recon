#!/usr/bin/env python3
"""Tests for the /api/reverse/<lat>/<lon> enrichment bundle (lib.netsyms_api).

Photon/DEM/landclass are mocked so the suite runs without live services;
one timezone test exercises the real SpatiaLite DB when it is present. Plain
asserts + a __main__ runner, matching the rest of lib/*_test.py.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from lib import netsyms_api

EXPECTED_KEYS = set(netsyms_api._BUNDLE_KEYS)


def _client():
    app = Flask(__name__)
    app.register_blueprint(netsyms_api.geocode_bp)
    return app.test_client()


def _clear_cache():
    netsyms_api._REVERSE_BUNDLE_CACHE.clear()


def test_happy_path():
    _clear_cache()
    with mock.patch.object(netsyms_api, '_reverse_photon', return_value={
            'name': 'Where you are', 'city': 'Boise', 'county': 'Ada',
            'state': 'Idaho', 'country': 'United States', 'postal_code': '83701'}), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value='America/Boise'), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value='Boise National Forest'), \
         mock.patch.object(netsyms_api, '_reverse_elevation', return_value=824):
        resp = _client().get('/api/reverse/43.6150/-116.2023')
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert set(data.keys()) == EXPECTED_KEYS, data.keys()
    assert data['city'] == 'Boise' and data['timezone'] == 'America/Boise'
    assert data['landclass'] == 'Boise National Forest' and data['elevation_m'] == 824
    print("  PASS: happy path — all 9 fields populated, exact key set")


def test_negative_and_integer_coords_parse():
    # Regression: Flask's <float:> converter would 404 these; manual parse must not.
    _clear_cache()
    with mock.patch.object(netsyms_api, '_reverse_photon', return_value={}), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_elevation', return_value=None):
        for path in ('/api/reverse/43.6/-116.2', '/api/reverse/43/-116'):
            resp = _client().get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"
            assert set(resp.get_json().keys()) == EXPECTED_KEYS
    print("  PASS: negative and integer coordinates parse (200, not 404)")


def test_partial_failure_returns_200_with_nulls():
    _clear_cache()
    with mock.patch.object(netsyms_api, '_reverse_photon',
                           side_effect=RuntimeError('photon down')), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value='America/Boise'), \
         mock.patch.object(netsyms_api, '_reverse_landclass',
                           side_effect=RuntimeError('postgis down')), \
         mock.patch.object(netsyms_api, '_reverse_elevation', return_value=824):
        resp = _client().get('/api/reverse/43.6150/-116.2023')
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert set(data.keys()) == EXPECTED_KEYS
    assert data['name'] is None and data['city'] is None     # photon failed -> nulls
    assert data['landclass'] is None                          # landclass failed -> null
    assert data['timezone'] == 'America/Boise' and data['elevation_m'] == 824
    print("  PASS: per-component failure -> 200 with nulls, no 5xx")


def test_ocean_point_mostly_null():
    _clear_cache()
    with mock.patch.object(netsyms_api, '_reverse_photon', return_value={}), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value='Etc/GMT+2'), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_elevation', return_value=0):
        resp = _client().get('/api/reverse/0.0/-30.0')
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert set(data.keys()) == EXPECTED_KEYS
    assert data['city'] is None and data['country'] is None and data['landclass'] is None
    print("  PASS: ocean point -> 200, mostly null")


def test_invalid_input_400():
    _clear_cache()
    client = _client()
    for path in ('/api/reverse/9999/0', '/api/reverse/0/9999', '/api/reverse/abc/0'):
        resp = client.get(path)
        assert resp.status_code == 400, f"{path} -> {resp.status_code}"
    print("  PASS: out-of-range / unparseable input -> 400")


def test_cache_hit_serves_without_recompute():
    _clear_cache()
    with mock.patch.object(netsyms_api, '_reverse_photon',
                           return_value={'name': 'X'}) as m_photon, \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_elevation', return_value=None):
        client = _client()
        client.get('/api/reverse/12.3456/-65.4321')
        client.get('/api/reverse/12.3456/-65.4321')   # same key (rounded) -> cached
        assert m_photon.call_count == 1, f"expected 1 compute, got {m_photon.call_count}"
    print("  PASS: second identical request served from cache (no recompute)")


def test_real_timezone_db():
    if not os.path.exists(netsyms_api._TZ_DB_PATH):
        print("  SKIP: real timezone test (timezones.sqlite not present)")
        return
    assert netsyms_api._reverse_timezone(43.6150, -116.2023) == 'America/Boise'
    assert netsyms_api._reverse_timezone(40.7128, -74.0060) == 'America/New_York'
    print("  PASS: real timezones.sqlite point-in-polygon")


def test_elevation_from_dem_reader_mock():
    # elevation_m comes from DEMReader.sample_point (not Valhalla); other
    # components stubbed to null so the bundle is hermetic.
    _clear_cache()
    fake_dem = mock.Mock()
    fake_dem.sample_point.return_value = 824
    with mock.patch.object(netsyms_api, '_DEM', fake_dem), \
         mock.patch.object(netsyms_api, '_reverse_photon', return_value={}), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value=None):
        resp = _client().get('/api/reverse/43.6150/-116.2023')
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert set(data.keys()) == EXPECTED_KEYS
    assert data['elevation_m'] == 824, data['elevation_m']
    fake_dem.sample_point.assert_called_once()
    print("  PASS: elevation_m sourced from DEMReader.sample_point")


def test_elevation_dem_unavailable():
    # DEMReader failed to init at startup (_DEM is None) -> elevation_m null, 200.
    _clear_cache()
    with mock.patch.object(netsyms_api, '_DEM', None), \
         mock.patch.object(netsyms_api, '_reverse_photon', return_value={}), \
         mock.patch.object(netsyms_api, '_reverse_timezone', return_value=None), \
         mock.patch.object(netsyms_api, '_reverse_landclass', return_value=None):
        resp = _client().get('/api/reverse/43.6150/-116.2023')
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert set(data.keys()) == EXPECTED_KEYS
    assert data['elevation_m'] is None
    print("  PASS: DEMReader unavailable -> elevation_m null, still 200")


if __name__ == '__main__':
    print("Running reverse-bundle tests...")
    test_happy_path()
    test_negative_and_integer_coords_parse()
    test_partial_failure_returns_200_with_nulls()
    test_ocean_point_mostly_null()
    test_invalid_input_400()
    test_cache_hit_serves_without_recompute()
    test_real_timezone_db()
    test_elevation_from_dem_reader_mock()
    test_elevation_dem_unavailable()
    print("All tests passed.")
