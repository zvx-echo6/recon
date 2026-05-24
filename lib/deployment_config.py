"""
Deployment profile loader.

Reads RECON_PROFILE env var (default: "home"), loads the matching YAML
from config/profiles/<profile>.yaml, and caches the parsed dict in memory.

Exposes get_deployment_config() as the in-process accessor for the profile.

Note: its former consumers (the /api/landclass gate, google_places,
place_detail, offroute/router) were all extracted to navi-* services or removed
across cleanups #4–#6/#27 — recon has no remaining caller of
get_deployment_config() today; the module is retained per cleanup #1.
(The former /api/config HTTP endpoint that served this dict to the frontend was
removed once navi-config (:8422) took over that route.)
"""
import os
import yaml
from .utils import setup_logging

logger = setup_logging('recon.deployment_config')

_config_cache = None


def load_deployment_config():
    """Load and cache the deployment profile. Called once at import time."""
    global _config_cache

    profile = os.environ.get('RECON_PROFILE', 'home')
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'profiles')
    config_path = os.path.join(config_dir, f'{profile}.yaml')

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Deployment profile '{profile}' not found at {config_path}. "
            f"Available profiles: {', '.join(f.replace('.yaml','') for f in os.listdir(config_dir) if f.endswith('.yaml'))}"
        )

    with open(config_path, 'r') as f:
        _config_cache = yaml.safe_load(f)

    logger.info(f"Loaded deployment profile: {profile} ({_config_cache.get('region_name', 'unknown')})")
    return _config_cache


def get_deployment_config():
    """Return the cached deployment config dict."""
    if _config_cache is None:
        load_deployment_config()
    return _config_cache


# Load on import so startup fails fast if profile is missing
load_deployment_config()
