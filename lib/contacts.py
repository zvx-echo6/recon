"""
RECON Contacts Database — per-user phone book with soft delete and proximity queries.

Separate DB at data/contacts.db. Thread-local connections with WAL mode (StatusDB pattern).
"""
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    label       TEXT NOT NULL,
    name        TEXT,
    call_sign   TEXT,
    phone       TEXT,
    email       TEXT,
    category    TEXT,
    notes       TEXT,
    lat         REAL,
    lon         REAL,
    osm_type    TEXT,
    osm_id      INTEGER,
    address     TEXT,
    show_proximity INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at  TEXT,
    deleted_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_contacts_user ON contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_contacts_user_category ON contacts(user_id, category);
CREATE INDEX IF NOT EXISTS idx_contacts_user_deleted ON contacts(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_contacts_geo ON contacts(lat, lon);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_home_work
    ON contacts(user_id, label)
    WHERE label IN ('Home', 'Work') AND deleted_at IS NULL;
"""


def _haversine_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters."""
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _row_to_dict(row):
    """Convert sqlite3.Row to dict, casting show_proximity to bool."""
    d = dict(row)
    d['show_proximity'] = bool(d.get('show_proximity', 0))
    return d


class ContactsDB:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'contacts.db')
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        if not hasattr(_local, 'contacts_conn') or _local.contacts_conn is None:
            _local.contacts_conn = sqlite3.connect(self.db_path, timeout=30)
            _local.contacts_conn.row_factory = sqlite3.Row
            _local.contacts_conn.execute("PRAGMA journal_mode=WAL")
            _local.contacts_conn.execute("PRAGMA busy_timeout=5000")
        return _local.contacts_conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def list_all(self, user_id, category=None, search=None):
        conn = self._get_conn()
        sql = "SELECT * FROM contacts WHERE user_id = ? AND deleted_at IS NULL"
        params = [user_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if search:
            sql += " AND (label LIKE ? OR name LIKE ? OR call_sign LIKE ? OR phone LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like, like])
        sql += " ORDER BY label"
        return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_deleted(self, user_id):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM contacts WHERE user_id = ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
            (user_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get(self, user_id, contact_id, include_deleted=False):
        conn = self._get_conn()
        sql = "SELECT * FROM contacts WHERE id = ? AND user_id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = conn.execute(sql, (contact_id, user_id)).fetchone()
        return _row_to_dict(row) if row else None

    def create(self, user_id, **fields):
        conn = self._get_conn()
        fields.pop('id', None)
        fields.pop('user_id', None)
        fields.pop('created_at', None)
        fields.pop('updated_at', None)
        fields.pop('deleted_at', None)
        fields.pop('deleted_by', None)
        if 'show_proximity' in fields:
            fields['show_proximity'] = 1 if fields['show_proximity'] else 0
        columns = ['user_id'] + list(fields.keys())
        placeholders = ', '.join(['?'] * len(columns))
        col_str = ', '.join(columns)
        values = [user_id] + list(fields.values())
        try:
            cur = conn.execute(f"INSERT INTO contacts ({col_str}) VALUES ({placeholders})", values)
            conn.commit()
            return self.get(user_id, cur.lastrowid), None
        except sqlite3.IntegrityError:
            return None, 'conflict'

    def update(self, user_id, contact_id, **fields):
        conn = self._get_conn()
        fields.pop('id', None)
        fields.pop('user_id', None)
        fields.pop('created_at', None)
        fields.pop('deleted_at', None)
        fields.pop('deleted_by', None)
        if 'show_proximity' in fields:
            fields['show_proximity'] = 1 if fields['show_proximity'] else 0
        fields['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        sets = ', '.join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [contact_id, user_id]
        conn.execute(f"UPDATE contacts SET {sets} WHERE id = ? AND user_id = ? AND deleted_at IS NULL", values)
        conn.commit()
        return self.get(user_id, contact_id)

    def soft_delete(self, user_id, contact_id):
        conn = self._get_conn()
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        conn.execute(
            "UPDATE contacts SET deleted_at = ?, deleted_by = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (now, user_id, contact_id, user_id)
        )
        conn.commit()
        return self.get(user_id, contact_id, include_deleted=True)

    def restore(self, user_id, contact_id):
        conn = self._get_conn()
        row = self.get(user_id, contact_id, include_deleted=True)
        if not row or not row.get('deleted_at'):
            return None, 'not_found'
        if row.get('label') in ('Home', 'Work'):
            existing = conn.execute(
                "SELECT id FROM contacts WHERE user_id = ? AND label = ? AND deleted_at IS NULL AND id != ?",
                (user_id, row['label'], contact_id)
            ).fetchone()
            if existing:
                return None, 'conflict'
        conn.execute(
            "UPDATE contacts SET deleted_at = NULL, deleted_by = NULL WHERE id = ? AND user_id = ?",
            (contact_id, user_id)
        )
        conn.commit()
        return self.get(user_id, contact_id), None

    def purge(self, user_id, contact_id):
        conn = self._get_conn()
        row = self.get(user_id, contact_id, include_deleted=True)
        if not row:
            return False, 'not_found'
        if not row.get('deleted_at'):
            return False, 'not_deleted'
        conn.execute("DELETE FROM contacts WHERE id = ? AND user_id = ?", (contact_id, user_id))
        conn.commit()
        return True, None

    def find_nearby(self, user_id, lat, lon, radius_m=75):
        conn = self._get_conn()
        # Bounding box pre-filter (~111km per degree lat)
        dlat = radius_m / 111_000
        dlon = radius_m / (111_000 * math.cos(math.radians(lat)))
        rows = conn.execute(
            """SELECT * FROM contacts
               WHERE user_id = ? AND deleted_at IS NULL AND show_proximity = 1
                 AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
            (user_id, lat - dlat, lat + dlat, lon - dlon, lon + dlon)
        ).fetchall()
        results = []
        for r in rows:
            dist = _haversine_m(lat, lon, r['lat'], r['lon'])
            if dist <= radius_m:
                d = _row_to_dict(r)
                d['distance_m'] = round(dist, 1)
                results.append(d)
        results.sort(key=lambda x: x['distance_m'])
        return results
