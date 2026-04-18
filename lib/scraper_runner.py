"""
RECON Scraper Runner

Daemon loop that processes scrape jobs: crawl → zimwriterfs → kiwix-manage.
Supports two crawl backends:
  - wget (static sites) — default
  - SingleFile CLI (JS-rendered sites) — browser mode

Pre-flight detection automatically chooses the right backend unless
crawl_mode is pre-set on the job.

Public entry point: scraper_loop(stop_event, config).

Config section: scraper (workspace, output_dir, rate_limit_delay, preflight, singlefile)
DB table: scrape_jobs (status flow: pending → scraping → packaging → complete)
"""
import glob as _glob
import json as _json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from .utils import setup_logging
from .status import StatusDB

logger = setup_logging('recon.scraper_runner')


def scraper_loop(stop_event, config):
    """Daemon loop: poll for pending scrape jobs, execute pipeline."""
    scraper_cfg = config.get('scraper', {})
    poll_interval = scraper_cfg.get('poll_interval', 300)

    logger.info("Scraper runner started")

    while not stop_event.is_set():
        db = StatusDB()
        job = db.get_pending_scrape_job()
        if job:
            try:
                _process_job(job, config, stop_event)
            except Exception as e:
                logger.error(f"Scraper job {job['id']} unexpected error: {e}", exc_info=True)
                try:
                    db.update_scrape_job(job['id'],
                                         status='failed',
                                         error_message=str(e)[:1000],
                                         subprocess_pid=None,
                                         completed_at=_now())
                except Exception:
                    pass
        else:
            stop_event.wait(poll_interval)

    logger.info("Scraper runner stopped")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sanitize_domain(url):
    """Extract and sanitize domain from URL for use in filenames."""
    parsed = urlparse(url)
    domain = parsed.hostname or 'unknown'
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


def _sanitize_filename(s):
    """Sanitize a string for safe filename use."""
    return re.sub(r'[^a-zA-Z0-9._-]', '_', s)


def _check_cancelled(db, job_id):
    """Check if a job has been cancelled in the DB."""
    job = db.get_scrape_job(job_id)
    return job and job['status'] == 'cancelled'


def _kill_process(proc, timeout=5):
    """Gracefully terminate a subprocess, force kill if needed."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _count_html_files(directory):
    """Count HTML files in a directory tree."""
    count = 0
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(('.html', '.htm')):
                count += 1
    return count


def _find_welcome_page(content_dir, domain):
    """Find the welcome page (index.html) in the wget mirror."""
    domain_dir = None
    for entry in os.listdir(content_dir):
        entry_path = os.path.join(content_dir, entry)
        if os.path.isdir(entry_path):
            domain_dir = entry_path
            break

    if not domain_dir:
        return None, content_dir

    for candidate in ['index.html', 'index.htm']:
        path = os.path.join(domain_dir, candidate)
        if os.path.isfile(path):
            return candidate, domain_dir

    for root, dirs, files in os.walk(domain_dir):
        for f in sorted(files):
            if f.lower().endswith(('.html', '.htm')):
                rel = os.path.relpath(os.path.join(root, f), domain_dir)
                return rel, domain_dir

    return 'index.html', domain_dir


def _create_placeholder_illustration(path):
    """Create a 48x48 placeholder PNG for zimwriterfs --illustration."""
    from PIL import Image
    img = Image.new('RGB', (48, 48), color=(40, 192, 232))
    img.save(path, 'PNG')


# ── Crawl mode detection ──────────────────────────────────────────


def _get_chromium_path(config):
    """Auto-detect Chromium from Playwright's cache, or use config override."""
    configured = config.get('scraper', {}).get('singlefile', {}).get('chromium_path', '')
    if configured and os.path.isfile(configured):
        return configured
    # Playwright stores Chromium — check both root and user caches
    search_paths = [
        os.path.expanduser('~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome'),
        '/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome',
    ]
    for pattern in search_paths:
        matches = sorted(_glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def _detect_crawl_mode(url, config):
    """
    Pre-flight detection: determine whether a URL needs a browser to crawl.

    Returns (mode, resolved_url) where mode is 'static', 'browser', or 'redirect'.
    'redirect' means the URL redirected to a different domain (parking page etc.);
    resolved_url will be the final browser URL in that case.
    """
    preflight_cfg = config.get('scraper', {}).get('preflight', {})
    if not preflight_cfg.get('enabled', True):
        return 'static', url

    timeout = preflight_cfg.get('timeout', 30)
    min_static = preflight_cfg.get('min_static_size', 5120)
    min_browser = preflight_cfg.get('min_browser_size', 20480)
    spa_markers = preflight_cfg.get('spa_markers', ['div#root', 'div#app', 'div#__next'])

    input_domain = urlparse(url).hostname or ''
    if input_domain.startswith('www.'):
        input_domain = input_domain[4:]

    # Step 1: wget single-page fetch
    wget_html = ''
    wget_size = 0
    try:
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as tmp:
            tmp_path = tmp.name
        result = subprocess.run(
            ['wget', '-q', '-O', tmp_path, '--timeout=30', '--tries=1', url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        if os.path.isfile(tmp_path):
            wget_size = os.path.getsize(tmp_path)
            with open(tmp_path, 'r', errors='replace') as f:
                wget_html = f.read()
        os.unlink(tmp_path)
    except Exception as e:
        logger.debug(f"Preflight wget failed for {url}: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    # Step 2: Playwright headless fetch
    browser_html = ''
    browser_size = 0
    browser_url = url
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            page = browser.new_page()
            page.goto(url, wait_until='networkidle', timeout=timeout * 1000)
            browser_url = page.url
            browser_html = page.content()
            browser_size = len(browser_html.encode('utf-8'))
            browser.close()
    except Exception as e:
        logger.debug(f"Preflight Playwright failed for {url}: {e}")
        # If Playwright fails entirely, fall back to static
        return 'static', url

    # Step 3: Decision logic
    browser_domain = urlparse(browser_url).hostname or ''
    if browser_domain.startswith('www.'):
        browser_domain = browser_domain[4:]

    # Check for cross-domain redirect (parking page detection)
    if browser_domain and input_domain and browser_domain != input_domain:
        logger.info(f"Preflight: {url} redirected to different domain {browser_domain}, mode=redirect")
        return 'redirect', browser_url

    # Check size disparity: small wget + large browser = JS-rendered
    if wget_size < min_static and browser_size > min_browser:
        logger.info(f"Preflight: {url} wget={wget_size}B browser={browser_size}B, mode=browser")
        return 'browser', url

    # Check for SPA shell markers in wget HTML
    if wget_html:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(wget_html, 'html.parser')
            for marker in spa_markers:
                # marker is like 'div#root' — split tag and id
                parts = marker.split('#', 1)
                tag = parts[0] if parts[0] else 'div'
                elem_id = parts[1] if len(parts) > 1 else None
                elem = soup.find(tag, id=elem_id) if elem_id else soup.find(tag)
                if elem:
                    text_content = elem.get_text(strip=True)
                    if len(text_content) < 100:
                        logger.info(f"Preflight: {url} has SPA marker {marker} with {len(text_content)} chars text, mode=browser")
                        return 'browser', url
        except Exception as e:
            logger.debug(f"Preflight SPA marker check failed: {e}")

    logger.info(f"Preflight: {url} wget={wget_size}B browser={browser_size}B, mode=static")
    return 'static', url


# ── Crawl backends ────────────────────────────────────────────────


def _crawl_wget(job, url, site_dir, config, stop_event, db):
    """
    wget mirror crawl backend.
    Returns (page_count, error_msg) — error_msg is None on success, 'cancelled' on cancel.
    """
    job_id = job['id']
    scraper_cfg = config.get('scraper', {})
    rate_limit_delay = scraper_cfg.get('rate_limit_delay', 0.5)
    user_agent = scraper_cfg.get('user_agent', 'Mozilla/5.0 (compatible; RECON/1.0)')
    keep_workspace = scraper_cfg.get('keep_workspace_on_failure', True)
    workspace = os.path.dirname(site_dir)

    # Build reject-regex from config defaults + per-job overrides
    reject_patterns = []
    skip_defaults = bool(job.get('skip_default_patterns'))
    if not skip_defaults:
        reject_patterns.extend(scraper_cfg.get('default_reject_patterns', []))
    additional_raw = job.get('additional_reject_patterns')
    if additional_raw:
        try:
            additional = _json.loads(additional_raw) if isinstance(additional_raw, str) else additional_raw
            if isinstance(additional, list):
                reject_patterns.extend(additional)
        except (ValueError, TypeError):
            pass

    wget_cmd = [
        'wget', '--mirror', '--convert-links', '--adjust-extension',
        '--page-requisites', '--no-parent',
        '--restrict-file-names=windows',
        f'--wait={rate_limit_delay}', '--random-wait',
        f'--user-agent={user_agent}',
        f'--directory-prefix={site_dir}',
        '--timeout=30', '--tries=3',
    ]
    if reject_patterns:
        combined_regex = '|'.join(f'({p})' for p in reject_patterns)
        wget_cmd.extend([f'--reject-regex={combined_regex}'])
        logger.info(f"Job {job_id}: reject-regex has {len(reject_patterns)} patterns")
    wget_cmd.append(url)

    logger.info(f"Job {job_id}: wget mirror starting")
    wget_log = os.path.join(workspace, 'wget.log')
    try:
        with open(wget_log, 'w') as log_fh:
            proc = subprocess.Popen(
                wget_cmd,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        db.update_scrape_job(job_id, subprocess_pid=proc.pid)

        while proc.poll() is None:
            if stop_event.is_set() or _check_cancelled(db, job_id):
                _kill_process(proc)
                return 0, 'cancelled'
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        db.update_scrape_job(job_id, subprocess_pid=None)

        if stop_event.is_set() or _check_cancelled(db, job_id):
            return 0, 'cancelled'

        # wget returns 8 for some server errors but may still have useful content
        if proc.returncode not in (0, 4, 6, 8):
            output = ''
            try:
                with open(wget_log, 'r') as f:
                    f.seek(max(0, os.path.getsize(wget_log) - 500))
                    output = f.read()
            except Exception:
                pass
            return 0, f"wget failed with code {proc.returncode}: {output[-500:]}"

    except Exception as e:
        return 0, f"wget error: {e}"

    page_count = _count_html_files(site_dir)
    logger.info(f"Job {job_id}: wget complete, {page_count} HTML pages found")

    if page_count == 0:
        return 0, 'wget produced no HTML files'

    return page_count, None


def _crawl_singlefile(job, url, site_dir, config, stop_event, db):
    """
    SingleFile CLI crawl backend for JS-rendered sites.
    Returns (page_count, error_msg) — error_msg is None on success, 'cancelled' on cancel.
    """
    job_id = job['id']
    scraper_cfg = config.get('scraper', {})
    sf_cfg = scraper_cfg.get('singlefile', {})
    keep_workspace = scraper_cfg.get('keep_workspace_on_failure', True)
    workspace = os.path.dirname(site_dir)

    executable = sf_cfg.get('executable', 'single-file')
    chromium_path = _get_chromium_path(config)
    crawl_max_depth = sf_cfg.get('crawl_max_depth', 10)
    crawl_delay = sf_cfg.get('crawl_delay', 2)

    if not chromium_path:
        return 0, 'Chromium not found — cannot use browser crawl mode'

    # SingleFile outputs into site_dir/<domain>/ to match wget's structure
    domain = _sanitize_domain(url)
    output_dir = os.path.join(site_dir, domain)
    os.makedirs(output_dir, exist_ok=True)

    sf_cmd = [
        executable,
        '--crawl-links=true',
        '--crawl-inner-links-only=true',
        f'--crawl-max-depth={crawl_max_depth}',
        f'--crawl-delay={crawl_delay * 1000}',  # milliseconds
        f'--browser-executable-path={chromium_path}',
        '--browser-headless=true',
        '--browser-args=["--no-sandbox","--disable-dev-shm-usage"]',
        f'--output-directory={output_dir}',
        url,
    ]

    logger.info(f"Job {job_id}: SingleFile crawl starting (depth={crawl_max_depth}, delay={crawl_delay}s)")
    sf_log = os.path.join(workspace, 'singlefile.log')
    try:
        with open(sf_log, 'w') as log_fh:
            proc = subprocess.Popen(
                sf_cmd,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        db.update_scrape_job(job_id, subprocess_pid=proc.pid)

        while proc.poll() is None:
            if stop_event.is_set() or _check_cancelled(db, job_id):
                _kill_process(proc)
                return 0, 'cancelled'
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        db.update_scrape_job(job_id, subprocess_pid=None)

        if stop_event.is_set() or _check_cancelled(db, job_id):
            return 0, 'cancelled'

        if proc.returncode != 0:
            output = ''
            try:
                with open(sf_log, 'r') as f:
                    f.seek(max(0, os.path.getsize(sf_log) - 500))
                    output = f.read()
            except Exception:
                pass
            # SingleFile may still produce some files even with non-zero exit
            page_count = _count_html_files(site_dir)
            if page_count == 0:
                return 0, f"SingleFile failed with code {proc.returncode}: {output[-500:]}"
            logger.warning(f"Job {job_id}: SingleFile exited {proc.returncode} but produced {page_count} pages, continuing")

    except Exception as e:
        return 0, f"SingleFile error: {e}"

    # If no index.html exists, rename the first HTML file to index.html
    index_path = os.path.join(output_dir, 'index.html')
    if not os.path.isfile(index_path):
        for f in sorted(os.listdir(output_dir)):
            if f.lower().endswith(('.html', '.htm')):
                src = os.path.join(output_dir, f)
                os.rename(src, index_path)
                logger.info(f"Job {job_id}: renamed {f} → index.html")
                break

    page_count = _count_html_files(site_dir)
    logger.info(f"Job {job_id}: SingleFile complete, {page_count} HTML pages found")

    if page_count == 0:
        return 0, 'SingleFile produced no HTML files'

    return page_count, None


# ── Main job pipeline ─────────────────────────────────────────────


def _process_job(job, config, stop_event):
    """Execute the full scrape pipeline for a single job."""
    db = StatusDB()
    job_id = job['id']
    url = job['url']
    title = job.get('title') or _sanitize_domain(url)
    language = job.get('language') or config.get('scraper', {}).get('default_language', 'eng')
    category = job.get('category') or ''

    scraper_cfg = config.get('scraper', {})
    workspace_root = scraper_cfg.get('workspace', '/opt/recon/data/scraper')
    output_dir = scraper_cfg.get('output_dir', '/mnt/kiwix')
    keep_workspace = scraper_cfg.get('keep_workspace_on_failure', True)

    workspace = os.path.join(workspace_root, str(job_id))
    site_dir = os.path.join(workspace, 'site')
    os.makedirs(site_dir, exist_ok=True)

    domain = _sanitize_domain(url)
    date_tag = datetime.now().strftime('%Y-%m')
    zim_filename = f"{_sanitize_filename(domain)}_{language}_{date_tag}.zim"
    zim_path = os.path.join(output_dir, zim_filename)

    logger.info(f"Job {job_id}: starting scrape of {url}")
    db.update_scrape_job(job_id,
                         status='scraping',
                         workspace_path=workspace,
                         started_at=_now())

    # ── Phase 0: Pre-flight mode detection ─────────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return

    pre_set = job.get('crawl_mode')
    if pre_set:
        crawl_mode, resolved_url = pre_set, url
        logger.info(f"Job {job_id}: using pre-set crawl_mode={crawl_mode}")
    else:
        crawl_mode, resolved_url = _detect_crawl_mode(url, config)
        logger.info(f"Job {job_id}: detected crawl_mode={crawl_mode}")

    db.update_scrape_job(job_id, crawl_mode=crawl_mode)

    # If redirect detected, update domain/filename to match resolved URL
    if crawl_mode == 'redirect' and resolved_url != url:
        logger.info(f"Job {job_id}: URL resolved from {url} → {resolved_url}")
        domain = _sanitize_domain(resolved_url)
        zim_filename = f"{_sanitize_filename(domain)}_{language}_{date_tag}.zim"
        zim_path = os.path.join(output_dir, zim_filename)

    # ── Phase A: Crawl (dispatch to backend) ────────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return

    if crawl_mode == 'browser':
        page_count, error = _crawl_singlefile(job, resolved_url, site_dir, config, stop_event, db)
    else:  # 'static' or 'redirect'
        page_count, error = _crawl_wget(job, resolved_url, site_dir, config, stop_event, db)

    if error == 'cancelled':
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return
    elif error:
        db.update_scrape_job(job_id,
                             status='failed',
                             error_message=error,
                             subprocess_pid=None,
                             completed_at=_now())
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        return

    db.update_scrape_job(job_id, page_count=page_count)

    # ── Phase B: Prepare zimwriterfs inputs ────────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return

    welcome_page, content_dir = _find_welcome_page(site_dir, domain)
    if welcome_page is None:
        welcome_page = 'index.html'

    illustration_path = os.path.join(workspace, 'illustration.png')
    _create_placeholder_illustration(illustration_path)
    illust_dest = os.path.join(content_dir, 'illustration.png')
    shutil.copy2(illustration_path, illust_dest)

    description = f"Mirror of {domain}"
    if category:
        description = f"{category} — mirror of {domain}"

    logger.info(f"Job {job_id}: packaging ZIM (welcome={welcome_page}, content_dir={content_dir})")
    db.update_scrape_job(job_id, status='packaging')

    # ── Phase C: zimwriterfs ───────────────────────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return

    zim_name = _sanitize_filename(domain)
    long_description = f"Offline mirror of {resolved_url} created by RECON web scraper"

    zim_cmd = [
        'zimwriterfs',
        f'--welcome={welcome_page}',
        f'--illustration=illustration.png',
        f'--language={language}',
        f'--title={title}',
        f'--description={description[:80]}',
        f'--longDescription={long_description[:4096]}',
        f'--name={zim_name}',
        f'--creator={domain}',
        '--publisher=RECON',
        content_dir,
        zim_path,
    ]

    zim_log = os.path.join(workspace, 'zimwriterfs.log')
    try:
        with open(zim_log, 'w') as log_fh:
            proc = subprocess.Popen(
                zim_cmd,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        db.update_scrape_job(job_id, subprocess_pid=proc.pid)

        while proc.poll() is None:
            if stop_event.is_set() or _check_cancelled(db, job_id):
                _kill_process(proc)
                _handle_cancel(db, job_id, workspace, keep_workspace)
                return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        db.update_scrape_job(job_id, subprocess_pid=None)

        if stop_event.is_set() or _check_cancelled(db, job_id):
            _handle_cancel(db, job_id, workspace, keep_workspace)
            return

        if proc.returncode != 0:
            output = ''
            try:
                with open(zim_log, 'r') as f:
                    f.seek(max(0, os.path.getsize(zim_log) - 500))
                    output = f.read()
            except Exception:
                pass
            raise RuntimeError(f"zimwriterfs failed with code {proc.returncode}: {output[-500:]}")

    except RuntimeError:
        raise
    except Exception as e:
        db.update_scrape_job(job_id,
                             status='failed',
                             error_message=f"zimwriterfs error: {e}",
                             subprocess_pid=None,
                             completed_at=_now())
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        return

    if not os.path.isfile(zim_path):
        db.update_scrape_job(job_id,
                             status='failed',
                             error_message='zimwriterfs produced no output file',
                             completed_at=_now())
        return

    logger.info(f"Job {job_id}: ZIM created at {zim_path}")

    # ── Phase D: kiwix-manage + registration ───────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id, workspace, keep_workspace)
        return

    kiwix_manage = shutil.which('kiwix-manage') or '/opt/recon/bin/kiwix-manage'
    library_xml = '/mnt/kiwix/library.xml'

    try:
        subprocess.run(
            [kiwix_manage, library_xml, 'add', zim_path],
            capture_output=True, text=True, timeout=30
        )
        logger.info(f"Job {job_id}: registered with kiwix-serve library")
    except Exception as e:
        logger.warning(f"Job {job_id}: kiwix-manage add failed: {e}")

    try:
        result = subprocess.run(['pidof', 'kiwix-serve'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split()[0])
            os.kill(pid, signal.SIGHUP)
            logger.info(f"Job {job_id}: sent SIGHUP to kiwix-serve (pid {pid})")
    except Exception as e:
        logger.warning(f"Job {job_id}: failed to signal kiwix-serve: {e}")

    zim_source_id = None
    try:
        from .zim_monitor import scan_zims
        scan_zims()
        conn = db._get_conn()
        row = conn.execute(
            "SELECT id FROM zim_sources WHERE zim_filename = ?", (zim_filename,)
        ).fetchone()
        if row:
            zim_source_id = row['id']
            logger.info(f"Job {job_id}: linked to zim_source_id={zim_source_id}")
    except Exception as e:
        logger.warning(f"Job {job_id}: scan_zims failed: {e}")

    try:
        shutil.rmtree(workspace, ignore_errors=True)
    except Exception:
        pass

    db.update_scrape_job(job_id,
                         status='complete',
                         zim_filename=zim_filename,
                         zim_source_id=zim_source_id,
                         completed_at=_now())

    logger.info(f"Job {job_id}: complete — {zim_filename} ({page_count} pages, mode={crawl_mode})")


def _handle_cancel(db, job_id, workspace, keep_workspace):
    """Handle job cancellation: clean up and update status."""
    logger.info(f"Job {job_id}: cancelled")
    db.update_scrape_job(job_id,
                         status='cancelled',
                         subprocess_pid=None,
                         completed_at=_now())
    if not keep_workspace:
        shutil.rmtree(workspace, ignore_errors=True)
