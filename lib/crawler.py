"""
RECON Site Crawler — URL discovery for bulk web ingestion.

Two discovery strategies:
1. Sitemap-based (preferred) — parses sitemap.xml for all URLs
2. Link-following (fallback) — crawls from root URL following internal links

Discovered URLs are fed into web_scraper.ingest_url() for processing.
"""

import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag

import requests
from lxml import etree

from .utils import get_config, setup_logging

logger = setup_logging('recon.crawler')


def _get_crawler_config(config=None):
    """Load crawler config with defaults."""
    if config is None:
        config = get_config()
    crawler_cfg = config.get('crawler', {})
    web_cfg = config.get('web_scraper', {})
    return {
        'user_agent': (
            crawler_cfg.get('user_agent') or
            web_cfg.get('user_agent') or
            'Mozilla/5.0 (compatible; RECON/1.0)'
        ),
        'fetch_timeout': crawler_cfg.get('fetch_timeout', 30),
        'rate_limit_delay': crawler_cfg.get('rate_limit_delay', 1.0),
        'max_pages': crawler_cfg.get('max_pages', 500),
        'max_depth': crawler_cfg.get('max_depth', 3),
        'default_exclude': crawler_cfg.get('default_exclude', [
            '/search', '/404', '/login', '/signup', '/auth/', '/api/', '/assets/', '/static/'
        ]),
    }


# ─── Sitemap Discovery ─────────────────────────────────────────────

def discover_sitemap_url(base_url, config=None):
    """
    Find the sitemap URL for a site.

    Checks: robots.txt Sitemap: directive, /sitemap.xml,
    /sitemap_index.xml, /sitemap-0.xml.

    Returns sitemap URL or None.
    """
    cfg = _get_crawler_config(config)
    headers = {'User-Agent': cfg['user_agent']}
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # Check robots.txt first
    try:
        resp = requests.get(
            f"{root}/robots.txt",
            headers=headers,
            timeout=cfg['fetch_timeout']
        )
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.strip().lower().startswith('sitemap:'):
                    sitemap_url = line.split(':', 1)[1].strip()
                    # Handle "Sitemap: https://..." — split(':',1) keeps the URL intact
                    # but "Sitemap: https://..." splits into "Sitemap" and " https://..."
                    # Need to rejoin properly
                    if not sitemap_url.startswith('http'):
                        sitemap_url = line[line.index(':') + 1:].strip()
                    logger.info(f"Found sitemap in robots.txt: {sitemap_url}")
                    return sitemap_url
    except Exception as e:
        logger.debug(f"robots.txt fetch failed: {e}")

    # Try common sitemap locations
    candidates = [
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
        f"{root}/sitemap-0.xml",
    ]

    for url in candidates:
        try:
            resp = requests.head(
                url,
                headers=headers,
                timeout=cfg['fetch_timeout'],
                allow_redirects=True
            )
            if resp.status_code == 200:
                logger.info(f"Found sitemap at: {url}")
                return url
        except Exception:
            continue

    logger.warning(f"No sitemap found for {base_url}")
    return None


def parse_sitemap(sitemap_url, config=None):
    """
    Parse a sitemap XML and return all page URLs.

    Handles standard sitemaps (<urlset>) and sitemap indexes
    (<sitemapindex>) with recursive sub-sitemap fetching.
    """
    cfg = _get_crawler_config(config)
    headers = {'User-Agent': cfg['user_agent']}
    all_urls = []

    def _fetch_and_parse(url, depth=0):
        if depth > 3:
            return

        try:
            resp = requests.get(url, headers=headers, timeout=cfg['fetch_timeout'])
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch sitemap {url}: {e}")
            return

        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as e:
            logger.error(f"Invalid XML in sitemap {url}: {e}")
            return

        nsmap = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

        # Check if this is a sitemap index
        sitemap_locs = root.findall('.//ns:sitemap/ns:loc', nsmap)
        if sitemap_locs:
            logger.info(f"Sitemap index at {url} — {len(sitemap_locs)} sub-sitemaps")
            for loc in sitemap_locs:
                if loc.text:
                    _fetch_and_parse(loc.text.strip(), depth + 1)
            return

        # Standard sitemap — extract URLs
        url_locs = root.findall('.//ns:loc', nsmap)

        # Fallback: try without namespace
        if not url_locs:
            url_locs = root.findall('.//loc')

        for loc in url_locs:
            if loc.text:
                all_urls.append(loc.text.strip())

        logger.info(f"Parsed {len(url_locs)} URLs from {url}")

    _fetch_and_parse(sitemap_url)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for url in all_urls:
        url_clean = urldefrag(url)[0]
        if url_clean not in seen:
            seen.add(url_clean)
            unique.append(url_clean)

    logger.info(f"Total unique URLs from sitemap: {len(unique)}")
    return unique


# ─── Link-Following Discovery (Fallback) ───────────────────────────

def crawl_links(base_url, max_depth=3, max_pages=500, config=None):
    """
    Discover URLs by following internal links (BFS).
    Fallback when no sitemap is available.
    """
    from bs4 import BeautifulSoup

    cfg = _get_crawler_config(config)
    headers = {'User-Agent': cfg['user_agent']}

    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc

    discovered = []
    visited = set()
    queue = deque([(base_url, 0)])

    skip_extensions = (
        '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.svg',
        '.css', '.js', '.zip', '.tar', '.gz', '.mp4', '.mp3',
        '.ico', '.woff', '.woff2', '.ttf', '.eot',
    )
    skip_paths = (
        '/tag/', '/tags/', '/page/', '/feed/', '/rss/',
        '/wp-json/', '/wp-admin/', '/wp-includes/',
    )

    while queue and len(discovered) < max_pages:
        url, depth = queue.popleft()
        url = urldefrag(url)[0]

        if url in visited:
            continue
        if depth > max_depth:
            continue

        visited.add(url)
        discovered.append(url)

        if depth >= max_depth:
            continue

        try:
            resp = requests.get(url, headers=headers, timeout=cfg['fetch_timeout'])
            if resp.status_code != 200:
                continue
            if 'text/html' not in resp.headers.get('content-type', ''):
                continue
        except Exception:
            continue

        try:
            soup = BeautifulSoup(resp.text, 'lxml')
        except Exception:
            continue

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            full_url = urljoin(url, href)
            full_url = urldefrag(full_url)[0]

            parsed = urlparse(full_url)
            if parsed.netloc != base_domain:
                continue
            if any(parsed.path.lower().endswith(ext) for ext in skip_extensions):
                continue
            if any(skip in parsed.path.lower() for skip in skip_paths):
                continue

            if full_url not in visited:
                queue.append((full_url, depth + 1))

        time.sleep(cfg['rate_limit_delay'])

    logger.info(f"Link crawl: {len(discovered)} URLs (visited {len(visited)}, depth {max_depth})")
    return discovered


# ─── URL Filtering ──────────────────────────────────────────────────

def filter_urls(urls, include=None, exclude=None):
    """
    Filter URLs by path prefix include/exclude rules.

    include: URL must match at least one prefix (if provided)
    exclude: URL must not match any prefix
    """
    filtered = []

    for url in urls:
        path = urlparse(url).path

        if include:
            if not any(path.startswith(prefix) for prefix in include):
                continue

        if exclude:
            if any(path.startswith(prefix) for prefix in exclude):
                continue

        filtered.append(url)

    logger.info(f"Filtered {len(urls)} -> {len(filtered)} URLs "
                f"(include={include}, exclude={exclude})")
    return filtered


# ─── Main Crawl Orchestrator ────────────────────────────────────────

def crawl_site(
    base_url,
    category='Web',
    source=None,
    include=None,
    exclude=None,
    max_pages=None,
    max_depth=None,
    delay=None,
    dry_run=False,
    use_sitemap=True,
    use_links=True,
    config=None,
):
    """
    Crawl a site and ingest all discovered pages.

    1. Discover URLs via sitemap or link-following
    2. Apply include/exclude filters
    3. Feed each URL through web_scraper.ingest_url()

    Returns summary dict with counts and per-URL results.
    """
    if config is None:
        config = get_config()
    cfg = _get_crawler_config(config)

    if max_pages is None:
        max_pages = cfg['max_pages']
    if max_depth is None:
        max_depth = cfg['max_depth']
    if delay is None:
        delay = cfg['rate_limit_delay']
    if source is None:
        source = urlparse(base_url).netloc

    logger.info(f"Crawling {base_url} (category={category}, max_pages={max_pages})")

    # ── Phase 1: Discover URLs ──

    urls = []
    discovery_method = None

    if use_sitemap:
        sitemap_url = discover_sitemap_url(base_url, config)
        if sitemap_url:
            urls = parse_sitemap(sitemap_url, config)
            discovery_method = 'sitemap'

    if not urls and use_links:
        logger.info("No sitemap URLs, falling back to link crawl...")
        urls = crawl_links(base_url, max_depth=max_depth, max_pages=max_pages, config=config)
        discovery_method = 'link_crawl'

    if not urls:
        logger.warning(f"No URLs discovered for {base_url}")
        return {
            'site': base_url,
            'discovery_method': None,
            'urls_discovered': 0,
            'urls_after_filter': 0,
            'results': [],
            'summary': {'total': 0, 'succeeded': 0, 'duplicates': 0, 'failed': 0},
        }

    # ── Phase 2: Filter URLs ──

    all_exclude = list(cfg['default_exclude'])
    if exclude:
        all_exclude.extend(exclude)

    urls = filter_urls(urls, include=include, exclude=all_exclude)

    if len(urls) > max_pages:
        logger.info(f"Limiting to {max_pages} pages (discovered {len(urls)})")
        urls = urls[:max_pages]

    logger.info(f"After filtering: {len(urls)} URLs to process")

    # ── Dry run ──

    if dry_run:
        return {
            'site': base_url,
            'discovery_method': discovery_method,
            'dry_run': True,
            'urls_discovered': len(urls),
            'urls': urls,
        }

    # ── Phase 3: Ingest each URL ──

    from .web_scraper import ingest_url

    results = []
    total = len(urls)

    for i, url in enumerate(urls, 1):
        logger.info(f"[{i}/{total}] Ingesting: {url}")

        try:
            result = ingest_url(url, category=category, source=source, config=config)
            result['url'] = url
            results.append(result)

            status = result.get('status', 'unknown')
            title = result.get('title', '')
            if status == 'duplicate':
                logger.info(f"  DUPLICATE: {title}")
            else:
                logger.info(f"  OK: {title} ({result.get('page_count', 0)} pages)")

        except Exception as e:
            logger.error(f"  FAILED: {url} -- {e}")
            results.append({
                'url': url,
                'status': 'failed',
                'error': str(e),
            })

        if i < total and delay > 0:
            time.sleep(delay)

    # ── Summary ──

    succeeded = sum(1 for r in results if r.get('status') not in ('failed', 'duplicate'))
    duplicates = sum(1 for r in results if r.get('status') == 'duplicate')
    failed = sum(1 for r in results if r.get('status') == 'failed')

    summary = {
        'total': len(results),
        'succeeded': succeeded,
        'duplicates': duplicates,
        'failed': failed,
    }

    logger.info(f"Crawl complete: {succeeded} new, {duplicates} duplicates, {failed} failed out of {total}")

    return {
        'site': base_url,
        'domain': urlparse(base_url).netloc,
        'category': category,
        'discovery_method': discovery_method,
        'urls_discovered': total,
        'results': results,
        'summary': summary,
    }
