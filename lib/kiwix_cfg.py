"""Kiwix settings: paths, binaries, URLs. Read from the optional `kiwix:` section of
config.yaml; every default is the value the code used before the section existed."""
import logging
import os
import signal
import subprocess

from .utils import get_config

logger = logging.getLogger("recon.kiwix_cfg")

DEFAULTS = {
    'dir': '/mnt/kiwix',                              # ZIM files + library.xml
    'library_xml': None,                              # default: <dir>/library.xml
    'manage_bin': '/opt/recon/bin/kiwix-manage',
    'serve_url': 'http://localhost:8430',             # local kiwix-serve (OPDS)
    'catalog_url': 'https://opds.library.kiwix.org',  # official Kiwix catalog (OPDS)
    'sighup_serve': True,                             # SIGHUP kiwix-serve after library changes
    'timezone': 'America/Boise',                      # schedules and the calendar
    'daily_check_time': '03:00',                      # daily catalog check (local time)
    'scheduler': True,                                # run the update scheduler thread
    'file_owner': None,                               # optional "user:group" for downloaded ZIMs
}


def kcfg():
    cfg = dict(DEFAULTS)
    cfg.update(get_config().get('kiwix') or {})
    cfg['dir'] = cfg['dir'].rstrip('/') or '/'
    if not cfg.get('library_xml'):
        cfg['library_xml'] = os.path.join(cfg['dir'], 'library.xml')
    return cfg


def reload_kiwix_serve():
    """SIGHUP kiwix-serve so it re-reads library.xml (no restart). No-op when disabled."""
    if not kcfg()['sighup_serve']:
        return
    try:
        result = subprocess.run(['pidof', 'kiwix-serve'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split()[0])
            os.kill(pid, signal.SIGHUP)
            logger.info("Sent SIGHUP to kiwix-serve (pid %d)", pid)
    except Exception as e:
        logger.warning("Failed to signal kiwix-serve: %s", e)
