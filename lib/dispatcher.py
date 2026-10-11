"""
RECON Dispatcher

Watches configured acquired/<subfolder>/ directories for content+sidecar pairs
that have been stable (mtime unchanged) for the configured threshold, then
hands them to the appropriate processor's pre_flight().

Phase 3: importable one-shot dispatcher. Service-loop integration in Phase 5.
Phase 4: sidecar is optional (PDFs may arrive without .meta.json).
Phase 6f-2: format normalizer converts non-standard formats to PDF before dispatch.

Parallel dispatch: pre_flight runs on a bounded worker pool
(pipeline.dispatch_workers, default 3) so one slow file (e.g. a scanned PDF going
through vision OCR) no longer blocks intake. The scan loop never waits for running
jobs: it submits only files whose path is not already in flight, and processors call
claim_hash() right after hashing so two workers never process the same content hash
concurrently (the loser leaves its file in place for a later scan, where the normal
duplicate check removes it).
"""
import collections
import concurrent.futures
import importlib
import logging
import os
import subprocess
import threading
import time

from .utils import get_config
from .status import StatusDB

logger = logging.getLogger("recon.dispatcher")

# Content file extensions recognized by the dispatcher
CONTENT_EXTENSIONS = {'.txt', '.vtt', '.html', '.pdf'}

# Non-standard formats that can be converted to PDF before dispatch
CONVERTIBLE_EXTENSIONS = {'.epub', '.mobi', '.doc', '.docx'}


def _load_processor(processor_name):
    """Dynamically import a processor module from lib.processors."""
    module_path = f"lib.processors.{processor_name}"
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError:
        logger.debug("Processor module not found: %s (not yet implemented)", processor_name)
        return None
    except ImportError as e:
        logger.error("Failed to import processor %s: %s", processor_name, e)
        return None


def _normalize_formats(subfolder_path):
    """Convert non-standard document formats to PDF before dispatch.

    Walks the subfolder for files with convertible extensions (.epub, .mobi,
    .doc, .docx). Converts each to PDF using the appropriate tool, then
    deletes the original.

    Returns count of files converted.
    """
    if not os.path.isdir(subfolder_path):
        return 0

    converted = 0

    for fname in sorted(os.listdir(subfolder_path)):
        stem, ext = os.path.splitext(fname)
        if ext.lower() not in CONVERTIBLE_EXTENSIONS:
            continue

        source = os.path.join(subfolder_path, fname)
        target = os.path.join(subfolder_path, stem + '.pdf')

        if os.path.exists(target):
            logger.debug("Target PDF already exists, skipping: %s", fname)
            continue

        try:
            if ext.lower() in ('.epub', '.mobi'):
                subprocess.run(
                    ['ebook-convert', source, target],
                    capture_output=True, check=True, timeout=300,
                )
            elif ext.lower() in ('.doc', '.docx'):
                subprocess.run(
                    ['libreoffice', '--headless', '--convert-to', 'pdf',
                     '--outdir', subfolder_path, source],
                    capture_output=True, check=True, timeout=300,
                )

            if os.path.isfile(target) and os.path.getsize(target) > 0:
                os.remove(source)
                converted += 1
                logger.info("Converted %s -> %s.pdf", fname, stem)
            else:
                logger.warning("Conversion produced no output: %s", fname)

        except subprocess.TimeoutExpired:
            logger.error("Conversion timed out: %s", fname)
        except subprocess.CalledProcessError as e:
            logger.error("Conversion failed for %s: %s", fname,
                         e.stderr.decode(errors='replace')[:200] if e.stderr else str(e))
        except Exception as e:
            logger.error("Unexpected error converting %s: %s", fname, e)

    return converted


def _find_pairs(subfolder_path):
    """Find content files (with optional sidecar) in a subfolder.

    A pair is:
      <basename>.<ext>       — content file
      <basename>.meta.json   — optional sidecar

    Returns list of (content_path, meta_path_or_None, basename) tuples.
    """
    if not os.path.isdir(subfolder_path):
        return []

    files = set(os.listdir(subfolder_path))
    pairs = []
    seen_basenames = set()

    # First pass: find .meta.json files and their matching content
    for fname in sorted(files):
        if fname.endswith('.meta.json'):
            basename = fname[:-len('.meta.json')]
            for ext in sorted(CONTENT_EXTENSIONS):
                content_name = basename + ext
                if content_name in files:
                    pairs.append((
                        os.path.join(subfolder_path, content_name),
                        os.path.join(subfolder_path, fname),
                        basename,
                    ))
                    seen_basenames.add(content_name)
                    break

    # Second pass: find solo content files (no sidecar)
    for fname in sorted(files):
        if fname in seen_basenames:
            continue
        _stem, ext = os.path.splitext(fname)
        if ext.lower() in CONTENT_EXTENSIONS and not fname.endswith('.meta.json'):
            pairs.append((
                os.path.join(subfolder_path, fname),
                None,
                _stem,
            ))

    return pairs


def _is_stable(filepath, stability_seconds):
    """Check if a file's mtime is older than stability_seconds ago."""
    try:
        mtime = os.path.getmtime(filepath)
        return (time.time() - mtime) >= stability_seconds
    except OSError:
        return False


# ── Parallel worker pool ─────────────────────────────────────────────

DEFAULT_DISPATCH_WORKERS = 3

_state_lock = threading.Lock()
_inflight_paths = set()      # content paths submitted or running
_inflight_hashes = set()     # content hashes currently claimed by a running job
_running = 0                 # jobs currently inside pre_flight
_cooldown = {}               # content path -> time its last attempt ended
RETRY_COOLDOWN_S = 30        # a file left in place is not re-dispatched sooner than this
_shared_db = None            # one StatusDB for all scans (connections are per-thread)
_executor = None
_executor_workers = 0
_results = collections.deque()   # finished results not yet reported by dispatch_once
_job_ctx = threading.local()     # per-worker: list of hashes claimed by the current job
_stopping = threading.Event()    # set at shutdown: stop submitting, abort long jobs


def stop_requested():
    """True once shutdown began; long-running processors poll this between units of work."""
    return _stopping.is_set()


def claim_hash(file_hash):
    """Claim a content hash for the current dispatch job.

    Processors call this right after hashing. Returns False if another running job
    already holds the hash (caller must return without touching any file). Outside a
    dispatcher job (direct pre_flight calls, tests) it always returns True.
    """
    claimed = getattr(_job_ctx, 'hashes', None)
    if claimed is None:
        return True
    with _state_lock:
        if file_hash in _inflight_hashes:
            return False
        _inflight_hashes.add(file_hash)
    claimed.append(file_hash)
    return True


def _get_executor(workers):
    global _executor, _executor_workers
    with _state_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix='dispatch')
            _executor_workers = workers
            logger.info("[dispatcher] Worker pool started: %d workers", workers)
        return _executor


def _run_job(processor, processor_name, subfolder_name, content_path, meta_path,
             basename, db, config):
    """Worker-thread body: run one pre_flight, always release in-flight bookkeeping."""
    global _running
    started = time.time()
    with _state_lock:
        _running += 1
    _job_ctx.hashes = []
    try:
        if _stopping.is_set():
            result = {'action': 'skipped_shutdown', 'basename': basename}
        elif not os.path.exists(content_path):
            result = {'action': 'vanished', 'basename': basename}
        else:
            logger.info("Dispatching %s/%s to %s", subfolder_name, basename, processor_name)
            try:
                result = processor.pre_flight(content_path, meta_path, db, config)
            except Exception as e:
                logger.error("Processor %s crashed on %s: %s", processor_name, basename, e,
                             exc_info=True)
                result = {'action': 'error', 'error': str(e), 'basename': basename}
            logger.info("Result for %s: %s (%.1fs)", basename,
                        result.get('action', 'unknown'), time.time() - started)
    finally:
        with _state_lock:
            for h in _job_ctx.hashes:
                _inflight_hashes.discard(h)
            _inflight_paths.discard(content_path)
            _cooldown[content_path] = time.time()
            _running -= 1
        _job_ctx.hashes = None
    _results.append(result)
    return result


def _drain_results():
    out = []
    while True:
        try:
            out.append(_results.popleft())
        except IndexError:
            return out


def dispatch_once(wait=False):
    """Scan all configured acquired/ subfolders once and submit new files to the pool.

    Never blocks on running jobs (unless wait=True, used by the CLI/tests, which waits
    for the jobs submitted by this call). Returns the results of jobs that finished
    since the previous call.
    """
    config = get_config()
    pipeline_cfg = config.get('pipeline', {})
    acquired_root = pipeline_cfg.get('acquired_root', '/opt/recon/data/acquired')
    dispatch_map = pipeline_cfg.get('dispatch', {})
    stability_seconds = pipeline_cfg.get('mtime_stability_seconds', 10)
    workers = max(1, int(pipeline_cfg.get('dispatch_workers', DEFAULT_DISPATCH_WORKERS)))
    max_pending = workers * 4    # bound on submitted-but-unfinished jobs

    global _shared_db
    if _shared_db is None or _shared_db.db_path != config['paths']['db']:
        _shared_db = StatusDB(config['paths']['db'])
    db = _shared_db
    executor = _get_executor(workers)
    futures = []
    capped = False

    for subfolder_name, processor_name in dispatch_map.items():
        if _stopping.is_set() or capped:
            break
        subfolder_path = os.path.join(acquired_root, subfolder_name)

        processor = _load_processor(processor_name)
        if processor is None:
            continue

        if not hasattr(processor, 'pre_flight'):
            logger.error("Processor %s has no pre_flight function", processor_name)
            continue

        # Convert non-standard formats to PDF before scanning for pairs
        _normalize_formats(subfolder_path)

        pairs = _find_pairs(subfolder_path)
        if not pairs:
            continue

        for content_path, meta_path, basename in pairs:
            if _stopping.is_set():
                break
            # Content file must be stable; sidecar too if present
            if not _is_stable(content_path, stability_seconds):
                logger.debug("File %s not yet stable, skipping", basename)
                continue
            if meta_path and not _is_stable(meta_path, stability_seconds):
                logger.debug("Sidecar for %s not yet stable, skipping", basename)
                continue

            with _state_lock:
                if content_path in _inflight_paths:
                    continue
                last = _cooldown.get(content_path)
                if last is not None and time.time() - last < RETRY_COOLDOWN_S:
                    continue
                if len(_inflight_paths) >= max_pending:
                    capped = True
                    break
                _inflight_paths.add(content_path)
            try:
                futures.append(executor.submit(
                    _run_job, processor, processor_name, subfolder_name,
                    content_path, meta_path, basename, db, config))
            except RuntimeError:     # executor shut down underneath us
                with _state_lock:
                    _inflight_paths.discard(content_path)
                break

    if futures:
        with _state_lock:
            logger.info("[dispatcher] Submitted %d job(s); in flight: %d (running %d, workers %d)",
                        len(futures), len(_inflight_paths), _running, workers)
    if wait and futures:
        concurrent.futures.wait(futures)
    return _drain_results()


def shutdown_workers(timeout=40):
    """Stop submitting, cancel queued jobs, and wait up to `timeout` s for running jobs
    (which poll stop_requested() and put their files back). Returns jobs still running."""
    _stopping.set()
    with _state_lock:
        ex = _executor
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _state_lock:
            if _running == 0:
                return 0
        time.sleep(0.25)
    with _state_lock:
        return _running


def _recover_orphans(config, db):
    """Let each processor put back files left half-moved by an unclean stop."""
    for processor_name in config.get('pipeline', {}).get('dispatch', {}).values():
        processor = _load_processor(processor_name)
        fn = getattr(processor, 'recover_orphans', None) if processor else None
        if fn:
            try:
                fn(db, config)
            except Exception as e:
                logger.error("[dispatcher] recover_orphans failed for %s: %s", processor_name, e,
                             exc_info=True)


def dispatch_loop(stop_event, db, config, interval=30):
    """Run dispatch_once() on a loop until stop_event is set.

    Designed to run as a service thread. Never raises to the caller. Scans are
    non-blocking: pre_flight work happens on the worker pool.
    """
    logger.info("[dispatcher] Loop started (interval: %ds)", interval)
    _stopping.clear()
    _recover_orphans(config, db)

    while not stop_event.is_set():
        try:
            results = dispatch_once()
            if results:
                actions = {}
                for r in results:
                    a = r.get('action', 'unknown')
                    actions[a] = actions.get(a, 0) + 1
                logger.info("[dispatcher] Finished %d items: %s",
                            len(results),
                            ", ".join(f"{k}={v}" for k, v in sorted(actions.items())))
            else:
                logger.debug("[dispatcher] No items finished")
        except Exception as e:
            logger.error("[dispatcher] Error in dispatch_once: %s", e, exc_info=True)

        # While jobs are in flight rescan every second so finished workers are refilled
        # promptly; per-file cooldown stops a failing file being retried at that rate.
        with _state_lock:
            if len(_cooldown) > 5000:
                cutoff = time.time() - 3600
                for p in [p for p, t in _cooldown.items() if t < cutoff]:
                    del _cooldown[p]
            busy = bool(_inflight_paths)
        stop_event.wait(min(interval, 1.0) if busy else interval)

    _stopping.set()
    logger.info("[dispatcher] Loop stopped")
