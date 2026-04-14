"""
RECON Metrics Collector

Background daemon thread that snapshots pipeline metrics every 5 minutes
to the metrics_snapshots SQLite table. Used for time-series charts.
"""
import json
import time
import threading
import logging

logger = logging.getLogger('recon.collector')


def start_collector(stop_event=None):
    """Start the metrics collector in a daemon thread."""
    def _run():
        from .status import StatusDB
        from .utils import get_config
        import requests as req

        interval = 120  # 2 minutes
        logger.info(f"Metrics collector started (interval: {interval}s)")

        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                _snapshot(StatusDB(), get_config(), req)
            except Exception as e:
                logger.error(f"Metrics snapshot failed: {e}")

            # Wait with stop check
            if stop_event:
                stop_event.wait(interval)
                if stop_event.is_set():
                    break
            else:
                time.sleep(interval)

        logger.info("Metrics collector stopped")

    t = threading.Thread(target=_run, daemon=True, name='metrics-collector')
    t.start()
    return t


def _snapshot(db, config, req):
    """Take a single metrics snapshot."""
    from datetime import datetime, timezone, timedelta

    conn = db._get_conn()
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:00Z')  # Round to minute

    # Knowledge pipeline stats
    try:
        totals = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status NOT IN ('complete', 'failed') THEN 1 ELSE 0 END) as in_pipeline,
                SUM(COALESCE(concepts_extracted, 0)) as concepts,
                SUM(COALESCE(vectors_inserted, 0)) as vectors
            FROM documents
        """).fetchone()

        knowledge_data = {
            'total': totals['total'],
            'complete': totals['complete'],
            'failed': totals['failed'],
            'in_pipeline': totals['in_pipeline'],
            'concepts': totals['concepts'],
            'vectors': totals['vectors'],
        }

        conn.execute(
            "INSERT OR REPLACE INTO metrics_snapshots (timestamp, metric_type, data) VALUES (?, ?, ?)",
            (ts, 'knowledge', json.dumps(knowledge_data))
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"Knowledge snapshot failed: {e}")

    # PeerTube pipeline stats (via SSH)
    try:
        import subprocess
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
             'zvx@192.168.1.170',
             'sudo -u peertube psql peertube_prod -t -A -c "SELECT state, COUNT(*) FROM video GROUP BY state;" 2>/dev/null; '
             'echo "---"; '
             'for d in staging completed transcoded failed; do '
             '  dir="/opt/bulk-import/$d"; '
             '  files=$(find -L "$dir" -type f 2>/dev/null | wc -l); '
             '  echo "$d|$files"; '
             'done'],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0 or result.stdout.strip():
            sections = result.stdout.split('---')
            video_states = {}
            if len(sections) > 0:
                for line in sections[0].strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|')
                        if len(parts) == 2 and parts[1].isdigit():
                            video_states[parts[0]] = int(parts[1])
            pipeline_files = {}
            if len(sections) > 1:
                for line in sections[1].strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|')
                        if len(parts) == 2:
                            pipeline_files[parts[0]] = int(parts[1]) if parts[1].isdigit() else 0

            pt_data = {
                'video_states': video_states,
                'pipeline_files': pipeline_files,
                'published': video_states.get('1', 0),
                'backlog': sum(pipeline_files.values()),
            }
            conn.execute(
                "INSERT OR REPLACE INTO metrics_snapshots (timestamp, metric_type, data) VALUES (?, ?, ?)",
                (ts, 'peertube', json.dumps(pt_data))
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"PeerTube snapshot failed: {e}")

    # Prune old snapshots (> 7 days)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        conn.execute("DELETE FROM metrics_snapshots WHERE timestamp < ?", (cutoff,))
        conn.commit()
    except Exception:
        pass
