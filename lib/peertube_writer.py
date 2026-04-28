"""
RECON PeerTube Writer

Authenticated PeerTube API client for pushing domain category assignments.
Uses OAuth2 password grant, caches tokens, refreshes on 401.

Config keys used:
  peertube.api_url         — internal PeerTube URL (http://192.168.1.170:9000)
  peertube.host_header     — Host header for API requests (stream.echo6.co)
  peertube.username        — PeerTube admin username
  peertube.password_env    — env var name holding the password
  peertube.rate_limit_delay — delay between API calls (seconds)
"""
import json
import os
import time

import requests as http_requests

from .recon_domains import DOMAIN_CATEGORY_MAP
from .utils import setup_logging

logger = setup_logging('recon.peertube_writer')

TOKEN_CACHE_PATH = '/opt/recon/data/peertube-oauth-token.json'


def _get_peertube_config(config):
    """Extract PeerTube writer config with defaults."""
    pt = config.get('peertube', {})
    return {
        'api_url': pt.get('api_url', pt.get('api_base', 'http://192.168.1.170:9000')),
        'host_header': pt.get('host_header', 'stream.echo6.co'),
        'username': pt.get('username', 'root'),
        'password_env': pt.get('password_env', 'PEERTUBE_PASSWORD'),
        'rate_limit_delay': pt.get('writer_rate_limit', 0.1),
    }


def _load_cached_token():
    """Load cached OAuth token from disk."""
    if os.path.exists(TOKEN_CACHE_PATH):
        try:
            with open(TOKEN_CACHE_PATH, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_token(token_data):
    """Save OAuth token to disk cache."""
    os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
    with open(TOKEN_CACHE_PATH, 'w') as f:
        json.dump(token_data, f)


def _get_oauth_client(api_url, host_header):
    """Get PeerTube OAuth client credentials.

    Args:
        api_url: Base API URL
        host_header: Host header value

    Returns:
        (client_id, client_secret) tuple
    """
    resp = http_requests.get(
        f"{api_url}/api/v1/oauth-clients/local",
        headers={'Host': host_header},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data['client_id'], data['client_secret']


def _get_token(api_url, host_header, username, password, client_id, client_secret):
    """Obtain OAuth2 access token via password grant.

    Args:
        api_url: Base API URL
        host_header: Host header value
        username: PeerTube username
        password: PeerTube password
        client_id: OAuth client ID
        client_secret: OAuth client secret

    Returns:
        Token data dict with access_token, refresh_token, etc.
    """
    resp = http_requests.post(
        f"{api_url}/api/v1/users/token",
        headers={'Host': host_header},
        data={
            'client_id': client_id,
            'client_secret': client_secret,
            'grant_type': 'password',
            'username': username,
            'password': password,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token_data['client_id'] = client_id
    token_data['client_secret'] = client_secret
    _save_token(token_data)
    return token_data


def _refresh_token(api_url, host_header, token_data):
    """Refresh an expired access token.

    Returns:
        New token data dict, or None on failure.
    """
    try:
        resp = http_requests.post(
            f"{api_url}/api/v1/users/token",
            headers={'Host': host_header},
            data={
                'client_id': token_data['client_id'],
                'client_secret': token_data['client_secret'],
                'grant_type': 'refresh_token',
                'refresh_token': token_data['refresh_token'],
            },
            timeout=30,
        )
        resp.raise_for_status()
        new_data = resp.json()
        new_data['client_id'] = token_data['client_id']
        new_data['client_secret'] = token_data['client_secret']
        _save_token(new_data)
        return new_data
    except Exception as e:
        logger.warning(f"Token refresh failed: {e}")
        return None


def _ensure_token(config):
    """Ensure we have a valid OAuth token. Returns token data dict.

    Tries cached token first, then obtains a new one.
    """
    pt = _get_peertube_config(config)
    password = os.environ.get(pt['password_env'], '')
    if not password:
        raise ValueError(f"PeerTube password not set in env var {pt['password_env']}")

    # Try cached token
    token_data = _load_cached_token()
    if token_data and 'access_token' in token_data:
        return token_data

    # Get fresh token
    client_id, client_secret = _get_oauth_client(pt['api_url'], pt['host_header'])
    return _get_token(
        pt['api_url'], pt['host_header'],
        pt['username'], password,
        client_id, client_secret,
    )


def _api_request(method, path, config, token_data, **kwargs):
    """Make an authenticated PeerTube API request with auto-refresh on 401.

    Args:
        method: HTTP method ('GET', 'PUT', etc.)
        path: API path (e.g. '/api/v1/videos/{uuid}')
        config: RECON config dict
        token_data: Current token data dict
        **kwargs: Additional requests kwargs (json, data, etc.)

    Returns:
        (response, token_data) tuple — token_data may be refreshed.
    """
    pt = _get_peertube_config(config)
    url = f"{pt['api_url']}{path}"
    headers = {
        'Host': pt['host_header'],
        'Authorization': f"Bearer {token_data['access_token']}",
    }

    resp = http_requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if resp.status_code == 401:
        # Try refresh
        new_token = _refresh_token(pt['api_url'], pt['host_header'], token_data)
        if new_token:
            headers['Authorization'] = f"Bearer {new_token['access_token']}"
            resp = http_requests.request(method, url, headers=headers, timeout=30, **kwargs)
            return resp, new_token
        else:
            # Full re-auth
            password = os.environ.get(pt['password_env'], '')
            client_id, client_secret = _get_oauth_client(pt['api_url'], pt['host_header'])
            new_token = _get_token(
                pt['api_url'], pt['host_header'],
                pt['username'], password,
                client_id, client_secret,
            )
            headers['Authorization'] = f"Bearer {new_token['access_token']}"
            resp = http_requests.request(method, url, headers=headers, timeout=30, **kwargs)
            return resp, new_token

    return resp, token_data


def push_category(video_uuid, category_id, config, token_data=None):
    """Push a category assignment to a single PeerTube video.

    Args:
        video_uuid: PeerTube video UUID
        category_id: Category ID (100-117)
        config: RECON config dict
        token_data: Optional pre-fetched token data

    Returns:
        (success: bool, token_data: dict) tuple
    """
    if token_data is None:
        token_data = _ensure_token(config)

    resp, token_data = _api_request(
        'PUT',
        f'/api/v1/videos/{video_uuid}',
        config,
        token_data,
        json={'category': category_id},
    )

    if resp.status_code in (200, 204):
        return True, token_data
    else:
        logger.warning(f"Failed to push category for {video_uuid}: "
                       f"HTTP {resp.status_code} — {resp.text[:200]}")
        return False, token_data


def extract_uuid(catalogue_path):
    """Extract PeerTube video UUID from catalogue path.

    Catalogue paths for PeerTube videos look like:
        https://stream.echo6.co/w/UUID

    Args:
        catalogue_path: catalogue.path value

    Returns:
        UUID string or None
    """
    if not catalogue_path:
        return None
    if '/w/' in catalogue_path:
        return catalogue_path.rsplit('/w/', 1)[-1]
    return None


def push_pending(db, config, limit=None):
    """Push all assigned-but-unpushed domain categories to PeerTube.

    Args:
        db: StatusDB instance
        config: RECON config dict
        limit: Optional max number of items to push

    Returns:
        (success_count, fail_count) tuple
    """
    items = db.get_unpushed_assignments()
    if limit:
        items = items[:limit]
    if not items:
        logger.info("No unpushed assignments to push")
        return (0, 0)

    pt = _get_peertube_config(config)
    delay = pt['rate_limit_delay']

    SYSTEMIC_FAIL_THRESHOLD = 5  # abort if first N items all fail

    logger.info(f"Pushing {len(items)} category assignments to PeerTube")

    token_data = _ensure_token(config)
    success = 0
    failed = 0

    for item in items:
        file_hash = item['hash']
        domain = item.get('recon_domain')
        catalogue_path = item.get('catalogue_path', '')

        if not domain or domain not in DOMAIN_CATEGORY_MAP:
            logger.warning(f"  {file_hash[:12]}: invalid domain '{domain}', skipping")
            failed += 1
            continue

        uuid = extract_uuid(catalogue_path)
        if not uuid:
            logger.warning(f"  {file_hash[:12]}: could not extract UUID from '{catalogue_path}'")
            failed += 1
            continue

        category_id = DOMAIN_CATEGORY_MAP[domain]
        ok, token_data = push_category(uuid, category_id, config, token_data)

        if ok:
            db.set_peertube_pushed(file_hash)
            success += 1
        else:
            failed += 1

        # Abort on systemic failure (e.g. plugin not installed, auth broken)
        if success == 0 and failed >= SYSTEMIC_FAIL_THRESHOLD:
            logger.error(f"Aborting push: first {failed} items all failed — "
                         f"check plugin installation and PeerTube API config")
            break

        time.sleep(delay)

    logger.info(f"Push complete: {success} succeeded, {failed} failed")
    return (success, failed)
