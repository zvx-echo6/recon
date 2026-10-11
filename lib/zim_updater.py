"""
ZIM updater - keeps the ZIMs in the kiwix-serve library up to date.

  * Catalog check: finds the newest version of each library ZIM in the official Kiwix
    OPDS catalog (matching name + flavour) and stores it in zim_update_catalog.
  * Update: download (.meta4 -> mirror, size + sha256 verified), add to library.xml,
    move the old file to <kiwix.dir>/.previous/ (one previous copy per ZIM), pull the old
    version's search index (Qdrant vectors + DB rows) and queue the new one for ingest.
  * Rollback: swap the .previous copy back in (the current file becomes the new previous).
  * Re-crawl: self-crawled (Zimit) ZIMs are re-crawled through a new scrape job; the
    scraper runner calls finish_recrawl() to swap the result in.
  * Scheduler: per-ZIM schedules in zim_update_schedules, a daily catalog check, and
    the calendar data. The scheduler thread re-reads the DB every tick, so edits apply live.

A ZIM is identified by its key: the file name without the date (and crawl job id), e.g.
wikiquote_af_all_maxi_2026-04.zim -> wikiquote_af_all_maxi.

Updates run one at a time (module-level lock). Settings: config.yaml `kiwix:` section.
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from .kiwix_cfg import kcfg, reload_kiwix_serve
from .status import StatusDB
from . import zim_monitor

logger = logging.getLogger("recon.zim_updater")

ATOM = "http://www.w3.org/2005/Atom"
META4 = "urn:ietf:params:xml:ns:metalink"
_NAME_RE = re.compile(r'^(?P<base>.+?)_(?P<ver>\d{4}-\d{2}[a-z]?)(?:_(?P<job>\d+))?$')
MARGIN = 1.10                 # free-space margin
MISSED_AFTER = timedelta(hours=12)   # a schedule overdue by more than this is skipped, not run late
STALE_TMP_PREFIX = '.download_'

_RUN_LOCK = threading.Lock()  # one update/rollback/swap at a time
_db_inst = None
_check_all_running = threading.Event()


class StepError(Exception):
    """A step failed in a way that is reported on the run as 'failed'."""


class Skip(Exception):
    """A pre-condition is not met; the run is recorded as 'skipped' with this reason."""


# ── small helpers ────────────────────────────────────────────────

def _conn():
    global _db_inst
    if _db_inst is None:
        _db_inst = StatusDB()
    return _db_inst._get_conn()


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(s):
    return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def _tz():
    return ZoneInfo(kcfg()['timezone'])


def _fmt_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def parse_name(filename):
    """file name -> (key, version, crawl job id). Version/job are None when absent."""
    stem = filename[:-4] if filename.endswith('.zim') else filename
    m = _NAME_RE.match(stem)
    if not m:
        return stem, None, None
    return m.group('base'), m.group('ver'), (int(m.group('job')) if m.group('job') else None)


def vlabel(filename):
    """Version shown to people: '2026-09', or '2026-10 #23' for a self-crawl (crawl job id)."""
    _, ver, job = parse_name(filename)
    return f"{ver} #{job}" if ver and job else (ver or '-')


def _vkey(ver):
    """Sortable version: '2026-09' < '2026-09b' < '2026-10'."""
    ver = ver or ''
    return (ver[:7], ver[7:])


def previous_dir():
    return os.path.join(kcfg()['dir'], '.previous')


def previous_files(key):
    """[(version, filename)] of kept previous copies for this key, newest first."""
    out = []
    try:
        names = os.listdir(previous_dir())
    except FileNotFoundError:
        return out
    for fn in names:
        if fn.endswith('.zim'):
            k, ver, _ = parse_name(fn)
            if k == key:
                out.append((ver, fn))
    out.sort(key=lambda t: _vkey(t[0]), reverse=True)
    return out


# ── run records ──────────────────────────────────────────────────

def _run_create(key, title, action, trigger, message='', **fields):
    cols = {'zim_key': key, 'title': title, 'action': action, 'trigger': trigger,
            'status': 'running', 'message': message, 'started_at': _iso(_utcnow())}
    cols.update(fields)
    c = _conn()
    cur = c.execute(
        f"INSERT INTO zim_update_runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()))
    c.commit()
    return cur.lastrowid


def _run_set(run_id, **fields):
    if not fields:
        return
    c = _conn()
    c.execute(f"UPDATE zim_update_runs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
              list(fields.values()) + [run_id])
    c.commit()


def _run_log(run_id, msg):
    line = f"{datetime.now(_tz()).strftime('%H:%M:%S')} {msg}\n"
    c = _conn()
    c.execute("UPDATE zim_update_runs SET log = COALESCE(log, '') || ? WHERE id = ?", (line, run_id))
    c.commit()
    logger.info("run %s: %s", run_id, msg)


def _run_finish(run_id, status, message, **fields):
    _run_set(run_id, status=status, message=message, ended_at=_iso(_utcnow()), **fields)
    _run_log(run_id, f"{status}: {message}")
    _refresh_dashboard_cache()


def _refresh_dashboard_cache():
    try:
        from . import api
        api._cache['kiwix_sources'] = api._build_kiwix_sources()
    except Exception:
        pass


# ── inventory ────────────────────────────────────────────────────

def _crawl_job_for(filename):
    row = _conn().execute(
        "SELECT * FROM scrape_jobs WHERE zim_filename = ? ORDER BY id DESC LIMIT 1", (filename,)).fetchone()
    return dict(row) if row else None


def inventory():
    """Library ZIMs by key (newest version per key), from zim_sources + the file system."""
    rows = _conn().execute("SELECT * FROM zim_sources WHERE status != 'removed'").fetchall()
    items = {}
    for r in rows:
        r = dict(r)
        key, ver, job = parse_name(r['zim_filename'])
        cur = items.get(key)
        if cur and (_vkey(cur['version']), cur['id']) > (_vkey(ver), r['id']):
            continue
        try:
            size = os.path.getsize(r['zim_path'])
        except OSError:
            size = None
        items[key] = {
            'key': key, 'id': r['id'], 'filename': r['zim_filename'], 'path': r['zim_path'],
            'uuid': r['zim_uuid'], 'title': r['title'] or r['zim_filename'], 'version': ver,
            'crawl_job': job, 'label': vlabel(r['zim_filename']), 'status': r['status'], 'ingest_enabled': r['ingest_enabled'],
            'size': size, 'domain': r.get('domain'), 'subdomain': r.get('subdomain'),
            'ingestion_tier': r.get('ingestion_tier'),
        }
    return items


def _ingest_conflict(item):
    """Another library ZIM with the same title that is ingested/ingesting (its documents share
    this ZIM's catalogue category), or None."""
    row = _conn().execute(
        "SELECT zim_filename FROM zim_sources WHERE title = ? AND id != ? "
        "AND status IN ('ingesting', 'complete') LIMIT 1", (item['title'], item['id'])).fetchone()
    return row['zim_filename'] if row else None


def _doc_count(title):
    return _conn().execute(
        "SELECT COUNT(*) FROM catalogue WHERE source = 'kiwix' AND category = ?", (title,)).fetchone()[0]


# ── catalog check ────────────────────────────────────────────────

def _derive_name(key):
    for fl in ('maxi', 'mini', 'nopic'):
        if key.endswith('_' + fl):
            return key[:-len(fl) - 1], fl
    return key, ''


def _opds_names():
    """filename -> (name, flavour) from the local kiwix-serve OPDS."""
    out = {}
    try:
        for e in zim_monitor.fetch_opds():
            if e.get('zim_filename'):
                out[e['zim_filename']] = (e.get('name') or '', e.get('flavour') or '')
    except Exception as e:
        logger.warning("local OPDS unavailable: %s", e)
    return out


def check_key(key, item=None, names=None):
    """Look up one ZIM in the Kiwix catalog and store the result. Returns the catalog row dict."""
    item = item or inventory().get(key)
    if not item:
        raise StepError(f"{key}: not in library")
    row = {'zim_key': key, 'name': None, 'flavour': None, 'in_catalog': 0, 'latest_version': None,
           'latest_filename': None, 'meta4_url': None, 'size_bytes': None,
           'checked_at': _iso(_utcnow()), 'error': None}
    if _crawl_job_for(item['filename']):
        row['error'] = None   # self-crawl: not looked up
        _store_catalog(row)
        return row
    names = names if names is not None else _opds_names()
    name, flavour = names.get(item['filename']) or _derive_name(key)
    row['name'], row['flavour'] = name, flavour
    try:
        url = f"{kcfg()['catalog_url'].rstrip('/')}/catalog/v2/entries?name={quote(name)}&count=50"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        best = None
        for entry in root.findall(f"{{{ATOM}}}entry"):
            for link in entry.findall(f"{{{ATOM}}}link"):
                if link.get('type') != 'application/x-zim':
                    continue
                href = urljoin(url, link.get('href', ''))
                fn = os.path.basename(href.split('?')[0])
                if fn.endswith('.meta4'):
                    fn = fn[:-6]
                base, ver, _ = parse_name(fn)
                if base != key or not ver:
                    continue
                if best is None or _vkey(ver) > _vkey(best['ver']):
                    best = {'ver': ver, 'fn': fn, 'href': href, 'len': link.get('length')}
        if best:
            row.update(in_catalog=1, latest_version=best['ver'], latest_filename=best['fn'],
                       meta4_url=best['href'], size_bytes=int(best['len']) if best['len'] else None)
    except Exception as e:
        row['error'] = f"{type(e).__name__}: {e}"[:300]
    _store_catalog(row)
    return row


def _store_catalog(row):
    c = _conn()
    cols = list(row)
    c.execute(f"INSERT OR REPLACE INTO zim_update_catalog ({', '.join(cols)}) "
              f"VALUES ({', '.join('?' * len(cols))})", [row[k] for k in cols])
    c.commit()


def check_all(skip_auto_update=False):
    """Check every catalog ZIM. skip_auto_update: leave ZIMs with an enabled schedule alone
    (their own update run checks first). Returns the number checked."""
    items = inventory()
    scheduled = {r['zim_key'] for r in _conn().execute(
        "SELECT zim_key FROM zim_update_schedules WHERE enabled = 1").fetchall()}
    names = _opds_names()
    n = 0
    for key, item in items.items():
        if skip_auto_update and key in scheduled:
            continue
        try:
            check_key(key, item, names)
            n += 1
        except Exception as e:
            logger.warning("check %s failed: %s", key, e)
        time.sleep(0.2)
    _conn().execute("INSERT OR REPLACE INTO zim_update_state (k, v) VALUES ('last_check_at', ?)",
                    (_iso(_utcnow()),))
    _conn().commit()
    return n


def check_all_async():
    if _check_all_running.is_set():
        return False
    _check_all_running.set()

    def _go():
        try:
            check_all()
        finally:
            _check_all_running.clear()
    threading.Thread(target=_go, daemon=True, name='zim-check-all').start()
    return True


# ── listing ──────────────────────────────────────────────────────

def _zim_status(item, cat, crawl, running):
    if running:
        return 'updating'
    if crawl:
        return 'self-crawl'
    if not cat or not cat['checked_at']:
        return 'not checked'
    if cat['error']:
        return 'check failed'
    if not cat['in_catalog']:
        return 'not in catalog'
    if _vkey(cat['latest_version']) > _vkey(item['version']):
        return 'update available'
    return 'current'


def list_updates():
    c = _conn()
    items = inventory()
    cats = {r['zim_key']: dict(r) for r in c.execute("SELECT * FROM zim_update_catalog").fetchall()}
    scheds = {r['zim_key']: dict(r) for r in c.execute("SELECT * FROM zim_update_schedules").fetchall()}
    last_runs = {}
    for r in c.execute("SELECT * FROM zim_update_runs WHERE id IN "
                       "(SELECT MAX(id) FROM zim_update_runs GROUP BY zim_key)").fetchall():
        last_runs[r['zim_key']] = dict(r)
    running = {r['zim_key']: dict(r) for r in c.execute(
        "SELECT * FROM zim_update_runs WHERE status = 'running'").fetchall()}
    state = {r['k']: r['v'] for r in c.execute("SELECT * FROM zim_update_state").fetchall()}

    out = []
    for key, it in items.items():
        cat = cats.get(key)
        crawl = _crawl_job_for(it['filename'])
        run = running.get(key)
        status = _zim_status(it, cat, crawl, bool(run))
        prev = previous_files(key)
        lr = last_runs.get(key)
        recrawl_reason = None
        if crawl:
            if not crawl.get('url'):
                recrawl_reason = 'crawl settings not recorded'
        entry = {
            'key': key, 'source_id': it['id'], 'title': it['title'], 'filename': it['filename'],
            'kind': 'self-crawl' if crawl else 'catalog',
            'current_version': it['label'],
            'latest_version': (cat or {}).get('latest_version') if not crawl else None,
            'download_size': (cat or {}).get('size_bytes') if not crawl else None,
            'size': it['size'], 'status': status, 'checked_at': (cat or {}).get('checked_at'),
            'check_error': (cat or {}).get('error'),
            'ingest_status': it['status'], 'ingest_enabled': it['ingest_enabled'],
            'has_previous': bool(prev), 'previous_version': vlabel(prev[0][1]) if prev else None,
            'can_recrawl': bool(crawl) and not recrawl_reason, 'recrawl_reason': recrawl_reason,
            'schedule': scheds.get(key),
            'last_run': ({k: lr[k] for k in ('id', 'action', 'trigger', 'status', 'message', 'started_at', 'ended_at')}
                         if lr else None),
            'running_run': ({k: run[k] for k in ('id', 'action', 'message', 'bytes', 'bytes_total')} if run else None),
        }
        out.append(entry)
    out.sort(key=lambda e: e['title'].lower())
    return {'items': out, 'timezone': kcfg()['timezone'], 'now': _iso(_utcnow()),
            'last_check_at': state.get('last_check_at'), 'daily_check_time': kcfg()['daily_check_time'],
            'checking': _check_all_running.is_set(), 'busy': _RUN_LOCK.locked()}


# ── downloading ──────────────────────────────────────────────────

def fetch_meta4(url):
    """-> {'name', 'size', 'sha256', 'urls': [mirror urls by priority]}"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    f = root.find(f"{{{META4}}}file")
    if f is None:
        raise StepError("meta4 has no file entry")
    size = f.findtext(f"{{{META4}}}size")
    sha = None
    for h in f.findall(f"{{{META4}}}hash"):
        if h.get('type') in ('sha-256', 'sha256'):
            sha = (h.text or '').strip().lower()
    urls = []
    for u in f.findall(f"{{{META4}}}url"):
        if u.text and u.text.strip():
            try:
                prio = int(u.get('priority', '99'))
            except ValueError:
                prio = 99
            urls.append((prio, u.text.strip()))
    urls.sort(key=lambda t: t[0])
    mirrors = [u for _, u in urls]
    if not mirrors and url.endswith('.meta4'):
        mirrors = [url[:-6]]
    return {'name': f.get('name'), 'size': int(size) if size else None, 'sha256': sha, 'urls': mirrors}


def _download(run_id, meta, tmp_path):
    """Stream from the mirrors in priority order until one verifies. Raises StepError."""
    last_err = 'no mirrors listed'
    _run_set(run_id, bytes_total=meta['size'] or 0, bytes=0)
    for mirror in meta['urls'][:5]:
        host = mirror.split('/')[2] if '//' in mirror else mirror
        _run_log(run_id, f"downloading from {host}")
        h = hashlib.sha256()
        got = 0
        try:
            with requests.get(mirror, stream=True, timeout=(15, 60)) as r:
                r.raise_for_status()
                last_tick = time.time()
                with open(tmp_path, 'wb') as out:
                    for chunk in r.iter_content(1 << 20):
                        if not chunk:
                            continue
                        out.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
                        if time.time() - last_tick >= 2:
                            _run_set(run_id, bytes=got)
                            last_tick = time.time()
            _run_set(run_id, bytes=got)
        except Exception as e:
            last_err = f"{host}: {type(e).__name__}: {e}"[:200]
            _run_log(run_id, f"download failed ({last_err})")
            continue
        if meta['size'] and got != meta['size']:
            last_err = f"{host}: size {got} != expected {meta['size']}"
            _run_log(run_id, f"size mismatch ({got} of {meta['size']} bytes)")
            continue
        if meta['sha256'] and h.hexdigest() != meta['sha256']:
            last_err = f"{host}: checksum mismatch"
            _run_log(run_id, "sha256 mismatch")
            continue
        _run_log(run_id, f"downloaded {_fmt_bytes(got)}, size and sha256 verified")
        return got
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    raise StepError(f"download failed: {last_err}")


def _check_zim_opens(path):
    if not zim_monitor.HAVE_LIBZIM:
        return
    from libzim.reader import Archive
    try:
        Archive(path).entry_count
    except Exception as e:
        raise StepError(f"downloaded file is not a valid ZIM: {e}")


# ── library + swap ───────────────────────────────────────────────

def _manage(*args):
    d = kcfg()
    res = subprocess.run([d['manage_bin'], d['library_xml']] + list(args),
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise StepError(f"kiwix-manage {args[0]} failed: {(res.stderr or res.stdout).strip()[:200]}")
    return res.stdout


def _library_remove(filename, uuid):
    from .api import _kiwix_remove_book
    return _kiwix_remove_book(filename, uuid)


def _wait_for_source(filename, timeout=90):
    """Rescan until kiwix-serve lists the file (adds the zim_sources row). Returns the row dict."""
    deadline = time.time() + timeout
    while True:
        try:
            zim_monitor.scan_zims()
        except Exception as e:
            logger.warning("scan_zims: %s", e)
        row = _conn().execute("SELECT * FROM zim_sources WHERE zim_filename = ? AND status != 'removed'",
                              (filename,)).fetchone()
        if row:
            return dict(row)
        if time.time() > deadline:
            return None
        time.sleep(3)


def _spawn_ingest(source_id):
    from .api import _spawn_zim_ingest
    _spawn_zim_ingest(source_id)


def _file_perms(path):
    """Make a ZIM readable by kiwix-serve (0644) and apply kiwix.file_owner.

    Zimit runs in a container as root, so a freshly crawled ZIM is root-owned and recon
    (running as an unprivileged user) cannot chmod/chown it. That is fine as long as the
    file is already world-readable: recon owns the directory, so it can still rename and
    delete the file. Only fail when the file is genuinely unreadable by others."""
    try:
        os.chmod(path, 0o644)
        owner = kcfg().get('file_owner')
        if owner:
            shutil.chown(path, *owner.split(':', 1)) if ':' in owner else shutil.chown(path, owner)
    except PermissionError as e:
        if os.stat(path).st_mode & 0o004:
            logger.warning("could not change permissions on %s (%s); already world-readable, continuing", path, e)
        else:
            raise


def _preflight_index(old):
    """Qdrant must be reachable before an index is pulled (a half-done swap would leave
    orphan vectors). Only needed when the old version has documents."""
    if not (old['status'] == 'complete' or _doc_count(old['title'])):
        return
    from .utils import get_config
    v = get_config().get('vector_db', {})
    url = f"http://{v.get('host', '100.64.0.14')}:{v.get('port', 6333)}/collections"
    try:
        requests.get(url, timeout=10).raise_for_status()
    except Exception as e:
        raise Skip(f"Qdrant unreachable ({type(e).__name__}); the old search index cannot be removed safely")


def _swap(run_id, key, new_filename, old, undo_new):
    """New ZIM file is already in place in kiwix.dir. Add it, retire the old one, move the
    index over. undo_new: 'delete' | 'keep' | 'restore' (what to do with the new file if the
    swap fails before the old version is retired). Returns a dict of what happened."""
    d = kcfg()
    new_path = os.path.join(d['dir'], new_filename)
    out = {'ingest_queued': False}

    def undo():
        try:
            _library_remove(new_filename, (_conn().execute(
                "SELECT zim_uuid FROM zim_sources WHERE zim_filename = ?", (new_filename,)).fetchone() or {'zim_uuid': None})['zim_uuid'])
        except Exception:
            pass
        _conn().execute("DELETE FROM zim_sources WHERE zim_filename = ?", (new_filename,))
        _conn().commit()
        if undo_new == 'delete':
            _try_remove(new_path)
        elif undo_new == 'restore':
            os.makedirs(previous_dir(), exist_ok=True)
            os.replace(new_path, os.path.join(previous_dir(), new_filename))
        reload_kiwix_serve()

    # 1. add to the library and wait for kiwix-serve
    try:
        _manage('add', new_path)
        _run_log(run_id, "added to library.xml")
        reload_kiwix_serve()
        new = _wait_for_source(new_filename)
        if not new:
            raise StepError("kiwix-serve did not list the new ZIM within 90 s")
        if new['zim_uuid'] and new['zim_uuid'] == old['uuid']:
            raise StepError("new ZIM has the same book id as the current one")
    except Exception:
        undo()
        raise

    # 2. retire the old version: library entry first, then the file into .previous
    if not _library_remove(old['filename'], old['uuid']):
        undo()
        raise StepError("could not remove the old ZIM from library.xml")
    reload_kiwix_serve()
    _run_log(run_id, f"removed {old['filename']} from library.xml")
    try:
        os.makedirs(previous_dir(), exist_ok=True)
        for _, fn in previous_files(key):
            os.remove(os.path.join(previous_dir(), fn))
            _run_log(run_id, f"deleted older previous copy {fn}")
        os.replace(old['path'], os.path.join(previous_dir(), old['filename']))
        _run_log(run_id, f"moved {old['filename']} to .previous/")
    except Exception as e:
        try:   # put the old version back
            _manage('add', old['path'])
            reload_kiwix_serve()
        except Exception:
            pass
        undo()
        raise StepError(f"could not move the old file to .previous: {e}")

    # 3. search index: pull the old version's, queue the new one
    reingest = old['status'] == 'complete' or bool(old['ingest_enabled'])
    has_docs = old['status'] == 'complete' or _doc_count(old['title']) > 0
    if has_docs:
        from .api import _full_zim_cleanup
        res = _full_zim_cleanup(old['id'], remove_file=False, remove_library=False, remove_scrape_jobs=False)
        if res is None:
            raise StepError("old ZIM source row vanished during index cleanup")
        if res.get('blocked'):
            raise StepError(f"swapped, but the old index was not removed: {res['blocked']}")
        _run_log(run_id, f"old index removed ({res['docs_deleted']} documents, "
                         f"{res['vectors_deleted']} vectors)")
    else:
        c = _conn()
        c.execute("DELETE FROM zim_articles WHERE zim_source_id = ?", (old['id'],))
        c.execute("DELETE FROM zim_sources WHERE id = ?", (old['id'],))
        c.commit()

    # 4. carry settings over to the new row, scan, ingest
    c = _conn()
    c.execute("UPDATE zim_sources SET ingest_enabled = ?, domain = COALESCE(domain, ?), "
              "subdomain = COALESCE(subdomain, ?), ingestion_tier = COALESCE(ingestion_tier, ?) WHERE id = ?",
              (1 if reingest else 0, old.get('domain'), old.get('subdomain'), old.get('ingestion_tier'), new['id']))
    c.commit()
    try:
        zim_monitor.scan_zims()
    except Exception as e:
        logger.warning("scan_zims after swap: %s", e)
    if reingest:
        _spawn_ingest(new['id'])
        out['ingest_queued'] = True
        _run_log(run_id, "ingest of the new version queued")
    out['new_id'] = new['id']
    return out


def _try_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ── guards shared by update / rollback / re-crawl ────────────────

def _guard_item(item):
    if item['status'] == 'ingesting':
        raise Skip("ingest in progress for this ZIM")
    if item['status'] == 'complete' or _doc_count(item['title']):
        other = _ingest_conflict(item)
        if other:
            raise Skip(f"title shared with {other}, which is ingested; cleanup by title would delete its documents")


def _disk_check(need_bytes):
    free = shutil.disk_usage(kcfg()['dir']).free
    if free < need_bytes:
        raise Skip(f"not enough disk space: need {_fmt_bytes(need_bytes)}, free {_fmt_bytes(free)}")
    return free


def _begin(key, action, trigger, message=''):
    """Take the global lock and open a run. Returns (run_id, None) or (None, error)."""
    items = inventory()
    item = items.get(key)
    if not item:
        return None, f"{key}: not in the library"
    c = _conn()
    if c.execute("SELECT 1 FROM zim_update_runs WHERE zim_key = ? AND status = 'running'", (key,)).fetchone():
        return None, "a run for this ZIM is already in progress"
    if not _RUN_LOCK.acquire(blocking=False):
        return None, "another update is running"
    try:
        run_id = _run_create(key, item['title'], action, trigger, message,
                             old_version=item['label'], old_filename=item['filename'])
    except Exception:
        _RUN_LOCK.release()
        raise
    return run_id, None


def _spawn(fn, run_id, key, name):
    def _go():
        try:
            fn(run_id, key)
        except Skip as e:
            _run_finish(run_id, 'skipped', str(e))
        except StepError as e:
            _run_finish(run_id, 'failed', str(e))
        except Exception as e:
            logger.exception("run %s crashed", run_id)
            _run_finish(run_id, 'failed', f"{type(e).__name__}: {e}"[:300])
        finally:
            _RUN_LOCK.release()
    threading.Thread(target=_go, daemon=True, name=name).start()


# ── update ───────────────────────────────────────────────────────

def start_update(key, trigger='manual'):
    run_id, err = _begin(key, 'update', trigger, 'starting')
    if err:
        return None, err
    _spawn(_do_update, run_id, key, f'zim-update-{key}')
    return run_id, None


def _do_update(run_id, key):
    d = kcfg()
    item = inventory()[key]
    if _crawl_job_for(item['filename']):
        raise Skip("self-crawled ZIM: use re-crawl")
    _run_log(run_id, f"checking catalog for {key} (current {item['version']})")
    cat = check_key(key, item)
    if cat['error']:
        raise StepError(f"catalog check failed: {cat['error']}")
    if not cat['in_catalog']:
        raise Skip("not in the Kiwix catalog")
    if _vkey(cat['latest_version']) <= _vkey(item['version']):
        return _run_finish(run_id, 'current', f"already current ({item['version']})")
    new_filename = cat['latest_filename']
    _run_set(run_id, new_version=cat['latest_version'], new_filename=new_filename,
             message=f"{item['version']} to {cat['latest_version']}")
    _run_log(run_id, f"update available: {item['version']} to {cat['latest_version']}")

    _guard_item(item)
    new_path = os.path.join(d['dir'], new_filename)
    if os.path.exists(new_path):
        raise Skip(f"{new_filename} already exists in {d['dir']}")

    meta = fetch_meta4(cat['meta4_url'])
    size = meta['size'] or cat['size_bytes'] or 0
    prev_size = sum(os.path.getsize(os.path.join(previous_dir(), fn)) for _, fn in previous_files(key))
    free = _disk_check(int((size + prev_size) * MARGIN))
    _run_log(run_id, f"disk ok: need {_fmt_bytes((size + prev_size) * MARGIN)}, free {_fmt_bytes(free)}")
    _preflight_index(item)

    tmp = os.path.join(d['dir'], f"{STALE_TMP_PREFIX}{new_filename}.tmp")
    got = _download(run_id, meta, tmp)
    try:
        _check_zim_opens(tmp)
        _file_perms(tmp)
        os.replace(tmp, new_path)
    except Exception:
        _try_remove(tmp)
        _try_remove(new_path)
        raise
    res = _swap(run_id, key, new_filename, item, undo_new='delete')
    msg = f"{item['version']} to {cat['latest_version']}, {_fmt_bytes(got)} downloaded, previous copy kept"
    if res['ingest_queued']:
        msg += ", ingest queued"
    _run_finish(run_id, 'updated', msg, bytes=got)


# ── rollback ─────────────────────────────────────────────────────

def start_rollback(key, trigger='manual'):
    run_id, err = _begin(key, 'rollback', trigger, 'starting')
    if err:
        return None, err
    _spawn(_do_rollback, run_id, key, f'zim-rollback-{key}')
    return run_id, None


def _do_rollback(run_id, key):
    d = kcfg()
    item = inventory()[key]
    prev = previous_files(key)
    if not prev:
        raise Skip("no previous copy")
    ver, fn = prev[0]
    _run_set(run_id, new_version=vlabel(fn), new_filename=fn, message=f"{item['label']} back to {vlabel(fn)}")
    _guard_item(item)
    src = os.path.join(previous_dir(), fn)
    dst = os.path.join(d['dir'], fn)
    if os.path.exists(dst):
        raise Skip(f"{fn} already exists in {d['dir']}")
    _preflight_index(item)
    _check_zim_opens(src)
    os.replace(src, dst)          # same file system: no extra space needed
    _file_perms(dst)
    _run_log(run_id, f"restored {fn} from .previous/")
    res = _swap(run_id, key, fn, item, undo_new='restore')
    msg = f"{item['label']} back to {vlabel(fn)}, current version kept as previous"
    if res['ingest_queued']:
        msg += ", ingest queued"
    _run_finish(run_id, 'updated', msg)


# ── re-crawl (self-crawled ZIMs) ─────────────────────────────────

def start_recrawl(key, trigger='manual'):
    items = inventory()
    item = items.get(key)
    if not item:
        return None, f"{key}: not in the library"
    job = _crawl_job_for(item['filename'])
    if not job:
        return None, "not a self-crawled ZIM"
    if not job.get('url'):
        return None, "crawl settings not recorded"
    c = _conn()
    if c.execute("SELECT 1 FROM zim_update_runs WHERE zim_key = ? AND status = 'running'", (key,)).fetchone():
        return None, "a run for this ZIM is already in progress"
    if c.execute("SELECT 1 FROM scrape_jobs WHERE url = ? AND status IN ('pending','scraping','registering')",
                 (job['url'],)).fetchone():
        return None, "a crawl of this URL is already queued or running"
    run_id = _run_create(key, item['title'], 'recrawl', trigger, 'crawl queued',
                         old_version=item['label'], old_filename=item['filename'])
    try:
        _guard_item(item)
        size = item['size'] or 0
        prev_size = sum(os.path.getsize(os.path.join(previous_dir(), fn)) for _, fn in previous_files(key))
        free = _disk_check(int((size + prev_size) * MARGIN))
        _run_log(run_id, f"disk ok: need about {_fmt_bytes((size + prev_size) * MARGIN)}, free {_fmt_bytes(free)}")
        cur = c.execute(
            "INSERT INTO scrape_jobs (url, title, language, category, status, crawl_mode, "
            "additional_reject_patterns, skip_default_patterns, replace_zim_filename, update_run_id) "
            "VALUES (?, ?, ?, ?, 'pending', 'zimit', ?, ?, ?, ?)",
            (job['url'], job.get('title'), job.get('language'), job.get('category'),
             job.get('additional_reject_patterns'), job.get('skip_default_patterns') or 0,
             item['filename'], run_id))
        c.commit()
        _run_set(run_id, scrape_job_id=cur.lastrowid)
        _run_log(run_id, f"queued crawl job #{cur.lastrowid} for {job['url']}")
    except Skip as e:
        _run_finish(run_id, 'skipped', str(e))
    except Exception as e:
        _run_finish(run_id, 'failed', f"{type(e).__name__}: {e}"[:300])
    return run_id, None


def finish_recrawl(job, new_filename):
    """Called by the scraper runner when a re-crawl job produced new_filename (already in
    kiwix.dir). Swaps it in. True when handled (job complete); False when the scraper should
    register the new ZIM the normal way (the run is already finished as skipped/failed)."""
    run_id = job.get('update_run_id')
    old_filename = job.get('replace_zim_filename')
    if not run_id or not old_filename:
        return False
    key, ver, _ = parse_name(new_filename)
    with _RUN_LOCK:
        try:
            _run_set(run_id, new_filename=new_filename, new_version=vlabel(new_filename))
            _run_log(run_id, f"crawl finished: {new_filename}")
            old = next((i for i in inventory().values() if i['filename'] == old_filename), None)
            if not old:
                raise Skip("the ZIM to replace is no longer in the library; new crawl registered separately")
            _guard_item(old)
            _preflight_index(old)
            _file_perms(os.path.join(kcfg()['dir'], new_filename))
            res = _swap(run_id, key, new_filename, old, undo_new='keep')
            c = _conn()
            c.execute("UPDATE scrape_jobs SET zim_source_id = ? WHERE id = ?", (res['new_id'], job['id']))
            c.commit()
            msg = f"re-crawled, {old['label']} replaced by {vlabel(new_filename)}, previous copy kept"
            if res['ingest_queued']:
                msg += ", ingest queued"
            _run_finish(run_id, 'updated', msg)
            return True
        except Skip as e:
            _run_finish(run_id, 'skipped', f"{e}; new crawl registered as a separate ZIM")
        except Exception as e:
            logger.exception("finish_recrawl")
            _run_finish(run_id, 'failed', f"{type(e).__name__}: {e}"[:300])
    return False


def recrawl_failed(job, error):
    run_id = job.get('update_run_id')
    if run_id:
        _run_finish(run_id, 'failed', f"crawl {'cancelled' if error == 'cancelled' else 'failed'}: {error}"[:300])


# ── schedules ────────────────────────────────────────────────────

def _month_candidate(y, m, day, hh, mm, tz):
    last = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return datetime(y, m, min(max(day, 1), last), hh, mm, tzinfo=tz)


def compute_next(s, after):
    """Next run (UTC datetime) strictly after `after` for schedule dict s."""
    tz = _tz()
    now = after.astimezone(tz)
    hh, mm = (int(x) for x in (s.get('run_time') or '02:00').split(':'))
    if s.get('frequency') == 'monthly':
        n = max(1, int(s.get('interval_n') or 1))
        y, m = now.year, now.month
        for _ in range(120):
            cand = _month_candidate(y, m, int(s.get('monthday') or 1), hh, mm, tz)
            if cand > now:
                return cand.astimezone(timezone.utc)
            m += n
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
        raise ValueError("no next run")
    wd = int(s.get('weekday') or 0)
    for add in range(0, 8):
        day = (now + timedelta(days=add))
        cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        if cand.weekday() == wd and cand > now:
            return cand.astimezone(timezone.utc)
    raise ValueError("no next run")


def _validate_schedule(data):
    freq = data.get('frequency', 'weekly')
    if freq not in ('weekly', 'monthly'):
        raise ValueError("frequency must be weekly or monthly")
    run_time = str(data.get('run_time') or '02:00')
    if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', run_time):
        raise ValueError("time must be HH:MM")
    s = {
        'enabled': 1 if data.get('enabled') else 0,
        'frequency': freq,
        'interval_n': int(data.get('interval_n') or 1),
        'weekday': int(data.get('weekday') if data.get('weekday') is not None else 0),
        'monthday': int(data.get('monthday') or 1),
        'run_time': run_time,
    }
    if not 1 <= s['interval_n'] <= 24:
        raise ValueError("interval must be 1 to 24 months")
    if not 0 <= s['weekday'] <= 6:
        raise ValueError("weekday must be 0 (Monday) to 6 (Sunday)")
    if not 1 <= s['monthday'] <= 31:
        raise ValueError("day of month must be 1 to 31")
    return s


def set_schedule(key, data):
    if key not in inventory():
        raise ValueError(f"{key}: not in the library")
    s = _validate_schedule(data)
    s['next_run_at'] = _iso(compute_next(s, _utcnow())) if s['enabled'] else None
    s['updated_at'] = _iso(_utcnow())
    s['zim_key'] = key
    c = _conn()
    cols = list(s)
    c.execute(f"INSERT OR REPLACE INTO zim_update_schedules ({', '.join(cols)}) "
              f"VALUES ({', '.join('?' * len(cols))})", [s[k] for k in cols])
    c.commit()
    return s


def scheduler_tick():
    """One scheduler pass: start due runs, advance their schedules, daily catalog check."""
    c = _conn()
    now = _utcnow()
    due = [dict(r) for r in c.execute(
        "SELECT * FROM zim_update_schedules WHERE enabled = 1 AND next_run_at IS NOT NULL "
        "AND next_run_at <= ? ORDER BY next_run_at", (_iso(now),)).fetchall()]
    items = inventory() if due else {}
    for s in due:
        key = s['zim_key']
        item = items.get(key)
        try:
            due_at = _parse_iso(s['next_run_at'])
            nxt = _iso(compute_next(s, now))
        except Exception:
            logger.exception("bad schedule for %s", key)
            continue
        if not item:
            c.execute("UPDATE zim_update_schedules SET next_run_at = ? WHERE zim_key = ?", (nxt, key))
            c.commit()
            continue
        if now - due_at > MISSED_AFTER:
            rid = _run_create(key, item['title'], 'update', 'schedule', 'missed',
                              old_version=item['label'], old_filename=item['filename'])
            _run_finish(rid, 'skipped', f"missed schedule ({s['next_run_at']}): service was not running")
            c.execute("UPDATE zim_update_schedules SET next_run_at = ? WHERE zim_key = ?", (nxt, key))
            c.commit()
            continue
        is_crawl = bool(_crawl_job_for(item['filename']))
        run_id, err = (start_recrawl if is_crawl else start_update)(key, 'schedule')
        if err and err in ('another update is running', 'a run for this ZIM is already in progress'):
            continue   # busy: stay due, retry next tick
        if err:
            rid = _run_create(key, item['title'], 'recrawl' if is_crawl else 'update', 'schedule', err,
                              old_version=item['label'], old_filename=item['filename'])
            _run_finish(rid, 'skipped', err)
        c.execute("UPDATE zim_update_schedules SET next_run_at = ? WHERE zim_key = ?", (nxt, key))
        c.commit()

    # daily catalog check: flags 'update available' for ZIMs without auto-update
    try:
        tz = _tz()
        local = now.astimezone(tz)
        hh, mm = (int(x) for x in kcfg()['daily_check_time'].split(':'))
        today = local.strftime('%Y-%m-%d')
        last = c.execute("SELECT v FROM zim_update_state WHERE k = 'daily_check_date'").fetchone()
        if (local.hour, local.minute) >= (hh, mm) and (not last or last['v'] != today):
            c.execute("INSERT OR REPLACE INTO zim_update_state (k, v) VALUES ('daily_check_date', ?)", (today,))
            c.commit()
            threading.Thread(target=lambda: check_all(skip_auto_update=True), daemon=True,
                             name='zim-daily-check').start()
    except Exception:
        logger.exception("daily check")


def _startup_cleanup():
    d = kcfg()['dir']
    try:
        for fn in os.listdir(d):
            if fn.startswith(STALE_TMP_PREFIX) and fn.endswith('.tmp'):
                _try_remove(os.path.join(d, fn))
    except OSError:
        pass
    c = _conn()
    for r in c.execute("SELECT id, action, scrape_job_id FROM zim_update_runs WHERE status = 'running'").fetchall():
        job = None
        if r['action'] == 'recrawl' and r['scrape_job_id']:
            job = c.execute("SELECT status FROM scrape_jobs WHERE id = ?", (r['scrape_job_id'],)).fetchone()
        if job and job['status'] in ('pending', 'scraping', 'registering'):
            continue    # the scraper will pick it up again
        _run_finish(r['id'], 'failed', 'interrupted (service restarted)')


def scheduler_loop(stop_event):
    logger.info("ZIM update scheduler started (tz %s)", kcfg()['timezone'])
    try:
        _startup_cleanup()
    except Exception:
        logger.exception("startup cleanup")
    while not stop_event.is_set():
        try:
            scheduler_tick()
        except Exception:
            logger.exception("scheduler tick")
        stop_event.wait(60)
    logger.info("ZIM update scheduler stopped")


def start_scheduler(stop_event):
    t = threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True, name='zim-updates')
    t.start()
    return t


# ── calendar + run detail ────────────────────────────────────────

def calendar(month):
    """Events for a month 'YYYY-MM': past/current runs and projected scheduled runs."""
    y, m = (int(x) for x in month.split('-'))
    tz = _tz()
    start = datetime(y, m, 1, tzinfo=tz)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
    c = _conn()
    items = inventory()
    events = []
    for r in c.execute("SELECT * FROM zim_update_runs WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
                       (_iso(start), _iso(end))).fetchall():
        t = _parse_iso(r['started_at']).astimezone(tz)
        events.append({'kind': 'run', 'date': t.strftime('%Y-%m-%d'), 'time': t.strftime('%H:%M'),
                       'run_id': r['id'], 'key': r['zim_key'], 'title': r['title'] or r['zim_key'],
                       'action': r['action'], 'trigger': r['trigger'], 'status': r['status'],
                       'message': r['message']})
    now = _utcnow()
    for s in c.execute("SELECT * FROM zim_update_schedules WHERE enabled = 1").fetchall():
        s = dict(s)
        item = items.get(s['zim_key'])
        if not item:
            continue
        try:
            t = _parse_iso(s['next_run_at']) if s['next_run_at'] else compute_next(s, now)
            n = 0
            while t < end.astimezone(timezone.utc) and n < 200:
                if t >= max(start.astimezone(timezone.utc), now):
                    lt = t.astimezone(tz)
                    events.append({'kind': 'scheduled', 'date': lt.strftime('%Y-%m-%d'),
                                   'time': lt.strftime('%H:%M'), 'run_id': None, 'key': s['zim_key'],
                                   'title': item['title'], 'is_crawl': bool(_crawl_job_for(item['filename'])),
                                   'action': 'update', 'trigger': 'schedule', 'status': 'scheduled',
                                   'message': None})
                t = compute_next(s, t)
                n += 1
        except Exception:
            logger.exception("calendar projection for %s", s['zim_key'])
    events.sort(key=lambda e: (e['date'], e['time']))
    return {'month': month, 'timezone': kcfg()['timezone'], 'today': now.astimezone(tz).strftime('%Y-%m-%d'),
            'events': events}


def get_run(run_id):
    r = _conn().execute("SELECT * FROM zim_update_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None
