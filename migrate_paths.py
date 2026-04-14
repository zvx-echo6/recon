#!/usr/bin/env python3
"""One-time migration: rescan library to detect moved files and sync paths to Qdrant.

This rescans all PDFs in the library. The upsert in add_to_catalogue() will
detect any files whose paths changed since they were originally catalogued,
and flag them with path_updated_at. Then sync_qdrant_paths() propagates
those path changes to Qdrant download_url payloads.

Usage: cd /opt/recon && source venv/bin/activate && python3 migrate_paths.py [--dry-run]
"""
import sys
import os

sys.path.insert(0, '/opt/recon')

from recon import scan_library, sync_qdrant_paths
from lib.status import StatusDB
from lib.utils import setup_logging

logger = setup_logging('recon.migrate')


def main():
    dry_run = '--dry-run' in sys.argv

    db = StatusDB()
    conn = db._get_conn()

    total_cat = conn.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
    total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    print(f"Before: {total_cat} catalogue entries, {total_docs} documents")

    # Rescan library — upsert will detect and flag path changes
    print("\nScanning library (this will re-hash all files)...")
    count = scan_library()
    print(f"Scanned {count} PDFs")

    # Check how many paths changed
    updates = db.get_path_updates()
    print(f"\nDetected {len(updates)} path changes")

    if not updates:
        print("No paths need syncing — all up to date")
        return 0

    # Show what changed
    for row in updates[:20]:
        print(f"  {row['hash'][:8]} {row['filename']}")
    if len(updates) > 20:
        print(f"  ... and {len(updates) - 20} more")

    if dry_run:
        print(f"\n[DRY RUN] Would sync {len(updates)} paths to Qdrant. Re-run without --dry-run to apply.")
        return 0

    # Sync to Qdrant
    print(f"\nSyncing {len(updates)} paths to Qdrant...")
    synced = sync_qdrant_paths()
    print(f"Synced {synced} document paths to Qdrant")

    # Verify
    remaining = db.get_path_updates()
    if remaining:
        print(f"\nWARNING: {len(remaining)} paths still pending (Qdrant sync may have partially failed)")
    else:
        print("\nAll paths synced successfully")

    return 0


if __name__ == '__main__':
    sys.exit(main())
