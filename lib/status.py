"""
RECON Status Tracker

SQLite operations for catalogue and documents tables. WAL mode, thread-local connections.
Status flow: catalogued -> queued -> extracting -> extracted -> enriching -> enriched -> embedding -> complete.

Config: paths.db
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .utils import get_config

_local = threading.local()


class StatusDB:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = get_config()['paths']['db']
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        if not hasattr(_local, 'conn') or _local.conn is None:
            _local.conn = sqlite3.connect(self.db_path, timeout=30)
            _local.conn.row_factory = sqlite3.Row
            _local.conn.execute("PRAGMA journal_mode=WAL")
            _local.conn.execute("PRAGMA busy_timeout=5000")
        return _local.conn

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

    def increment_retry(self, file_hash):
        conn = self._get_conn()
        conn.execute(
            "UPDATE documents SET retry_count = retry_count + 1, status = 'queued', error_message = NULL WHERE hash = ?",
            (file_hash,)
        )
        conn.commit()

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
