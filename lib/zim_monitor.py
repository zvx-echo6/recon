"""
ZIM Monitor — detects ZIMs loaded in kiwix-serve and tracks them in recon.db.

Polls the kiwix-serve OPDS v2 catalog, compares against the zim_sources table,
and for new ZIMs reads accurate metadata via python-libzim's Counter field.

Standalone:  python3 /opt/recon/lib/zim_monitor.py
As module:   from lib.zim_monitor import scan_zims
"""
import logging
import os
import sqlite3
import sys
import urllib.request
from xml.etree import ElementTree as ET

sys.path.insert(0, "/opt/recon")
from lib.utils import setup_logging

try:
    from libzim.reader import Archive
    HAVE_LIBZIM = True
except ImportError:
    HAVE_LIBZIM = False

OPDS_URL = "http://localhost:8430/catalog/v2/entries?count=-1"
ZIM_DIR = "/mnt/kiwix"
DB_PATH = "/opt/recon/data/recon.db"

ATOM_NS = "http://www.w3.org/2005/Atom"

logger = logging.getLogger("recon.zim_monitor")


def _text(element, tag, ns=ATOM_NS):
    """Get text content of a child element, or None."""
    child = element.find(f"{{{ns}}}{tag}")
    if child is not None and child.text:
        return child.text.strip()
    return None


def parse_counter(counter_str):
    """Parse ZIM Counter metadata into {mimetype: count}."""
    result = {}
    for pair in counter_str.split(";"):
        if "=" in pair:
            mime, count = pair.split("=", 1)
            try:
                result[mime.strip()] = int(count.strip())
            except ValueError:
                pass
    return result


def fetch_opds():
    """Fetch OPDS v2 catalog from kiwix-serve. Returns list of dicts."""
    try:
        with urllib.request.urlopen(OPDS_URL, timeout=10) as resp:
            data = resp.read()
    except Exception as e:
        logger.error("Failed to fetch OPDS catalog: %s", e)
        return []

    root = ET.fromstring(data)
    entries = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        uuid_raw = _text(entry, "id")
        uuid = uuid_raw.replace("urn:uuid:", "") if uuid_raw else None

        # Derive ZIM filename from the content link href
        zim_filename = None
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.get("type") == "text/html":
                href = link.get("href", "")
                # href looks like /content/appropedia_en_all_maxi_2025-11
                name = href.rsplit("/", 1)[-1] if "/" in href else href
                if name:
                    zim_filename = name + ".zim"
                break

        entries.append({
            "uuid": uuid,
            "title": _text(entry, "title"),
            "name": _text(entry, "name"),
            "flavour": _text(entry, "flavour"),
            "language": _text(entry, "language"),
            "category": _text(entry, "category") or None,
            "summary": _text(entry, "summary"),
            "article_count_opds": int(_text(entry, "articleCount") or 0),
            "zim_filename": zim_filename,
        })
    return entries


def get_libzim_metadata(zim_path):
    """Open a ZIM file and read accurate metadata via python-libzim."""
    if not HAVE_LIBZIM:
        logger.warning("python-libzim not available, skipping metadata read")
        return {}

    zim = Archive(zim_path)
    meta = {}

    def _get_meta(key):
        try:
            return zim.get_metadata(key).decode("utf-8", errors="replace")
        except RuntimeError:
            return None

    meta["title"] = _get_meta("Title")
    meta["description"] = _get_meta("Description")
    meta["language"] = _get_meta("Language")
    meta["tags"] = _get_meta("Tags")

    counter_str = _get_meta("Counter")
    if counter_str:
        counts = parse_counter(counter_str)
        meta["article_count"] = counts.get("text/html", 0)
        meta["counter_raw"] = counter_str
    else:
        meta["article_count"] = 0
        meta["counter_raw"] = None

    return meta


def scan_zims():
    """Compare OPDS catalog against zim_sources table. Insert/update as needed."""
    logger.info("Scanning kiwix-serve OPDS catalog...")
    opds_entries = fetch_opds()
    if not opds_entries:
        logger.info("No entries in OPDS catalog (or fetch failed)")
        return

    logger.info("OPDS returned %d entries", len(opds_entries))

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Get existing zim_sources keyed by filename
    existing = {}
    for row in con.execute("SELECT id, zim_filename, status FROM zim_sources"):
        existing[row["zim_filename"]] = dict(row)

    opds_filenames = set()
    new_count = 0

    for entry in opds_entries:
        filename = entry["zim_filename"]
        if not filename:
            logger.warning("Skipping OPDS entry with no derivable filename: %s", entry)
            continue

        opds_filenames.add(filename)

        if filename in existing:
            logger.debug("Already tracked: %s (status=%s)", filename, existing[filename]["status"])
            continue

        # New ZIM — read accurate metadata via python-libzim
        zim_path = os.path.join(ZIM_DIR, filename)
        if not os.path.isfile(zim_path):
            logger.warning("ZIM file not found on disk: %s", zim_path)
            continue

        logger.info("New ZIM detected: %s — reading metadata via libzim", filename)
        meta = get_libzim_metadata(zim_path)

        con.execute(
            """INSERT INTO zim_sources
               (zim_filename, zim_path, zim_uuid, title, description,
                language, category, article_count, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'detected')""",
            (
                filename,
                zim_path,
                entry["uuid"],
                meta.get("title") or entry["title"],
                meta.get("description") or entry["summary"],
                meta.get("language") or entry["language"],
                entry["category"],
                meta.get("article_count", 0),
            ),
        )
        new_count += 1
        logger.info(
            "  Inserted: %s — title=%r, articles=%s (OPDS said %s)",
            filename,
            meta.get("title") or entry["title"],
            meta.get("article_count", 0),
            entry["article_count_opds"],
        )

    # Detect removed ZIMs (in DB but not in OPDS, and not already marked removed)
    removed_count = 0
    for filename, row in existing.items():
        if filename not in opds_filenames and row["status"] != "removed":
            con.execute(
                "UPDATE zim_sources SET status = 'removed' WHERE id = ?",
                (row["id"],),
            )
            removed_count += 1
            logger.info("Marked removed: %s", filename)

    con.commit()
    con.close()

    logger.info(
        "Scan complete: %d new, %d removed, %d total in catalog",
        new_count, removed_count, len(opds_entries),
    )


if __name__ == "__main__":
    setup_logging("recon.zim_monitor")
    scan_zims()
