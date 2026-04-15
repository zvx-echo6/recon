"""
RECON PeerTube Acquisition Module

Polls PeerTube for new video transcripts and writes them as flat file pairs
into data/acquired/stream/ for the dispatcher to pick up.

Does NOT touch the database — that's transcript_processor's job.
"""
import json
import os
import time

from lib.peertube_scraper import get_videos, get_captions, fetch_vtt, vtt_to_text, _get_pt_config
from lib.utils import content_hash, get_config, setup_logging

logger = setup_logging("recon.acquisition.peertube")


def _build_known_sets(db):
    """Build sets of known UUIDs and titles from catalogue.

    Queries catalogue once per batch for dedup against both cohorts:
    - URL-path rows: extract UUID from https://stream.echo6.co/w/{uuid}
    - Library-path rows: extract title from filename column
    """
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT path, filename FROM catalogue WHERE source = 'stream.echo6.co'"
    ).fetchall()
    known_uuids = set()
    known_titles = set()
    for row in rows:
        path = row['path'] or ''
        if '/w/' in path:
            known_uuids.add(path.rsplit('/w/', 1)[-1])
        fname = row['filename'] or ''
        if fname.endswith('.txt'):
            known_titles.add(fname[:-4])
        else:
            known_titles.add(fname)
    return known_uuids, known_titles


def list_new_videos(db, config=None):
    """Find PeerTube videos with captions not yet in catalogue.

    Returns list of (video_dict, caption_path) tuples for videos that have
    captions and are not in the known UUID or title sets.
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)
    rate_delay = ptc.get('rate_limit_delay', 0.5)

    known_uuids, known_titles = _build_known_sets(db)

    videos = get_videos(config=config)
    new_videos = []
    checked = 0

    for video in videos:
        if video['uuid'] in known_uuids:
            continue
        if video['name'] in known_titles:
            continue

        # Rate limit caption API calls
        if checked > 0:
            time.sleep(rate_delay)
        checked += 1

        try:
            captions = get_captions(video['uuid'], config)
        except Exception as e:
            logger.warning("[peertube] Failed to get captions for %s: %s",
                           video['uuid'][:8], e)
            continue

        if not captions:
            continue

        # Prefer English caption
        caption_path = None
        for c in captions:
            if c.get('language', {}).get('id') == 'en':
                caption_path = c['captionPath']
                break
        if caption_path is None:
            caption_path = captions[0]['captionPath']

        new_videos.append((video, caption_path))

    return new_videos


def acquire_one(video, caption_path, config=None):
    """Fetch transcript and write to hopper as flat files.

    Returns hash string on success, None on skip/error.
    Does NOT touch the database — that's transcript_processor's job.
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)

    pipeline_cfg = config.get('pipeline', {})
    hopper_dir = os.path.join(
        pipeline_cfg.get('acquired_root', '/opt/recon/data/acquired'),
        'stream'
    )
    os.makedirs(hopper_dir, exist_ok=True)

    uuid = video['uuid']

    # Fetch and convert VTT
    vtt_content = fetch_vtt(caption_path, config)
    text, cue_timestamps = vtt_to_text(vtt_content)

    if not text or len(text.strip()) < 50:
        logger.debug("[peertube] Transcript too short for %s (%s): %d chars",
                     video['name'], uuid, len(text) if text else 0)
        return None

    # Write text to temp file, hash it, then rename to final name
    tmp_txt = os.path.join(hopper_dir, f'{uuid}.txt.tmp')
    with open(tmp_txt, 'w', encoding='utf-8') as f:
        f.write(text)

    file_hash = content_hash(tmp_txt)

    # Check if final file already exists (race condition guard)
    final_txt = os.path.join(hopper_dir, f'{file_hash}.txt')
    final_meta = os.path.join(hopper_dir, f'{file_hash}.meta.json')
    if os.path.exists(final_txt):
        os.remove(tmp_txt)
        logger.debug("[peertube] Hopper file already exists: %s", file_hash[:8])
        return None

    # Build sidecar metadata
    video_url = f"{ptc['public_url']}/w/{uuid}"
    meta = {
        'title': video['name'],
        'source_url': video_url,
        'url': video_url,
        'source': 'stream.echo6.co',
        'source_type': 'transcript',
        'category': 'Transcript',
        'channel': video.get('channel_display', ''),
        'duration': video.get('duration', 0),
        'uuid': uuid,
        'cue_timestamps': cue_timestamps,
    }

    # Write meta to tmp, then rename both atomically
    # Meta first, then content — dispatcher only picks up when content file exists
    tmp_meta = os.path.join(hopper_dir, f'{file_hash}.meta.json.tmp')
    with open(tmp_meta, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)

    os.rename(tmp_meta, final_meta)
    os.rename(tmp_txt, final_txt)

    logger.info("[peertube] Acquired: %s (%s) -> %s",
                video['name'], uuid[:8], file_hash[:12])
    return file_hash


def acquire_batch(db, config=None):
    """One-shot: find new videos and acquire them.

    Returns dict: {'acquired': N, 'skipped': N, 'errors': N}
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)
    rate_delay = ptc.get('rate_limit_delay', 0.5)

    result = {'acquired': 0, 'skipped': 0, 'errors': 0}

    try:
        new_videos = list_new_videos(db, config)
    except Exception as e:
        logger.error("[peertube] Failed to list new videos: %s", e, exc_info=True)
        result['errors'] = 1
        return result

    if not new_videos:
        logger.debug("[peertube] No new videos found")
        return result

    logger.info("[peertube] Found %d new videos to acquire", len(new_videos))

    for i, (video, caption_path) in enumerate(new_videos):
        if i > 0:
            time.sleep(rate_delay)
        try:
            file_hash = acquire_one(video, caption_path, config)
            if file_hash:
                result['acquired'] += 1
            else:
                result['skipped'] += 1
        except Exception as e:
            logger.error("[peertube] Error acquiring %s (%s): %s",
                         video['name'], video['uuid'][:8], e, exc_info=True)
            result['errors'] += 1

    return result


def acquisition_loop(stop_event, db, config, interval=1800):
    """Service loop: poll PeerTube for new transcripts every interval seconds."""
    logger.info("[peertube] Acquisition loop started (interval: %ds)", interval)
    while not stop_event.is_set():
        try:
            result = acquire_batch(db, config)
            if result['acquired']:
                logger.info("[peertube] Acquired %d new transcripts (%d skipped, %d errors)",
                            result['acquired'], result['skipped'], result['errors'])
            else:
                logger.debug("[peertube] No new transcripts")
        except Exception as e:
            logger.error("[peertube] Error: %s", e, exc_info=True)
        stop_event.wait(interval)
    logger.info("[peertube] Acquisition loop stopped")
