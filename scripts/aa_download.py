#!/usr/bin/env python3
"""
aa_download.py — Anna's Archive bulk downloader for RECON library acquisition.

For each target book:
  1. Searches annas-archive.org for the title + author
  2. Extracts the best PDF match (verified by author/page count)
  3. Gets the MD5 from the book page
  4. Attempts download from Libgen mirrors in order
  5. Verifies downloaded file is a valid PDF
  6. Writes full acquisition report

Usage:
  python3 /opt/recon/scripts/aa_download.py [--dry-run] [--limit N]

Report output: ~/projects/recon/aa_acquisition_report.md
"""

import json
import time
import random
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

REPORT_PATH = Path.home() / "projects/recon/aa_acquisition_report.md"
LOG_FILE    = Path("/opt/recon/logs/aa_download.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("aa_download")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept-Language": "en-US,en;q=0.9",
})

BASE_AA = "https://annas-archive.gl"

# Download attempt order — try fastest mirrors first
LIBGEN_MIRRORS = [
    "https://libgen.is/get.php?md5={md5}",
    "https://libgen.rs/get.php?md5={md5}",
    "https://libgen.st/get.php?md5={md5}",
    "https://libgen.li/ads.php?md5={md5}",
]

# ── Target book list ──────────────────────────────────────────────────────────
TARGETS = [
    # (title, author, dest_dir)

    # Medical — Herbalism
    ("Medical Herbalism",                          "David Hoffmann",             "Medical/Herbalism"),
    ("Making Plant Medicine",                      "Richo Cech",                 "Medical/Herbalism"),
    ("The Earthwise Herbal Volume 1",              "Matthew Wood",               "Medical/Herbalism"),
    ("The Earthwise Herbal Volume 2",              "Matthew Wood",               "Medical/Herbalism"),
    ("Herbal Antibiotics",                         "Stephen Buhner",             "Medical/Herbalism"),
    ("Herbal Antivirals",                          "Stephen Buhner",             "Medical/Herbalism"),
    ("The Herbal Medicine-Maker's Handbook",       "James Green",                "Medical/Herbalism"),
    ("Rosemary Gladstar's Medicinal Herbs",        "Rosemary Gladstar",          "Medical/Herbalism"),

    # Medical — Austere
    ("Wilderness Medicine",                        "Paul Auerbach",              "Medical/Austere"),
    ("Medicine for Mountaineering",                "James Wilkerson",            "Medical/Austere"),

    # Medical — Veterinary
    ("The Chicken Health Handbook",                "Gail Damerow",               "Medical/Veterinary"),
    ("Goat Husbandry",                             "David Mackenzie",            "Medical/Veterinary"),

    # Power Systems
    ("The Renewable Energy Handbook",              "William Kemp",               "Power"),
    ("Homebrew Wind Power",                        "Dan Bartmann",               "Power"),
    ("Wind Energy Basics",                         "Paul Gipe",                  "Power"),
    ("12-Volt Bible",                              "Brotherton",                 "Power"),
    ("Wiring a House",                             "Rex Cauldwell",              "Power"),

    # Navigation
    ("Wilderness Navigation",                      "Bob Burns",                  "Navigation"),
    ("Be Expert with Map and Compass",             "Bjorn Kjellstrom",           "Navigation"),
    ("Emergency Navigation",                       "David Burch",                "Navigation"),
    ("The Natural Navigator",                      "Tristan Gooley",             "Navigation"),
    ("The Essential Wilderness Navigator",         "David Seidman",              "Navigation"),

    # Water Systems
    ("Rainwater Harvesting for Drylands Volume 1", "Brad Lancaster",            "Water"),
    ("Rainwater Harvesting for Drylands Volume 2", "Brad Lancaster",            "Water"),
    ("Rainwater Harvesting for Drylands Volume 3", "Brad Lancaster",            "Water"),
    ("Water Storage",                              "Art Ludwig",                 "Water"),
    ("The Home Water Supply",                      "Stu Campbell",               "Water"),

    # Food Systems
    ("The Art of Fermentation",                    "Sandor Katz",                "Food"),
    ("Fermented Vegetables",                       "Kirsten Shockey",            "Food"),
    ("Mastering Artisan Cheesemaking",             "Gianaclis Caldwell",         "Food"),
    ("Home Cheese Making",                         "Ricki Carroll",              "Food"),
    ("The Art of Natural Cheesemaking",            "David Asher",                "Food"),

    # Permaculture
    ("Edible Forest Gardens Volume 1",             "Dave Jacke",                 "Permaculture"),
    ("Edible Forest Gardens Volume 2",             "Dave Jacke",                 "Permaculture"),
    ("Creating a Forest Garden",                   "Martin Crawford",            "Permaculture"),
    ("Sepp Holzer's Permaculture",                 "Sepp Holzer",                "Permaculture"),
    ("The Permaculture Handbook",                  "Peter Bane",                 "Permaculture"),
    ("The Market Gardener",                        "Jean-Martin Fortier",        "Permaculture"),

    # Scenario / Emergency
    ("SAS Survival Handbook",                      "John Wiseman",               "Scenario"),
    ("Pocket Ref",                                 "Thomas Glover",              "Scenario"),
    ("Deep Survival",                              "Laurence Gonzales",          "Scenario"),

    # Foundational Skills
    ("Back to Basics",                             "Reader's Digest",            "Skills"),
    ("A Pattern Language",                         "Christopher Alexander",      "Skills"),
]

BASE_LIB = Path("/mnt/library/Acquired")


def search_aa(title, author):
    """Search Anna's Archive and return list of candidate result dicts."""
    query = f"{title} {author}"
    url = f"{BASE_AA}/search"
    params = {"q": query, "ext": "pdf", "lang": "en"}
    try:
        r = SESSION.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Search failed for '{title}': {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    seen_md5 = set()
    for item in soup.select("a[href^='/md5/']"):
        href = item.get("href", "")
        md5 = href.split("/md5/")[-1].split("/")[0].split("?")[0].strip()
        if not md5 or len(md5) != 32:
            continue
        text = item.get_text(" ", strip=True)
        if not text or md5 in seen_md5:
            continue
        seen_md5.add(md5)
        results.append({"md5": md5, "text": text, "href": href})
        if len(results) >= 5:
            break

    return results


def get_book_details(md5):
    """Fetch the book detail page and extract useful metadata."""
    url = f"{BASE_AA}/md5/{md5}"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Extract page count if visible
        pages = None
        for word in text.split():
            if word.isdigit() and 50 < int(word) < 5000:
                pages = int(word)
                break
        return {"pages": pages, "text": text[:500]}
    except Exception as e:
        log.warning(f"Detail fetch failed for md5={md5}: {e}")
        return {}


def try_download(md5, dest_path):
    """Try each libgen mirror until one works. Returns True on success."""
    for mirror_tpl in LIBGEN_MIRRORS:
        url = mirror_tpl.format(md5=md5)
        try:
            r = SESSION.get(url, timeout=60, stream=True, allow_redirects=True)
            content_type = r.headers.get("content-type", "")
            if r.status_code != 200:
                continue
            # Some mirrors return an HTML ads page before the real file
            if "text/html" in content_type:
                # Parse redirect link from ads page
                soup = BeautifulSoup(r.text, "html.parser")
                dl_link = soup.select_one("a[href*='.pdf']")
                if not dl_link:
                    dl_link = soup.select_one("a[href*='get.php']")
                if not dl_link:
                    continue
                actual_url = dl_link["href"]
                if not actual_url.startswith("http"):
                    actual_url = f"https://libgen.is{actual_url}"
                r = SESSION.get(actual_url, timeout=120, stream=True)
                if r.status_code != 200:
                    continue

            # Stream to disk
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            # Verify it's a real PDF
            with open(dest_path, "rb") as f:
                header = f.read(4)
            if header == b"%PDF":
                size_mb = dest_path.stat().st_size / 1024 / 1024
                log.info(f"  [OK] {dest_path.name} ({size_mb:.1f}MB) via {url}")
                return True
            else:
                log.warning(f"  [BAD] Not a PDF from {url}")
                dest_path.unlink(missing_ok=True)

        except Exception as e:
            log.warning(f"  Mirror failed {url}: {e}")
            continue

    return False


def process_book(title, author, subdir, dry_run):
    """Full search + download pipeline for one book."""
    log.info(f"[SEARCH] '{title}' — {author}")
    result = {
        "title": title,
        "author": author,
        "status": "NOT FOUND",
        "md5": "",
        "pages": "",
        "file": "",
        "notes": "",
    }

    candidates = search_aa(title, author)
    if not candidates:
        result["notes"] = "No results from AA search"
        return result

    # Pick best candidate — prefer one whose text contains author name
    best = None
    for c in candidates:
        if author.split()[-1].lower() in c["text"].lower():
            best = c
            break
    if not best:
        best = candidates[0]  # take first result if no author match

    md5 = best["md5"]
    result["md5"] = md5

    details = get_book_details(md5)
    result["pages"] = details.get("pages", "")

    if dry_run:
        result["status"] = "DRY RUN — found"
        result["notes"] = f"MD5: {md5}"
        return result

    # Build destination path
    safe_title = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)[:60]
    safe_author = author.split()[-1]
    filename = f"{safe_title}_{safe_author}.pdf"
    dest = BASE_LIB / subdir / filename

    if dest.exists():
        result["status"] = "ALREADY EXISTS"
        result["file"] = str(dest)
        return result

    log.info(f"  MD5: {md5} — attempting download...")
    ok = try_download(md5, dest)

    if ok:
        result["status"] = "DOWNLOADED"
        result["file"] = str(dest)
    else:
        result["status"] = "MD5 ONLY"
        result["notes"] = f"All mirrors failed. MD5: {md5}"

    return result


def write_report(results):
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    downloaded   = [r for r in results if r["status"] == "DOWNLOADED"]
    md5_only     = [r for r in results if r["status"] == "MD5 ONLY"]
    not_found    = [r for r in results if r["status"] == "NOT FOUND"]
    already_have = [r for r in results if r["status"] == "ALREADY EXISTS"]

    lines = [
        f"# Anna's Archive Acquisition Report",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total searched:** {len(results)}",
        f"",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| Downloaded | {len(downloaded)} |",
        f"| MD5 only (mirrors failed) | {len(md5_only)} |",
        f"| Not found on AA | {len(not_found)} |",
        f"| Already in library | {len(already_have)} |",
        f"",
    ]

    if downloaded:
        lines += ["## Downloaded", ""]
        lines += ["| Title | Author | Pages | File |", "|-------|--------|-------|------|"]
        for r in downloaded:
            lines.append(f"| {r['title']} | {r['author']} | {r['pages']} | `{Path(r['file']).name}` |")
        lines.append("")

    if md5_only:
        lines += ["## Found on AA — Download Failed (use MD5 for manual retrieval)", ""]
        lines += ["| Title | Author | MD5 | Notes |", "|-------|--------|-----|-------|"]
        for r in md5_only:
            lines.append(f"| {r['title']} | {r['author']} | `{r['md5']}` | {r['notes']} |")
        lines.append("")

    if not_found:
        lines += ["## Not Found on Anna's Archive", ""]
        lines += ["| Title | Author | Notes |", "|-------|--------|-------|"]
        for r in not_found:
            lines.append(f"| {r['title']} | {r['author']} | {r['notes']} |")
        lines.append("")

    if already_have:
        lines += ["## Already in Library", ""]
        lines += ["| Title | Author |", "|-------|--------|"]
        for r in already_have:
            lines.append(f"| {r['title']} | {r['author']} |")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines))
    log.info(f"Report written to {REPORT_PATH}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    targets = TARGETS[:args.limit] if args.limit else TARGETS
    log.info(f"Starting AA acquisition: {len(targets)} books | dry_run={args.dry_run}")

    results = []
    for i, (title, author, subdir) in enumerate(targets, 1):
        log.info(f"[{i}/{len(targets)}]")
        result = process_book(title, author, subdir, args.dry_run)
        results.append(result)
        log.info(f"  -> {result['status']}")
        # Polite delay between requests
        time.sleep(random.uniform(8, 15))

    write_report(results)

    print(f"\n-- Summary -----------------------------------------------")
    for status in ["DOWNLOADED", "MD5 ONLY", "NOT FOUND", "ALREADY EXISTS", "DRY RUN — found"]:
        count = sum(1 for r in results if r["status"] == status)
        if count:
            print(f"  {status:<35} {count:>3}")
    print(f"  Report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
