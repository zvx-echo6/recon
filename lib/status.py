"""
RECON Status Tracker

SQLite operations for catalogue and documents tables. WAL mode, thread-local connections.
Status flow: catalogued -> queued -> extracting -> extracted -> enriching -> enriched -> embedding -> complete.

Config: paths.db
"""
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .utils import get_config

_local = threading.local()

# --- Write serialisation -----------------------------------------------------
# SQLite allows one writer at a time. With 16+ worker threads each holding its own
# connection, writers used to queue inside SQLite's busy handler (5 s) and fail with
# "database is locked". All connections handed out by StatusDB now serialise write
# transactions through one process-wide RLock (held from the first write statement
# until the transaction ends) and retry "database is locked" with jittered backoff.
# Reads never take the lock (WAL readers do not block).
BUSY_TIMEOUT_MS = 30000
WRITE_RETRY_ATTEMPTS = 5
WRITE_LOCK_TIMEOUT_S = 60

_WRITE_LOCK = threading.RLock()
_WRITE_SQL = re.compile(
    r'\s*(?:/\*.*?\*/\s*)*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|REINDEX|VACUUM|BEGIN)\b',
    re.I | re.S)


def _is_lock_error(exc):
    msg = str(exc).lower()
    return 'locked' in msg or 'busy' in msg


def _backoff(attempt):
    time.sleep(min(5.0, 0.25 * (2 ** attempt)) * (0.5 + random.random()))


_LIB_INDEX = {'built': 0.0, 'by_size': {}, 'by_name': {}}
_LIB_INDEX_LOCK = threading.Lock()
_LIB_INDEX_TTL_S = 600


def _norm_name(filename):
    return re.sub(r'[^a-z0-9]', '', os.path.splitext(filename)[0].lower())


def _library_pdf_index(config):
    """(by_size, by_name): library PDFs keyed by byte size and by normalised file name,
    cached for 10 minutes. Only used when a failed document has lost its source and is
    being retried (see find_source_pdf)."""
    with _LIB_INDEX_LOCK:
        if time.time() - _LIB_INDEX['built'] < _LIB_INDEX_TTL_S and _LIB_INDEX['built']:
            return _LIB_INDEX['by_size'], _LIB_INDEX['by_name']
        root = config.get('library_root', '/mnt/library')
        by_size = {}
        by_name = {}
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath == root:
                dirnames[:] = [d for d in dirnames if d not in ('_holding',)]
            for f in filenames:
                if f.lower().endswith('.pdf'):
                    p = os.path.join(dirpath, f)
                    by_name.setdefault(_norm_name(f), []).append(p)
                    try:
                        by_size.setdefault(os.path.getsize(p), []).append(p)
                    except OSError:
                        pass
        _LIB_INDEX['by_size'] = by_size
        _LIB_INDEX['by_name'] = by_name
        _LIB_INDEX['built'] = time.time()
        return by_size, by_name


class _LockedConnection(sqlite3.Connection):
    """sqlite3 connection that serialises write transactions process-wide and retries
    'database is locked'. Connections are thread-local, so per-instance state is safe."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._held = 0

    def _acquire(self):
        if not _WRITE_LOCK.acquire(timeout=WRITE_LOCK_TIMEOUT_S):
            raise sqlite3.OperationalError("database is locked (in-process write lock timeout)")
        self._held += 1

    def _release_if_idle(self):
        if not self.in_transaction:
            while self._held > 0:
                self._held -= 1
                _WRITE_LOCK.release()

    def _guarded(self, call, is_write):
        attempt = 0
        while True:
            # Only a statement that starts a fresh transaction is safe to retry.
            retryable = not self.in_transaction
            try:
                if is_write:
                    self._acquire()
                return call()
            except sqlite3.OperationalError as e:
                if not (retryable and _is_lock_error(e)) or attempt >= WRITE_RETRY_ATTEMPTS - 1:
                    raise
            finally:
                self._release_if_idle()
            _backoff(attempt)
            attempt += 1

    def execute(self, sql, *args):
        return self._guarded(lambda: super(_LockedConnection, self).execute(sql, *args),
                             bool(_WRITE_SQL.match(sql)))

    def executemany(self, sql, *args):
        return self._guarded(lambda: super(_LockedConnection, self).executemany(sql, *args),
                             bool(_WRITE_SQL.match(sql)))

    def executescript(self, script):
        return self._guarded(lambda: super(_LockedConnection, self).executescript(script), True)

    def commit(self):
        try:
            return self._guarded(lambda: super(_LockedConnection, self).commit(), False)
        finally:
            self._release_if_idle()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._release_if_idle()

    def close(self):
        try:
            return super().close()
        finally:
            self._held_release_all()

    def _held_release_all(self):
        while self._held > 0:
            self._held -= 1
            _WRITE_LOCK.release()

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._release_if_idle()


class StatusDB:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = get_config()['paths']['db']
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        # Connections are cached per thread; key by path so a second StatusDB on a
        # different file (tests, tools) never reuses the wrong connection.
        conns = getattr(_local, 'conns', None)
        if conns is None:
            conns = _local.conns = {}
        conn = conns.get(self.db_path)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000,
                                   factory=_LockedConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conns[self.db_path] = conn
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS catalogue (
                hash TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER,
                source TEXT,
                category TEXT,
                status TEXT DEFAULT 'catalogued',
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS documents (
                hash TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                path TEXT,
                size_bytes INTEGER,
                page_count INTEGER,
                book_title TEXT,
                book_author TEXT,
                collection TEXT DEFAULT 'survival',
                status TEXT DEFAULT 'pending',
                pages_extracted INTEGER DEFAULT 0,
                concepts_extracted INTEGER DEFAULT 0,
                vectors_inserted INTEGER DEFAULT 0,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                extracted_at TEXT,
                enriched_at TEXT,
                embedded_at TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS intel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                timestamp TEXT,
                region TEXT,
                category TEXT,
                content TEXT,
                summary TEXT,
                key_facts TEXT,
                credibility_score REAL,
                verification_status TEXT,
                vector_id INTEGER,
                ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS metrics_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(timestamp, metric_type)
            );

            CREATE INDEX IF NOT EXISTS idx_catalogue_status ON catalogue(status);
            CREATE INDEX IF NOT EXISTS idx_catalogue_source ON catalogue(source);
            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
        """)
        # Migration: add path_updated_at column if missing
        try:
            conn.execute("ALTER TABLE catalogue ADD COLUMN path_updated_at TEXT")
        except Exception:
            pass  # column already exists
        # Migration: add organized_at column to documents if missing
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN organized_at TEXT")
        except Exception:
            pass  # column already exists
        # Migration: add filing_error / filing_failed_at columns to documents if missing
        for col in ('filing_error', 'filing_failed_at'):
            try:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} TEXT")
            except Exception:
                pass  # column already exists

        # Migration: add subprocess_pid column to scrape_jobs if missing
        try:
            conn.execute("ALTER TABLE scrape_jobs ADD COLUMN subprocess_pid INTEGER")
        except Exception:
            pass  # column already exists

        # Migration: add reject pattern columns to scrape_jobs if missing
        for col, coltype in [('additional_reject_patterns', 'TEXT'), ('skip_default_patterns', 'INTEGER DEFAULT 0')]:
            try:
                conn.execute(f"ALTER TABLE scrape_jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # column already exists

        # Migration: add crawl_mode column to scrape_jobs if missing
        try:
            conn.execute("ALTER TABLE scrape_jobs ADD COLUMN crawl_mode TEXT")
        except Exception:
            pass  # column already exists

        # Stream B: file_operations + duplicate_review tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_hash TEXT NOT NULL,
                operation TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                target_filename TEXT NOT NULL,
                original_filename TEXT,
                collision_step INTEGER,
                qdrant_points_updated INTEGER DEFAULT 0,
                performed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                reversed_at TEXT,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fileops_hash ON file_operations(doc_hash);

            CREATE TABLE IF NOT EXISTS duplicate_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_hash TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                sanitized_filename TEXT NOT NULL,
                collision_with_hash TEXT,
                collision_path TEXT,
                duplicate_path TEXT NOT NULL,
                domain TEXT,
                subdomain TEXT,
                book_author TEXT,
                book_title TEXT,
                status TEXT DEFAULT 'pending',
                resolution TEXT,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dupreview_status ON duplicate_review(status);

            CREATE TABLE IF NOT EXISTS scrape_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                language TEXT DEFAULT 'eng',
                category TEXT,
                status TEXT DEFAULT 'pending',
                page_count INTEGER DEFAULT 0,
                error_message TEXT,
                zim_filename TEXT,
                zim_source_id INTEGER,
                workspace_path TEXT,
                subprocess_pid INTEGER,
                additional_reject_patterns TEXT,
                skip_default_patterns INTEGER DEFAULT 0,
                crawl_mode TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scrape_status ON scrape_jobs(status);
        """)
        conn.commit()

    def add_to_catalogue(self, file_hash, filename, path, size_bytes, source, category):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO catalogue (hash, filename, path, size_bytes, source, category)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   path = excluded.path,
                   filename = excluded.filename,
                   source = excluded.source,
                   category = excluded.category,
                   path_updated_at = CASE
                       WHEN catalogue.path != excluded.path THEN CURRENT_TIMESTAMP
                       ELSE catalogue.path_updated_at
                   END""",
            (file_hash, filename, path, size_bytes, source, category)
        )
        conn.commit()

    def queue_document(self, file_hash):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM catalogue WHERE hash = ?", (file_hash,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE catalogue SET status = 'queued' WHERE hash = ?", (file_hash,))
        conn.execute(
            """INSERT INTO documents (hash, filename, path, size_bytes, status)
               VALUES (?, ?, ?, ?, 'queued')
               ON CONFLICT(hash) DO UPDATE SET
                   path = excluded.path,
                   filename = excluded.filename""",
            (row['hash'], row['filename'], row['path'], row['size_bytes'])
        )
        conn.commit()
        return True

    def update_status(self, file_hash, status, **kwargs):
        conn = self._get_conn()
        sets = ["status = ?"]
        vals = [status]

        ts_field = {
            'extracted': 'extracted_at',
            'enriched': 'enriched_at',
            'complete': 'embedded_at',
        }.get(status)
        if ts_field:
            sets.append(f"{ts_field} = ?")
            vals.append(datetime.now(timezone.utc).isoformat())

        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)

        vals.append(file_hash)
        conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE hash = ?", vals)
        conn.commit()

    def get_by_status(self, status, limit=None):
        conn = self._get_conn()
        q = "SELECT * FROM documents WHERE status = ? ORDER BY discovered_at"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(q, (status,)).fetchall()]

    def get_catalogued(self, source=None, category=None, limit=None):
        conn = self._get_conn()
        q = "SELECT * FROM catalogue WHERE status = 'catalogued'"
        params = []
        if source:
            q += " AND source = ?"
            params.append(source)
        if category:
            q += " AND category = ?"
            params.append(category)
        q += " ORDER BY discovered_at"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    def get_document(self, file_hash):
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM documents WHERE hash = ?", (file_hash,)).fetchone()
        return dict(row) if row else None

    def get_status_counts(self):
        conn = self._get_conn()
        cat_counts = {}
        for row in conn.execute("SELECT status, COUNT(*) as cnt FROM catalogue GROUP BY status"):
            cat_counts[row['status']] = row['cnt']

        doc_counts = {}
        for row in conn.execute("SELECT status, COUNT(*) as cnt FROM documents GROUP BY status"):
            doc_counts[row['status']] = row['cnt']

        return {'catalogue': cat_counts, 'documents': doc_counts}

    def get_failures(self):
        conn = self._get_conn()
        return [dict(r) for r in conn.execute(
            "SELECT * FROM documents WHERE status = 'failed' ORDER BY discovered_at"
        ).fetchall()]

    def mark_failed(self, file_hash, error_msg):
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET status = 'failed', error_message = ? WHERE hash = ?",
            (str(error_msg)[:1000], file_hash)
        )
        conn.commit()

    @staticmethod
    def _dir_has_pages(d):
        try:
            return bool(d) and os.path.isdir(d) and any(
                f.startswith('page_') and f.endswith('.txt') for f in os.listdir(d))
        except OSError:
            return False

    def locate_text_dir(self, file_hash):
        """First directory holding extracted page files for this document, or None.
        Checks documents.text_dir, the legacy data/text/<hash>, and processing/<hash>."""
        from .utils import get_config
        config = get_config()
        conn = self._get_conn()
        row = conn.execute("SELECT text_dir FROM documents WHERE hash = ?", (file_hash,)).fetchone()
        processing_root = config.get('pipeline', {}).get('processing_root', '/opt/recon/data/processing')
        for d in (row['text_dir'] if row else None,
                  os.path.join(config['paths']['text'], file_hash),
                  os.path.join(processing_root, file_hash)):
            if self._dir_has_pages(d):
                return d
        return None

    def find_source_pdf(self, file_hash):
        """Path of a PDF whose content hash equals file_hash, or None.

        Candidates: documents.path, processing/<hash>/source.pdf, catalogue.path, and the
        file_operations history (filing moves). Every candidate is verified by md5, so a
        renamed/replaced file is never mistaken for the original."""
        from .utils import get_config, content_hash
        config = get_config()
        conn = self._get_conn()
        processing_root = config.get('pipeline', {}).get('processing_root', '/opt/recon/data/processing')
        cands = []
        row = conn.execute("SELECT path FROM documents WHERE hash = ?", (file_hash,)).fetchone()
        if row and row['path']:
            cands.append(row['path'])
        cands.append(os.path.join(processing_root, file_hash, 'source.pdf'))
        row = conn.execute("SELECT path FROM catalogue WHERE hash = ?", (file_hash,)).fetchone()
        if row and row['path']:
            cands.append(row['path'])
        for r in conn.execute(
                "SELECT target_path, source_path FROM file_operations WHERE doc_hash = ? "
                "ORDER BY id DESC", (file_hash,)):
            cands.extend([r['target_path'], r['source_path']])
        seen = set()

        def verified(p):
            if not p or p in seen or not p.startswith('/') or not p.lower().endswith('.pdf'):
                return False
            seen.add(p)
            try:
                return os.path.isfile(p) and content_hash(p) == file_hash
            except OSError:
                return False

        for p in cands:
            if verified(p):
                return p
        # The filing worker moves finished PDFs into the library (not always logged in
        # file_operations): look for a library PDF of the same size or file name, md5-verified.
        drow = conn.execute("SELECT size_bytes, filename FROM documents WHERE hash = ?",
                            (file_hash,)).fetchone()
        if drow:
            by_size, by_name = _library_pdf_index(config)
            lib_cands = list(by_size.get(drow['size_bytes'], [])[:8]) if drow['size_bytes'] else []
            lib_cands += by_name.get(_norm_name(drow['filename'] or ''), [])[:8]
            for p in lib_cands:
                if verified(p):
                    return p
        return None

    def _plan_retry(self, file_hash):
        """Work out where a failed/queued document should resume. Returns a dict with
        target ('enriched' | 'extracted' | 'queued'), text_dir (dir with pages or None) and
        source (verified PDF path or None), or None when it cannot be retried."""
        from .utils import get_config
        conn = self._get_conn()
        row = conn.execute(
            "SELECT enriched_at, concepts_extracted FROM documents WHERE hash = ?",
            (file_hash,)).fetchone()
        if row is None:
            return None
        config = get_config()
        text_dir = self.locate_text_dir(file_hash)
        if text_dir is None:
            source = self.find_source_pdf(file_hash)
            if source is None:
                return None
            return {'target': 'queued', 'text_dir': None, 'source': source}
        if row['enriched_at'] and (row['concepts_extracted'] or 0) > 0:
            cdir = os.path.join(config['paths']['concepts'], file_hash)
            try:
                if os.path.isdir(cdir) and os.listdir(cdir):
                    return {'target': 'enriched', 'text_dir': text_dir, 'source': None}
            except OSError:
                pass
        return {'target': 'extracted', 'text_dir': text_dir, 'source': None}

    def retry_target(self, file_hash):
        """Stage a failed/queued document should resume at, or None if it cannot be retried.

        'enriched'  - enrichment finished (concepts on disk); re-run embedding
        'extracted' - extracted text with pages exists; re-run enrichment (this stage also
                      creates the concepts directory, so a missing concepts dir lands here)
        'queued'    - no extracted text, but the source PDF is still on disk (md5-verified);
                      the service's queued-extraction thread re-extracts it
        None        - no text and no source: unrecoverable, left failed
        """
        plan = self._plan_retry(file_hash)
        return plan['target'] if plan else None

    def increment_retry(self, file_hash, count_retry=True):
        """Re-queue a document to the stage it failed at. Returns the new status, or None
        if it cannot be retried (left untouched)."""
        plan = self._plan_retry(file_hash)
        if plan is None:
            return None
        sets = ["retry_count = retry_count + ?", "status = ?", "error_message = NULL"]
        vals = [1 if count_retry else 0, plan['target']]
        if plan['text_dir']:
            # The pages may live somewhere other than documents.text_dir (legacy data/text);
            # point the row at them so the next stage finds them.
            sets.append("text_dir = ?")
            vals.append(plan['text_dir'])
        if plan['source']:
            sets.append("path = ?")
            vals.append(plan['source'])
        vals.append(file_hash)
        conn = self._get_conn()
        conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE hash = ?", vals)
        conn.commit()
        return plan['target']

    def reset_extracting_to_queued(self):
        """Startup: documents left in 'extracting' belong to a previous run; requeue them."""
        conn = self._get_conn()
        cur = conn.execute("UPDATE documents SET status = 'queued' WHERE status = 'extracting'")
        conn.commit()
        return cur.rowcount

    def next_queued_pdf(self, min_age_minutes=10):
        """Oldest 'queued' document with a PDF path, old enough that a processor is not still
        in the middle of registering it (queue_document -> 'extracted' takes milliseconds)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM documents WHERE status = 'queued' AND lower(path) LIKE '%.pdf' "
            "AND discovered_at < datetime('now', ?) ORDER BY discovered_at LIMIT 1",
            (f'-{int(min_age_minutes)} minutes',)).fetchone()
        return dict(row) if row else None

    def get_sources(self):
        conn = self._get_conn()
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM catalogue ORDER BY source"
        ).fetchall()]

    def get_categories(self, source=None):
        conn = self._get_conn()
        if source:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM catalogue WHERE source = ? ORDER BY category", (source,)
            ).fetchall()]
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM catalogue ORDER BY category"
        ).fetchall()]

    def get_all_documents(self, status=None, source=None, category=None, limit=None, offset=None):
        conn = self._get_conn()
        q = """SELECT d.*, c.source, c.category FROM documents d
               LEFT JOIN catalogue c ON d.hash = c.hash WHERE 1=1"""
        params = []
        if status:
            q += " AND d.status = ?"
            params.append(status)
        if source:
            q += " AND c.source = ?"
            params.append(source)
        if category:
            q += " AND c.category = ?"
            params.append(category)
        q += " ORDER BY d.discovered_at DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        if offset:
            q += f" OFFSET {int(offset)}"
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    def count_documents(self, source=None, category=None):
        """Count documents matching optional source/category filters."""
        conn = self._get_conn()
        q = """SELECT COUNT(*) FROM documents d
               LEFT JOIN catalogue c ON d.hash = c.hash WHERE 1=1"""
        params = []
        if source:
            q += " AND c.source = ?"
            params.append(source)
        if category:
            q += " AND c.category = ?"
            params.append(category)
        return conn.execute(q, params).fetchone()[0]

    def catalogue_count(self):
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]

    def source_breakdown(self):
        conn = self._get_conn()
        return [dict(r) for r in conn.execute(
            "SELECT source, COUNT(*) as count, SUM(size_bytes) as total_bytes FROM catalogue GROUP BY source ORDER BY count DESC"
        ).fetchall()]

    def category_breakdown(self, source=None):
        conn = self._get_conn()
        if source:
            return [dict(r) for r in conn.execute(
                "SELECT category, COUNT(*) as count FROM catalogue WHERE source = ? GROUP BY category ORDER BY count DESC",
                (source,)
            ).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT source, category, COUNT(*) as count FROM catalogue GROUP BY source, category ORDER BY source, count DESC"
        ).fetchall()]

    def get_path_updates(self):
        """Get catalogue entries where path was updated since last sync."""
        conn = self._get_conn()
        return [dict(r) for r in conn.execute(
            "SELECT hash, filename, path, source, category FROM catalogue "
            "WHERE path_updated_at IS NOT NULL"
        ).fetchall()]

    def clear_path_update(self, file_hash):
        """Clear path_updated_at flag after Qdrant sync."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE catalogue SET path_updated_at = NULL WHERE hash = ?",
            (file_hash,)
        )
        conn.commit()

    def sync_document_path(self, file_hash, path, filename):
        """Update path and filename in documents table."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET path = ?, filename = ? WHERE hash = ?",
            (path, filename, file_hash)
        )
        conn.commit()

    def status_breakdown(self):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM catalogue GROUP BY status ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unorganized(self, limit=None):
        """Get completed documents that haven't been organized yet."""
        conn = self._get_conn()
        q = "SELECT hash, filename, path FROM documents WHERE status = 'complete' AND organized_at IS NULL ORDER BY embedded_at"
        if limit:
            q += " LIMIT {}".format(int(limit))
        return [dict(r) for r in conn.execute(q).fetchall()]


    def get_ingest_pending(self, ingest_dir, limit=50):
        """Get completed docs in _ingest/ that haven't been organized."""
        conn = self._get_conn()
        pattern = ingest_dir + '%'
        return [dict(r) for r in conn.execute(
            "SELECT hash, filename, path FROM documents "
            "WHERE status = 'complete' AND organized_at IS NULL AND path LIKE ? "
            "ORDER BY embedded_at LIMIT ?",
            (pattern, limit)
        ).fetchall()]

    def mark_organized(self, file_hash):
        """Mark a document as organized (sets organized_at timestamp)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET organized_at = CURRENT_TIMESTAMP WHERE hash = ?",
            (file_hash,)
        )
        conn.commit()

    def mark_filing_failed(self, file_hash, reason):
        """Record a persistent filing failure; the filing worker skips these."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET filing_error = ?, filing_failed_at = CURRENT_TIMESTAMP WHERE hash = ?",
            (reason, file_hash)
        )
        conn.commit()

    def get_filing_failures(self):
        """Documents the filing worker could not file (documents.filing_error set). Their
        source files stay in place (data/processing/<hash>); this only lists them."""
        conn = self._get_conn()
        return [dict(r) for r in conn.execute(
            "SELECT hash, filename, book_title, status, path, discovered_at, embedded_at, "
            "filing_error, filing_failed_at FROM documents WHERE filing_error IS NOT NULL "
            "ORDER BY filing_failed_at DESC, hash"
        ).fetchall()]

    def clear_filing_failed(self, file_hash):
        """Clear a filing failure so the worker retries the document."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET filing_error = NULL, filing_failed_at = NULL WHERE hash = ?",
            (file_hash,)
        )
        conn.commit()

    def update_catalogue_path(self, file_hash, new_path, new_filename):
        """Update catalogue path/filename and flag for Qdrant sync."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE catalogue SET path = ?, filename = ?, path_updated_at = CURRENT_TIMESTAMP WHERE hash = ?",
            (new_path, new_filename, file_hash)
        )
        conn.commit()


    # ── Scraper Job Helpers ─────────────────────────────────────

    def get_pending_scrape_job(self):
        """Fetch the oldest pending scrape job."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM scrape_jobs WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def update_scrape_job(self, job_id, **kwargs):
        """Update arbitrary columns on a scrape job."""
        if not kwargs:
            return
        conn = self._get_conn()
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        conn.execute(f"UPDATE scrape_jobs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()

    def get_scrape_jobs(self, status=None):
        """List scrape jobs, optionally filtered by status."""
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM scrape_jobs WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scrape_jobs ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scrape_job(self, job_id):
        """Get a single scrape job by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    # ── Stream B: File Operations ───────────────────────────────────

    def log_file_operation(self, doc_hash, operation, source_path, target_path,
                           source_filename, target_filename, original_filename=None,
                           collision_step=None, qdrant_points_updated=0, notes=None):
        """Log a file move/rename operation for audit trail and rollback."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO file_operations
               (doc_hash, operation, source_path, target_path,
                source_filename, target_filename, original_filename,
                collision_step, qdrant_points_updated, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_hash, operation, source_path, target_path,
             source_filename, target_filename, original_filename,
             collision_step, qdrant_points_updated, notes)
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_file_operations(self, doc_hash=None, limit=50):
        """Get file operations, optionally filtered by doc_hash."""
        conn = self._get_conn()
        if doc_hash:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM file_operations WHERE doc_hash = ? ORDER BY performed_at DESC LIMIT ?",
                (doc_hash, limit)
            ).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM file_operations WHERE reversed_at IS NULL ORDER BY performed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()]

    def get_file_operation(self, op_id):
        """Get a single file operation by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM file_operations WHERE id = ?", (op_id,)).fetchone()
        return dict(row) if row else None

    def mark_operation_reversed(self, op_id):
        """Mark a file operation as reversed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE file_operations SET reversed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (op_id,)
        )
        conn.commit()

    def queue_duplicate_review(self, doc_hash, original_filename, sanitized_filename,
                                collision_with_hash=None, collision_path=None,
                                duplicate_path='', domain=None, subdomain=None,
                                book_author=None, book_title=None):
        """Queue a file for human duplicate review."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO duplicate_review
               (doc_hash, original_filename, sanitized_filename,
                collision_with_hash, collision_path, duplicate_path,
                domain, subdomain, book_author, book_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_hash, original_filename, sanitized_filename,
             collision_with_hash, collision_path, duplicate_path,
             domain, subdomain, book_author, book_title)
        )
        conn.commit()

    def get_duplicate_reviews(self, status='pending', limit=50):
        """Get duplicate review queue."""
        conn = self._get_conn()
        return [dict(r) for r in conn.execute(
            "SELECT * FROM duplicate_review WHERE status = ? ORDER BY discovered_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()]

    def get_pipeline_stats(self):
        """Get Stream B pipeline statistics."""
        conn = self._get_conn()
        ops = conn.execute(
            "SELECT operation, COUNT(*) as cnt FROM file_operations WHERE reversed_at IS NULL GROUP BY operation"
        ).fetchall()
        dupes = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM duplicate_review GROUP BY status"
        ).fetchall()
        acquired = 0
        ingest = 0
        try:
            acquired_dir = get_config().get('new_pipeline', {}).get('acquired_dir', '')
            ingest_dir = get_config().get('new_pipeline', {}).get('ingest_dir', '')
            if acquired_dir and os.path.isdir(acquired_dir):
                acquired = len([f for f in os.listdir(acquired_dir) if f.lower().endswith('.pdf')])
            if ingest_dir and os.path.isdir(ingest_dir):
                ingest = len([f for f in os.listdir(ingest_dir) if f.lower().endswith('.pdf')])
        except Exception:
            pass
        return {
            'operations': {dict(r)['operation']: dict(r)['cnt'] for r in ops},
            'duplicates': {dict(r)['status']: dict(r)['cnt'] for r in dupes},
            'acquired_pending': acquired,
            'ingest_pending': ingest,
        }
