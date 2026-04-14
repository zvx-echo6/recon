"""
RECON PeerTube Scraper — Video transcript ingestion.

Fetches WebVTT captions from a PeerTube instance, converts to plain text,
chunks into pages, and feeds into the standard RECON enrichment pipeline.

Output format matches lib/web_scraper.py so the enricher and embedder
process transcript content identically to web content.
"""

import hashlib
import io
import json
import os
import bisect
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import webvtt

from .utils import get_config, setup_logging
from .status import StatusDB
from .web_scraper import chunk_text

logger = setup_logging('recon.peertube_scraper')

# Module-level stop flag — set by service thread for graceful shutdown
_stop_check = None

def set_stop_check(fn):
    """Register a callable that returns True when shutdown is requested."""
    global _stop_check
    _stop_check = fn

# Defaults (overridden by config.yaml peertube section)
DEFAULT_API_BASE = 'http://192.168.1.170'
DEFAULT_PUBLIC_URL = 'https://stream.echo6.co'
DEFAULT_FETCH_TIMEOUT = 30
DEFAULT_RATE_LIMIT_DELAY = 0.5


def _get_pt_config(config=None):
    """Get PeerTube settings from config, with defaults."""
    if config is None:
        config = get_config()
    pt = config.get('peertube', {})
    return {
        'api_base': pt.get('api_base', DEFAULT_API_BASE),
        'public_url': pt.get('public_url', DEFAULT_PUBLIC_URL),
        'fetch_timeout': pt.get('fetch_timeout', DEFAULT_FETCH_TIMEOUT),
        'rate_limit_delay': pt.get('rate_limit_delay', DEFAULT_RATE_LIMIT_DELAY),
    }


def _api_get(path, config=None, params=None):
    """Make a GET request to the PeerTube API."""
    ptc = _get_pt_config(config)
    url = f"{ptc['api_base']}{path}"
    resp = requests.get(url, params=params, timeout=ptc['fetch_timeout'])
    resp.raise_for_status()
    return resp.json()


def get_videos(channel=None, since=None, config=None):
    """
    Paginate through all published videos on the PeerTube instance.

    Args:
        channel: Filter to this channel actor_name (e.g., 'mental-outlaw')
        since: ISO date string — only return videos published after this date
        config: RECON config dict

    Returns list of video dicts with: uuid, name, duration,
    channel.name, channel.displayName, publishedAt, description.
    """
    ptc = _get_pt_config(config)
    videos = []
    start = 0
    count = 100  # PeerTube supports up to 100 per page

    while True:
        if channel:
            path = f"/api/v1/video-channels/{channel}/videos"
        else:
            path = "/api/v1/videos"

        data = _api_get(path, config, params={
            'count': count,
            'start': start,
            'sort': '-publishedAt',
        })

        total = data.get('total', 0)
        batch = data.get('data', [])

        if not batch:
            break

        for v in batch:
            published = v.get('publishedAt', '')

            # Filter by since date
            if since and published < since:
                # Videos are sorted by publishedAt desc, so once we pass
                # the since threshold, all remaining are older — stop
                return videos

            videos.append({
                'uuid': v['uuid'],
                'name': v['name'],
                'duration': v.get('duration', 0),
                'channel_name': v.get('channel', {}).get('name', ''),
                'channel_display': v.get('channel', {}).get('displayName', ''),
                'publishedAt': published,
                'description': (v.get('description') or '')[:500],
            })

        start += count
        if start >= total:
            break

        # Check for shutdown during pagination
        if _stop_check and _stop_check():
            logger.info(f"Shutdown requested during video listing — returning {len(videos)} collected so far")
            return videos

        # Rate limit pagination requests
        time.sleep(ptc['rate_limit_delay'])

    return videos


def get_captions(uuid, config=None):
    """Get caption list for a video. Returns list of caption dicts."""
    data = _api_get(f"/api/v1/videos/{uuid}/captions", config)
    return data.get('data', [])


def fetch_vtt(caption_path, config=None):
    """Fetch raw VTT file content from PeerTube."""
    ptc = _get_pt_config(config)
    url = f"{ptc['api_base']}{caption_path}"
    resp = requests.get(url, timeout=ptc['fetch_timeout'])
    resp.raise_for_status()
    return resp.text



def _parse_vtt_time(time_str):
    """Parse VTT timestamp string (HH:MM:SS.mmm or MM:SS.mmm) to seconds."""
    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return 0.0


def vtt_to_text(vtt_content):
    """
    Convert WebVTT content to clean plain text with timestamp tracking.

    Strips timestamps, de-duplicates consecutive identical cues (common with
    Whisper output), removes HTML tags, and joins cues with spaces (not
    newlines — Whisper cues break mid-sentence).

    Returns (text, cue_timestamps) where:
    - text: clean prose string
    - cue_timestamps: list of (start_seconds, char_offset) tuples tracking
      where each VTT cue begins in the output text
    """
    buf = io.StringIO(vtt_content)
    try:
        captions = webvtt.read_buffer(buf)
    except Exception:
        # Fallback: manual regex parse if webvtt-py fails
        return _vtt_to_text_fallback(vtt_content)

    prev_text = None
    segments = []
    raw_timestamps = []  # (start_seconds, segment_index)

    for caption in captions:
        text = caption.text.strip()
        if not text:
            continue

        # Strip HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # De-duplicate consecutive identical cues
        if text == prev_text:
            continue
        prev_text = text

        start_seconds = _parse_vtt_time(caption.start)
        raw_timestamps.append((start_seconds, len(segments)))
        segments.append(text)

    # Join with spaces — VTT cues break mid-sentence
    raw = ' '.join(segments)

    # Clean up double spaces and whitespace
    raw = re.sub(r'\s+', ' ', raw).strip()

    # Compute char offsets for each tracked segment
    seg_offsets = []
    pos = 0
    for i, seg in enumerate(segments):
        seg_offsets.append(pos)
        pos += len(seg) + 1  # +1 for space separator

    cue_timestamps = []
    for start_secs, seg_idx in raw_timestamps:
        if seg_idx < len(seg_offsets):
            cue_timestamps.append((start_secs, seg_offsets[seg_idx]))

    return raw, cue_timestamps


def _vtt_to_text_fallback(vtt_content):
    """Regex-based VTT parser as fallback. Returns (text, cue_timestamps)."""
    lines = vtt_content.split('\n')
    prev_text = None
    segments = []
    raw_timestamps = []
    last_time = 0.0

    for line in lines:
        line = line.strip()
        if not line or line == 'WEBVTT':
            continue
        if '-->' in line:
            # Parse start time from "00:01:23.456 --> 00:01:25.789"
            time_part = line.split('-->')[0].strip()
            last_time = _parse_vtt_time(time_part)
            continue
        if line.isdigit():
            continue

        text = re.sub(r'<[^>]+>', '', line)
        if text == prev_text:
            continue
        prev_text = text
        raw_timestamps.append((last_time, len(segments)))
        segments.append(text)

    raw = ' '.join(segments)
    raw = re.sub(r'\s+', ' ', raw).strip()

    # Compute char offsets
    seg_offsets = []
    pos = 0
    for seg in segments:
        seg_offsets.append(pos)
        pos += len(seg) + 1

    cue_timestamps = []
    for start_secs, seg_idx in raw_timestamps:
        if seg_idx < len(seg_offsets):
            cue_timestamps.append((start_secs, seg_offsets[seg_idx]))

    return raw, cue_timestamps



def _map_page_timestamps(pages, full_text, cue_timestamps):
    """
    Map page numbers to video timestamps.

    For each page, finds its approximate start position in the full text,
    then looks up the nearest VTT cue timestamp via binary search.

    Returns dict: {"page_0001": 0.0, "page_0002": 312.5, ...}
    """
    if not cue_timestamps:
        return {}

    offsets = [ct[1] for ct in cue_timestamps]
    times = [ct[0] for ct in cue_timestamps]

    page_ts = {}
    search_start = 0

    for i, page_text in enumerate(pages):
        page_name = f"page_{i+1:04d}"

        # Find where this page starts in the full text
        snippet = page_text[:200].strip()
        pos = full_text.find(snippet, search_start)
        if pos < 0:
            pos = search_start  # fallback

        # Binary search for nearest cue at or before this position
        idx = bisect.bisect_right(offsets, pos) - 1
        if idx < 0:
            idx = 0

        page_ts[page_name] = round(times[idx], 1)
        search_start = pos + len(snippet)

    return page_ts

def _content_hash(text):
    """MD5 hash of text content — same as web_scraper."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def ingest_video(uuid, video_meta, config=None):
    """
    Ingest a single PeerTube video transcript.

    Fetches captions, converts VTT to text, chunks into pages,
    saves to data/text/{hash}/, and sets status to 'extracted'.

    Args:
        uuid: Video UUID
        video_meta: Dict with name, duration, channel_name, channel_display,
                    publishedAt, description
        config: RECON config dict

    Returns dict with hash, status, title, page_count — or None if no captions.
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)
    db = StatusDB()

    # Get captions
    captions = get_captions(uuid, config)
    if not captions:
        return None

    # Prefer English caption
    caption = None
    for c in captions:
        if c.get('language', {}).get('id') == 'en':
            caption = c
            break
    if caption is None:
        caption = captions[0]

    # Fetch VTT
    vtt_content = fetch_vtt(caption['captionPath'], config)

    # Convert to plain text with timestamp tracking
    text, cue_timestamps = vtt_to_text(vtt_content)
    if not text or len(text) < 50:
        logger.warning(f"Transcript too short for {video_meta['name']} ({uuid}): {len(text)} chars")
        return None

    # Hash the text content
    doc_hash = _content_hash(text)

    # Check for duplicate
    conn = db._get_conn()
    existing = conn.execute("SELECT * FROM catalogue WHERE hash = ?", (doc_hash,)).fetchone()
    if existing:
        doc = db.get_document(doc_hash)
        existing_status = doc['status'] if doc else existing['status']
        logger.debug(f"Duplicate transcript (hash {doc_hash[:12]}...) — {video_meta['name']}")
        return {
            'hash': doc_hash,
            'status': 'duplicate',
            'title': video_meta['name'],
            'existing_status': existing_status,
        }

    # Chunk into pages
    words_per_page = config.get('web_scraper', {}).get('words_per_page', 2000)
    pages = chunk_text(text, words_per_page)

    # Compute page-to-timestamp mapping
    page_timestamps = _map_page_timestamps(pages, text, cue_timestamps)

    # Save text files
    text_dir = os.path.join(config['paths']['text'], doc_hash)
    os.makedirs(text_dir, exist_ok=True)

    for i, page_text in enumerate(pages, 1):
        page_file = os.path.join(text_dir, f"page_{i:04d}.txt")
        with open(page_file, 'w', encoding='utf-8') as f:
            f.write(page_text)

    # Save meta.json
    video_url = f"{ptc['public_url']}/w/{uuid}"
    meta = {
        'hash': doc_hash,
        'source_type': 'transcript',
        'url': video_url,
        'title': video_meta['name'],
        'author': video_meta.get('channel_display', ''),
        'channel': video_meta.get('channel_name', ''),
        'duration': video_meta.get('duration', 0),
        'date': video_meta.get('publishedAt', ''),
        'description': video_meta.get('description', ''),
        'sitename': 'stream.echo6.co',
        'page_count': len(pages),
        'text_length': len(text),
        'page_timestamps': page_timestamps,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(text_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    # Display filename for catalogue
    display_name = re.sub(r'[^\w\s._-]', '', video_meta['name'])[:200].strip()
    if not display_name:
        display_name = uuid

    # Add to catalogue
    db.add_to_catalogue(
        doc_hash, display_name, video_url,
        len(text), 'stream.echo6.co', video_meta.get('channel_name', 'unknown')
    )

    # Queue + advance to extracted
    db.queue_document(doc_hash)
    db.update_status(doc_hash, 'extracted',
                     page_count=len(pages),
                     pages_extracted=len(pages),
                     book_title=video_meta['name'],
                     book_author=video_meta.get('channel_display', ''))

    logger.info(
        f"Ingested transcript: {video_meta['name']} ({uuid[:8]}...) "
        f"-> {doc_hash[:12]}... ({len(pages)} pages, {len(text)} chars)"
    )

    return {
        'hash': doc_hash,
        'status': 'extracted',
        'title': video_meta['name'],
        'page_count': len(pages),
        'text_length': len(text),
        'page_timestamps': page_timestamps,
        'channel': video_meta.get('channel_name', ''),
        'duration': video_meta.get('duration', 0),
        'url': video_url,
    }


def ingest_channel(channel_name, config=None, since=None):
    """
    Ingest all captioned videos from a specific channel.

    Returns summary dict.
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)

    logger.info(f"Ingesting channel: {channel_name}")
    videos = get_videos(channel=channel_name, since=since, config=config)
    return _ingest_video_list(videos, config, ptc)


def ingest_all(config=None, since=None):
    """
    Ingest all captioned videos from the entire PeerTube instance.

    Returns summary dict.
    """
    if config is None:
        config = get_config()
    ptc = _get_pt_config(config)

    logger.info("Ingesting all PeerTube videos with captions")
    videos = get_videos(since=since, config=config)
    return _ingest_video_list(videos, config, ptc)


def _ingest_video_list(videos, config, ptc):
    """Process a list of videos — shared logic for ingest_channel and ingest_all."""
    results = []
    skipped_no_captions = 0
    skipped_duplicate = 0
    failed = 0
    ingested = 0
    total_pages = 0

    total = len(videos)
    logger.info(f"Found {total} videos to check for captions")

    for i, video in enumerate(videos, 1):
        if _stop_check and _stop_check():
            logger.info(f"Shutdown requested — stopping after {i-1}/{total} videos")
            break
        uuid = video['uuid']

        try:
            result = ingest_video(uuid, video, config)

            if result is None:
                skipped_no_captions += 1
            elif result['status'] == 'duplicate':
                skipped_duplicate += 1
            else:
                ingested += 1
                total_pages += result.get('page_count', 0)
                results.append(result)

        except Exception as e:
            logger.error(f"[{i}/{total}] Failed: {video['name']} ({uuid}) — {e}")
            failed += 1

        # Check for shutdown
        if _stop_check and _stop_check():
            logger.info(f"Shutdown requested — stopping after {i}/{total} videos")
            break

        # Rate limit
        if i < total:
            time.sleep(ptc['rate_limit_delay'])

        # Progress logging every 50 videos
        if i % 50 == 0:
            logger.info(
                f"Progress: {i}/{total} checked — "
                f"{ingested} ingested, {skipped_no_captions} no captions, "
                f"{skipped_duplicate} dupes, {failed} failed"
            )

    logger.info(
        f"PeerTube ingestion complete: {ingested} ingested ({total_pages} pages), "
        f"{skipped_no_captions} no captions, {skipped_duplicate} duplicates, "
        f"{failed} failed out of {total} videos"
    )

    return {
        'results': results,
        'summary': {
            'total_checked': total,
            'ingested': ingested,
            'skipped_no_captions': skipped_no_captions,
            'skipped_duplicate': skipped_duplicate,
            'failed': failed,
            'total_pages': total_pages,
        }
    }


def get_instance_stats(config=None):
    """Get PeerTube instance statistics for the dashboard."""
    if config is None:
        config = get_config()
    db = StatusDB()

    # Total videos on instance
    try:
        data = _api_get("/api/v1/videos", config, params={'count': 1})
        total_videos = data.get('total', 0)
    except Exception:
        total_videos = 0

    # Videos ingested into RECON (from catalogue)
    conn = db._get_conn()
    ingested = conn.execute(
        "SELECT count(*) FROM catalogue WHERE source = 'stream.echo6.co'"
    ).fetchone()[0]

    # Status breakdown
    status_rows = conn.execute(
        "SELECT d.status, count(*) as cnt FROM documents d "
        "JOIN catalogue c ON d.hash = c.hash "
        "WHERE c.source = 'stream.echo6.co' "
        "GROUP BY d.status"
    ).fetchall()
    status_breakdown = {row['status']: row['cnt'] for row in status_rows}

    return {
        'total_videos': total_videos,
        'ingested': ingested,
        'status_breakdown': status_breakdown,
    }
