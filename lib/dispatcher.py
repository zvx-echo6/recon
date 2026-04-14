"""
RECON Dispatcher

Watches configured acquired/<subfolder>/ directories for content+sidecar pairs
that have been stable (mtime unchanged) for the configured threshold, then
hands them to the appropriate processor's pre_flight().

Phase 3: importable one-shot dispatcher. Service-loop integration in Phase 5.
"""
import importlib
import logging
import os
import time

from .utils import get_config
from .status import StatusDB

logger = logging.getLogger("recon.dispatcher")


def _load_processor(processor_name):
    """Dynamically import a processor module from lib.processors."""
    module_path = f"lib.processors.{processor_name}"
    try:
        return importlib.import_module(module_path)
    except ImportError as e:
        logger.error("Cannot load processor %s: %s", processor_name, e)
        return None


def _find_pairs(subfolder_path):
    """Find content+sidecar pairs in a subfolder.

    A pair is two files sharing a basename:
      <basename>.txt  (or other content extension)
      <basename>.meta.json  (sidecar)

    Returns list of (content_path, meta_path, basename) tuples.
    """
    if not os.path.isdir(subfolder_path):
        return []

    files = set(os.listdir(subfolder_path))
    pairs = []

    for fname in sorted(files):
        if fname.endswith('.meta.json'):
            basename = fname[:-len('.meta.json')]
            # Look for matching content file (try common extensions)
            for ext in ['.txt', '.vtt', '.html', '.pdf']:
                content_name = basename + ext
                if content_name in files:
                    pairs.append((
                        os.path.join(subfolder_path, content_name),
                        os.path.join(subfolder_path, fname),
                        basename,
                    ))
                    break

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
            # Both files must be stable
            if not (_is_stable(content_path, stability_seconds) and
                    _is_stable(meta_path, stability_seconds)):
                logger.debug("Pair %s not yet stable, skipping", basename)
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
