"""
RECON Dispatcher

Watches configured acquired/<subfolder>/ directories for content+sidecar pairs
that have been stable (mtime unchanged) for the configured threshold, then
hands them to the appropriate processor's pre_flight().

Phase 3: importable one-shot dispatcher. Service-loop integration in Phase 5.
Phase 4: sidecar is optional (PDFs may arrive without .meta.json).
"""
import importlib
import logging
import os
import time

from .utils import get_config
from .status import StatusDB

logger = logging.getLogger("recon.dispatcher")

# Content file extensions recognized by the dispatcher
CONTENT_EXTENSIONS = {'.txt', '.vtt', '.html', '.pdf'}


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


def dispatch_once():
    """One-shot dispatch: scan all configured acquired/ subfolders once.

    Returns list of result dicts from processor pre_flight calls.
    """
    config = get_config()
    pipeline_cfg = config.get('pipeline', {})
    acquired_root = pipeline_cfg.get('acquired_root', '/opt/recon/data/acquired')
    dispatch_map = pipeline_cfg.get('dispatch', {})
    stability_seconds = pipeline_cfg.get('mtime_stability_seconds', 10)

    db = StatusDB(config['paths']['db'])
    results = []

    for subfolder_name, processor_name in dispatch_map.items():
        subfolder_path = os.path.join(acquired_root, subfolder_name)

        processor = _load_processor(processor_name)
        if processor is None:
            continue

        if not hasattr(processor, 'pre_flight'):
            logger.error("Processor %s has no pre_flight function", processor_name)
            continue

        pairs = _find_pairs(subfolder_path)
        if not pairs:
            continue

        for content_path, meta_path, basename in pairs:
            # Content file must be stable; sidecar too if present
            if not _is_stable(content_path, stability_seconds):
                logger.debug("File %s not yet stable, skipping", basename)
                continue
            if meta_path and not _is_stable(meta_path, stability_seconds):
                logger.debug("Sidecar for %s not yet stable, skipping", basename)
                continue

            logger.info("Dispatching %s/%s to %s", subfolder_name, basename, processor_name)
            try:
                result = processor.pre_flight(content_path, meta_path, db, config)
                results.append(result)
                logger.info("Result for %s: %s", basename, result.get('action', 'unknown'))
            except Exception as e:
                logger.error("Processor %s crashed on %s: %s", processor_name, basename, e)
                results.append({
                    'action': 'error',
                    'error': str(e),
                    'basename': basename,
                })

    return results


def dispatch_loop(stop_event, db, config, interval=30):
    """Run dispatch_once() on a loop until stop_event is set.

    Designed to run as a service thread. Never raises to the caller.
    """
    logger.info("[dispatcher] Loop started (interval: %ds)", interval)

    while not stop_event.is_set():
        try:
            results = dispatch_once()
            if results:
                actions = {}
                for r in results:
                    a = r.get('action', 'unknown')
                    actions[a] = actions.get(a, 0) + 1
                logger.info("[dispatcher] Dispatched %d items: %s",
                            len(results),
                            ", ".join(f"{k}={v}" for k, v in sorted(actions.items())))
            else:
                logger.debug("[dispatcher] No items to dispatch")
        except Exception as e:
            logger.error("[dispatcher] Error in dispatch_once: %s", e, exc_info=True)

        stop_event.wait(interval)

    logger.info("[dispatcher] Loop stopped")
