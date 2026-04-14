#!/usr/bin/env python3
"""
aa_download_pass2.py — Second-pass downloader for books that failed in pass 1.

Reads the MD5 list from pass 1 report and tries:
  1. Z-Library search by title/author (separate catalog from Libgen)
  2. IPFS gateways using AA's IPFS CID (different from MD5 but findable)
  3. Alternative Libgen mirrors not tried in pass 1
  4. Direct AA slow download with longer timeout + retry

Checkpoint: saves progress to /opt/recon/data/aa_pass2_checkpoint.json
  so interrupted runs resume where they left off.

Usage:
  python3 /opt/recon/scripts/aa_download_pass2.py [--dry-run]
"""

import json
import time
import random
import logging
import hashlib
import argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

LOG_FILE       = Path("/opt/recon/logs/aa_download_pass2.log")
REPORT_IN      = Path.home() / "projects/recon/aa_acquisition_report.md"
REPORT_OUT     = Path.home() / "projects/recon/aa_acquisition_report_pass2.md"
CHECKPOINT     = Path("/opt/recon/data/aa_pass2_checkpoint.json")
BASE_LIB       = Path("/mnt/library/Acquired")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("aa_pass2")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept-Language": "en-US,en;q=0.9",
})

# ── Mirrors to try in order ───────────────────────────────────────────────────
MIRRORS = [
    # Libgen alternatives
    "https://libgen.li/ads.php?md5={md5}",
    "https://library.lol/main/{md5}",
    "https://libgen.rocks/get.php?md5={md5}",
    # Z-Library direct MD5 endpoint (sometimes works)
    "https://z-library.se/md5/{md5}",
    # IPFS public gateways — AA uses IPFS for storage
    "https://cloudflare-ipfs.com/ipfs/{md5}",
    "https://ipfs.io/ipfs/{md5}",
    "https://gateway.pinata.cloud/ipfs/{md5}",
]

# ── Books that failed in pass 1 — title, author, md5, subdir ─────────────────
PASS1_FAILURES = [
    # Medical/Herbalism
    ("The Earthwise Herbal Volume 1",         "Matthew Wood",         "fc8dc19f5a17f38849a3979830dc95c1", "Medical/Herbalism"),
    ("The Earthwise Herbal Volume 2",         "Matthew Wood",         "fc8dc19f5a17f38849a3979830dc95c1", "Medical/Herbalism"),
    ("Herbal Antibiotics",                    "Stephen Buhner",       "5839dab78edfdff0d7986fac62b814da", "Medical/Herbalism"),
    ("The Herbal Medicine-Maker's Handbook",  "James Green",          "27e8e8a3585705ed194029b69c7d61b1", "Medical/Herbalism"),
    ("Rosemary Gladstar's Medicinal Herbs",   "Rosemary Gladstar",    "9b1966f20a32ab4331bfece167be1dd0", "Medical/Herbalism"),

    # Medical/Austere
    ("Wilderness Medicine",                   "Paul Auerbach",        "957818eaa4ec40527bb05902f9ef7c51", "Medical/Austere"),
    ("Medicine for Mountaineering",           "James Wilkerson",      "39cb07998f2034206f0c9472e44cb0b4", "Medical/Austere"),

    # Medical/Veterinary
    ("The Chicken Health Handbook",           "Gail Damerow",         "0ba42fbea034b9a08ec8e2f8d7606efe", "Medical/Veterinary"),

    # Power
    ("The Renewable Energy Handbook",         "William Kemp",         "475d89fa80aea6c45aa4b1b4b9c5e274", "Power"),
    ("Homebrew Wind Power",                   "Dan Bartmann",         "0578696d5b1b6bceb3e5e3302c1a31aa", "Power"),
    ("Wind Energy Basics",                    "Paul Gipe",            "ccbe9d22e0a5e32d61921d20d66a8e05", "Power"),
    ("12-Volt Bible",                         "Brotherton",           "3f964fa6d730fdf2c3d3e231e87cf692", "Power"),
    ("Wiring a House",                        "Rex Cauldwell",        "5efcb53450e9eb560210eee40678adcf", "Power"),

    # Navigation
    ("Emergency Navigation",                  "David Burch",          "25e4def9e777b3fa9ca935134732ff9d", "Navigation"),

    # Water
    ("Water Storage",                         "Art Ludwig",           "17c965ec15c6cf4f09b5377b599a5266", "Water"),
    ("The Home Water Supply",                 "Stu Campbell",         "9b22677d2f8e8b39f7a6bf032187295b", "Water"),

    # Food
    ("Fermented Vegetables",                  "Kirsten Shockey",      "74d3bde876b4c17be66c21fdfa85213e", "Food"),
    ("The Art of Natural Cheesemaking",       "David Asher",          "bc0e0829d701fea9beca912d39f8cc74", "Food"),

    # Permaculture
    ("Edible Forest Gardens Volume 1",        "Dave Jacke",           "6b069c3bb077fdd89d487a363c070fbb", "Permaculture"),
    ("Edible Forest Gardens Volume 2",        "Dave Jacke",           "699255bfde7f69285c132a94ec291bf4", "Permaculture"),
    ("Creating a Forest Garden",              "Martin Crawford",      "96d71d70dba31ae86e14845f913e557e", "Permaculture"),
    ("Sepp Holzer's Permaculture",            "Sepp Holzer",          "32be55a9fce3e31cacd6912069abb410", "Permaculture"),
    ("The Permaculture Handbook",             "Peter Bane",           "08cb4492739fda4d01b5a868a408e4a0", "Permaculture"),
    ("The Market Gardener",                   "Jean-Martin Fortier",  "ac69f6c8c22305b42b539482dc761c19", "Permaculture"),

    # Scenario
    ("SAS Survival Handbook",                 "John Wiseman",         "fa967fd5fcbeb3c9887e22f73e590c64", "Scenario"),
    ("Pocket Ref",                            "Thomas Glover",        "8e4988ce513a4aa75e7e6c00ee36692b", "Scenario"),
    ("Deep Survival",                         "Laurence Gonzales",    "9a907ab13b81ea597407fffdb8ea1b04", "Scenario"),

    # Skills
    ("A Pattern Language",                    "Christopher Alexander","7f5cc06b5399b65a278c4005ccd8d476", "Skills"),
]


def load_checkpoint():
    """Load checkpoint: dict of {title: result_dict} for completed books."""
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text())
        except Exception:
            pass
    return {}


def save_checkpoint(completed):
    """Save checkpoint after each book."""
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(CHECKPOINT) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(completed, f, indent=2)
    Path(tmp).replace(CHECKPOINT)


def load_md5s_from_report():
    """Parse MD5 hashes from pass 1 report to pre-populate PASS1_FAILURES."""
    if not REPORT_IN.exists():
        return {}
    md5_map = {}
    for line in REPORT_IN.read_text().splitlines():
        if "`" in line and len(line) > 30:
            parts = line.split("|")
            if len(parts) >= 4:
                title = parts[1].strip()
                md5_cell = parts[3].strip().strip("`")
                if len(md5_cell) == 32 and md5_cell.isalnum():
                    md5_map[title.lower()] = md5_cell
    return md5_map


def search_zlib(title, author):
    """Try Z-Library search endpoint."""
    try:
        url = "https://z-library.se/s/"
        params = {"q": f"{title} {author}", "extension[]": "pdf"}
        r = SESSION.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # Z-lib book links contain /book/
        for a in soup.select("a[href*='/book/']")[:3]:
            href = a.get("href", "")
            if href:
                book_url = f"https://z-library.se{href}" if href.startswith("/") else href
                return book_url
    except Exception as e:
        log.debug(f"Zlib search failed: {e}")
    return None


def try_zlib_download(book_url, dest_path):
    """Download from Z-Library book page."""
    try:
        r = SESSION.get(book_url, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        dl = soup.select_one("a.addDownloadedBook, a[href*='/dl/'], a.btn-primary[href*='download']")
        if not dl:
            return False
        dl_url = dl["href"]
        if not dl_url.startswith("http"):
            dl_url = f"https://z-library.se{dl_url}"
        r2 = SESSION.get(dl_url, timeout=120, stream=True)
        if r2.status_code != 200:
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in r2.iter_content(8192):
                f.write(chunk)
        with open(dest_path, "rb") as f:
            if f.read(4) == b"%PDF":
                return True
        dest_path.unlink(missing_ok=True)
    except Exception as e:
        log.debug(f"Zlib download failed: {e}")
    return False


def try_mirrors(md5, dest_path):
    """Try all mirrors with the MD5."""
    import re as _re
    for tpl in MIRRORS:
        url = tpl.format(md5=md5)
        try:
            r = SESSION.get(url, timeout=20, stream=True, allow_redirects=True)
            if r.status_code != 200:
                continue
            ctype = r.headers.get("content-type", "")
            if "html" in ctype:
                soup = BeautifulSoup(r.text, "html.parser")
                # For libgen.li ads page, look for get.php with key
                dl = None
                match = _re.search(r'href="(get\.php\?md5=[^"]+)"', r.text)
                if match:
                    actual = f"https://libgen.li/{match.group(1)}"
                else:
                    dl = (soup.select_one("a[href*='.pdf']") or
                          soup.select_one("a[href*='get.php']") or
                          soup.select_one("a[href*='/get/']"))
                    if not dl:
                        continue
                    actual = dl["href"]
                    if not actual.startswith("http"):
                        base = url.split("/")[0] + "//" + url.split("/")[2]
                        actual = base + ("/" if not actual.startswith("/") else "") + actual

                r = SESSION.get(actual, timeout=60, stream=True)
                if r.status_code != 200:
                    continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            with open(dest_path, "rb") as f:
                if f.read(4) == b"%PDF":
                    size_mb = dest_path.stat().st_size / 1024 / 1024
                    log.info(f"    [OK] {size_mb:.1f}MB via {url}")
                    return True
            dest_path.unlink(missing_ok=True)
        except Exception as e:
            log.debug(f"Mirror {url} failed: {e}")
        time.sleep(2)
    return False


def get_ipfs_cids(md5):
    """Fetch IPFS CIDs from AA book detail page."""
    import re as _re
    cids = []
    try:
        r = SESSION.get(f"https://annas-archive.gl/md5/{md5}", timeout=20)
        if r.status_code == 200:
            for m in _re.finditer(r'ipfs_cid[:\s]+([A-Za-z0-9]{46,})', r.text):
                cids.append(m.group(1))
            # Also check for CIDs in href attributes
            for m in _re.finditer(r'ipfs://([A-Za-z0-9]{46,})', r.text):
                if m.group(1) not in cids:
                    cids.append(m.group(1))
    except Exception as e:
        log.debug(f"IPFS CID fetch failed: {e}")
    return cids


def try_ipfs_download(cids, dest_path):
    """Try downloading via IPFS public gateways."""
    gateways = [
        "https://cloudflare-ipfs.com/ipfs/{}",
        "https://dweb.link/ipfs/{}",
    ]
    for cid in cids[:3]:  # limit to first 3 CIDs
        for gw_tpl in gateways:
            url = gw_tpl.format(cid)
            try:
                r = SESSION.get(url, timeout=15, stream=True)
                if r.status_code != 200:
                    continue
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                with open(dest_path, "rb") as f:
                    if f.read(4) == b"%PDF":
                        size_mb = dest_path.stat().st_size / 1024 / 1024
                        log.info(f"    [OK] {size_mb:.1f}MB via IPFS {url[:60]}...")
                        return True
                dest_path.unlink(missing_ok=True)
            except Exception as e:
                log.debug(f"IPFS {url} failed: {e}")
            time.sleep(1)
    return False


def search_aa_fresh(title, author):
    """Fresh AA search on .gl domain for books that weren't found before."""
    for domain in ["annas-archive.gl", "annas-archive.se", "annas-archive.org"]:
        try:
            url = f"https://{domain}/search"
            params = {"q": f"{title} {author}", "ext": "pdf", "lang": "en"}
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href^='/md5/']"):
                text = a.get_text(" ", strip=True)
                if not text:
                    continue
                md5 = a["href"].split("/md5/")[-1].split("/")[0].strip()
                if len(md5) == 32:
                    if author.split()[-1].lower() in text.lower() or title.split()[0].lower() in text.lower():
                        return md5
        except Exception:
            continue
    return None


def process_book(title, author, md5_hint, subdir, dry_run):
    result = {
        "title": title, "author": author,
        "status": "NOT FOUND", "md5": md5_hint,
        "file": "", "notes": "",
    }

    safe_title  = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)[:60]
    safe_author = author.split()[-1]
    dest = BASE_LIB / subdir / f"{safe_title}_{safe_author}.pdf"

    if dest.exists():
        result["status"] = "ALREADY EXISTS"
        result["file"] = str(dest)
        return result

    if dry_run:
        result["status"] = "DRY RUN"
        return result

    # 1. Try Z-Library first (different catalog)
    log.info(f"  Trying Z-Library...")
    zlib_url = search_zlib(title, author)
    if zlib_url:
        if try_zlib_download(zlib_url, dest):
            result["status"] = "DOWNLOADED (Z-Library)"
            result["file"] = str(dest)
            return result

    # 2. If no MD5 from pass 1, do a fresh AA search
    md5 = md5_hint
    if not md5:
        log.info(f"  Searching AA for fresh MD5...")
        md5 = search_aa_fresh(title, author)
        if md5:
            result["md5"] = md5
            log.info(f"  Found MD5: {md5}")

    # 3. Try IPFS with real CIDs from AA detail page
    if md5:
        log.info(f"  Fetching IPFS CIDs from AA...")
        cids = get_ipfs_cids(md5)
        if cids:
            log.info(f"  Found {len(cids)} IPFS CID(s), trying gateways...")
            if try_ipfs_download(cids, dest):
                result["status"] = "DOWNLOADED (IPFS)"
                result["file"] = str(dest)
                return result

    # 4. Try all mirrors with MD5
    if md5:
        log.info(f"  Trying mirrors with MD5 {md5}...")
        if try_mirrors(md5, dest):
            result["status"] = "DOWNLOADED (mirror)"
            result["file"] = str(dest)
            return result
        result["status"] = "MD5 ONLY"
        result["notes"] = f"MD5 confirmed, all mirrors failed: {md5}"
    else:
        result["notes"] = "Not found on AA or Z-Library"

    return result


def write_report(results):
    downloaded = [r for r in results if "DOWNLOADED" in r["status"]]
    md5_only   = [r for r in results if r["status"] == "MD5 ONLY"]
    not_found  = [r for r in results if r["status"] == "NOT FOUND"]
    existing   = [r for r in results if r["status"] == "ALREADY EXISTS"]

    lines = [
        "# AA Acquisition Report -- Pass 2",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Searched:** {len(results)} | **Downloaded:** {len(downloaded)} | "
        f"**MD5 only:** {len(md5_only)} | **Not found:** {len(not_found)}",
        "",
    ]
    if downloaded:
        lines += ["## Downloaded", "",
                  "| Title | Author | Via | File |",
                  "|-------|--------|-----|------|"]
        for r in downloaded:
            lines.append(f"| {r['title']} | {r['author']} | {r['status']} | `{Path(r['file']).name}` |")
        lines.append("")

    if existing:
        lines += ["## Already in Library", "",
                  "| Title | Author |",
                  "|-------|--------|"]
        for r in existing:
            lines.append(f"| {r['title']} | {r['author']} |")
        lines.append("")

    if md5_only:
        lines += ["## MD5 Known -- All Mirrors Failed", "",
                  "| Title | Author | MD5 |",
                  "|-------|--------|-----|"]
        for r in md5_only:
            lines.append(f"| {r['title']} | {r['author']} | `{r['md5']}` |")
        lines.append("")

    if not_found:
        lines += ["## Not Found Anywhere", "",
                  "| Title | Author | Notes |",
                  "|-------|--------|-------|"]
        for r in not_found:
            lines.append(f"| {r['title']} | {r['author']} | {r['notes']} |")
        lines.append("")

    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text("\n".join(lines))
    log.info(f"Report written to {REPORT_OUT}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Load any MD5s captured in pass 1
    md5_map = load_md5s_from_report()
    targets = []
    for title, author, md5_hint, subdir in PASS1_FAILURES:
        md5 = md5_hint or md5_map.get(title.lower(), "")
        targets.append((title, author, md5, subdir))

    # Load checkpoint
    completed = load_checkpoint()
    if completed:
        log.info(f"Resuming: {len(completed)} books already processed in previous run")

    log.info(f"Pass 2: {len(targets)} books | dry_run={args.dry_run}")
    results = []
    for i, (title, author, md5, subdir) in enumerate(targets, 1):
        # Check checkpoint — skip already-processed books
        if title in completed and not args.dry_run:
            result = completed[title]
            results.append(result)
            log.info(f"[{i}/{len(targets)}] {title} — SKIPPED (checkpoint: {result['status']})")
            continue

        log.info(f"[{i}/{len(targets)}] {title} -- {author}")
        result = process_book(title, author, md5, subdir, args.dry_run)
        results.append(result)
        log.info(f"  -> {result['status']}")

        # Save checkpoint after each book (not in dry-run)
        if not args.dry_run:
            completed[title] = result
            save_checkpoint(completed)

        time.sleep(random.uniform(6, 12))

    write_report(results)
    print(f"\n-- Pass 2 Summary ----------------------------------------")
    for status in ["DOWNLOADED (Z-Library)", "DOWNLOADED (IPFS)", "DOWNLOADED (mirror)", "MD5 ONLY", "NOT FOUND", "ALREADY EXISTS", "DRY RUN"]:
        count = sum(1 for r in results if r["status"] == status)
        if count:
            print(f"  {status:<35} {count:>3}")
    print(f"  Report: {REPORT_OUT}")


if __name__ == "__main__":
    main()
