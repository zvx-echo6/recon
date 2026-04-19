"""
RECON Scraper Runner

Daemon loop that processes scrape jobs: crawl via Zimit → kiwix-manage.
Zimit (openZIM Docker crawler) handles all site types and produces ZIM
files directly — no separate zimwriterfs step needed.

Public entry point: scraper_loop(stop_event, config).

Config section: scraper (output_dir, docker_image, docker_workers, poll_interval)
DB table: scrape_jobs (status flow: pending → scraping → registering → complete)
"""
import glob as _glob
import os
import re
import shutil
import signal
import subprocess
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

    # Clean up any orphan Zimit containers from a previous crash
    _cleanup_orphan_containers()

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


def _cleanup_orphan_containers():
    """Remove any leftover recon-scraper-* Docker containers from a previous crash."""
    try:
        result = subprocess.run(
            ['docker', 'ps', '-a', '--filter', 'name=recon-scraper-', '--format', '{{.Names}}'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            for name in result.stdout.strip().split('\n'):
                name = name.strip()
                if name:
                    subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)
                    logger.info(f"Cleaned up orphan container: {name}")
    except Exception as e:
        logger.warning(f"Orphan container cleanup failed: {e}")


# ── Zimit crawl backend ──────────────────────────────────────────


def _crawl_zimit(job, config, stop_event, db):
    """
    Crawl a URL using Zimit (openZIM Docker crawler).

    Returns (page_count, zim_filename, error_msg).
    On success: (count, filename, None)
    On failure: (0, None, error_string)
    """
    job_id = job['id']
    url = job['url']
    title = job.get('title') or _sanitize_domain(url)
    language = job.get('language') or config.get('scraper', {}).get('default_language', 'eng')
    category = job.get('category') or ''

    scraper_cfg = config.get('scraper', {})
    output_dir = scraper_cfg.get('output_dir', '/mnt/kiwix')
    docker_image = scraper_cfg.get('docker_image', 'ghcr.io/openzim/zimit')
    docker_workers = scraper_cfg.get('docker_workers', 2)

    domain = _sanitize_domain(url)
    date_tag = datetime.now().strftime('%Y-%m')
    container_name = f'recon-scraper-{job_id}'
    tmp_dir = os.path.join(output_dir, f'.zimit-tmp-{job_id}')

    # Clean up any pre-existing container with same name (retry scenario)
    subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, timeout=10)

    os.makedirs(tmp_dir, exist_ok=True)

    description = f"Mirror of {domain}"
    if category:
        description = f"{category} — mirror of {domain}"

    docker_cmd = [
        'docker', 'run',
        '--name', container_name,
        '-v', f'{tmp_dir}:/output',
        docker_image,
        'zimit',
        '--seeds', url,
        '--name', _sanitize_filename(domain),
        '--zim-lang', language,
        '--title', title,
        '--description', description[:80],
        '--output', '/output',
        '-w', str(docker_workers),
    ]

    logger.info(f"Job {job_id}: Zimit crawl starting — {url}")
    try:
        proc = subprocess.Popen(
            docker_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        db.update_scrape_job(job_id, subprocess_pid=proc.pid)

        last_progress_check = 0
        while proc.poll() is None:
            if stop_event.is_set() or _check_cancelled(db, job_id):
                # Stop the Docker container
                subprocess.run(['docker', 'rm', '-f', container_name],
                               capture_output=True, timeout=10)
                _kill_process(proc)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return 0, None, 'cancelled'

            # Check progress every 30s via docker logs
            now = time.time()
            if now - last_progress_check >= 30:
                last_progress_check = now
                try:
                    log_result = subprocess.run(
                        ['docker', 'logs', '--tail', '20', container_name],
                        capture_output=True, text=True, timeout=10
                    )
                    if log_result.returncode == 0:
                        # Browsertrix logs JSON with "crawled":N — check both stdout and stderr
                        log_text = log_result.stdout or log_result.stderr or ''
                        lines = log_text.strip().split('\n')
                        for line in reversed(lines):
                            match = re.search(r'"crawled":(\d+)', line)
                            if match:
                                count = int(match.group(1))
                                if count > 0:
                                    db.update_scrape_job(job_id, page_count=count)
                                break
                except Exception:
                    pass

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        db.update_scrape_job(job_id, subprocess_pid=None)

        if stop_event.is_set() or _check_cancelled(db, job_id):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return 0, None, 'cancelled'

        if proc.returncode != 0:
            # Capture last 50 lines of docker logs for error context
            error_msg = f"Zimit exited with code {proc.returncode}"
            try:
                log_result = subprocess.run(
                    ['docker', 'logs', '--tail', '50', container_name],
                    capture_output=True, text=True, timeout=10
                )
                log_text = (log_result.stderr or log_result.stdout or '').strip()
                if log_text:
                    # Take last 500 chars
                    error_msg += f": {log_text[-500:]}"
            except Exception:
                pass
            # Remove container (no --rm flag, so we clean up manually)
            subprocess.run(['docker', 'rm', '-f', container_name],
                           capture_output=True, timeout=10)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return 0, None, error_msg

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0, None, f"Zimit error: {e}"

    # Remove container (no --rm flag, so we clean up manually after getting logs)
    subprocess.run(['docker', 'rm', '-f', container_name],
                   capture_output=True, timeout=10)

    # Find the output ZIM file
    zim_files = _glob.glob(os.path.join(tmp_dir, '*.zim'))
    if not zim_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0, None, 'Zimit produced no ZIM file'

    src_zim = zim_files[0]  # Should be exactly one

    # Get page count from file size as rough estimate if we don't have one
    page_count = 0
    try:
        job_state = db.get_scrape_job(job_id)
        page_count = job_state.get('page_count') or 0
    except Exception:
        pass

    # Rename to final location
    zim_filename = f"{_sanitize_filename(domain)}_{language}_{date_tag}_{job_id}.zim"
    zim_path = os.path.join(output_dir, zim_filename)
    try:
        shutil.move(src_zim, zim_path)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0, None, f"Failed to move ZIM to output dir: {e}"

    shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info(f"Job {job_id}: Zimit complete — {zim_filename}")

    return page_count, zim_filename, None


# ── Main job pipeline ─────────────────────────────────────────────


def _process_job(job, config, stop_event):
    """Execute the full scrape pipeline for a single job."""
    db = StatusDB()
    job_id = job['id']

    logger.info(f"Job {job_id}: starting scrape of {job['url']}")

    # ── Phase 1: Crawl via Zimit ───────────────────────────────────
    db.update_scrape_job(job_id,
                         status='scraping',
                         crawl_mode='zimit',
                         started_at=_now())

    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id)
        return

    page_count, zim_filename, error = _crawl_zimit(job, config, stop_event, db)

    if error == 'cancelled':
        _handle_cancel(db, job_id)
        return
    elif error:
        db.update_scrape_job(job_id,
                             status='failed',
                             error_message=error[:1000],
                             subprocess_pid=None,
                             completed_at=_now())
        return

    db.update_scrape_job(job_id, page_count=page_count)

    # ── Phase 2: Register with kiwix-serve ─────────────────────────
    if stop_event.is_set() or _check_cancelled(db, job_id):
        _handle_cancel(db, job_id)
        return

    db.update_scrape_job(job_id, status='registering')

    output_dir = config.get('scraper', {}).get('output_dir', '/mnt/kiwix')
    zim_path = os.path.join(output_dir, zim_filename)
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

    # ── Phase 3: Complete ──────────────────────────────────────────
    db.update_scrape_job(job_id,
                         status='complete',
                         zim_filename=zim_filename,
                         zim_source_id=zim_source_id,
                         completed_at=_now())

    logger.info(f"Job {job_id}: complete — {zim_filename} ({page_count} pages)")


def _handle_cancel(db, job_id):
    """Handle job cancellation: clean up Docker container and update status."""
    container_name = f'recon-scraper-{job_id}'
    try:
        subprocess.run(['docker', 'rm', '-f', container_name],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    # Clean up tmp dir if it exists
    output_dir = '/mnt/kiwix'
    tmp_dir = os.path.join(output_dir, f'.zimit-tmp-{job_id}')
    shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info(f"Job {job_id}: cancelled")
    db.update_scrape_job(job_id,
                         status='cancelled',
                         subprocess_pid=None,
                         completed_at=_now())
