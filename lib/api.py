"""
RECON Web Dashboard & API

Flask app on port 8420. Jinja2 templates + static files.
Pages: Knowledge (Dashboard, Catalogue, Upload, Web Ingest, Failures),
       PeerTube (Dashboard, Channels), Search, Settings (Keys, Cookies, VPN, Health).
API endpoints for all pipeline operations including crawl, ingest, and search.

Dependencies: Flask, qdrant-client, requests
Config: web, vector_db, embedding sections of config.yaml
"""
import glob
import json
import threading
import os
import shutil
import tempfile

import requests as http_requests
from flask import Flask, request, jsonify, redirect, render_template
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from werkzeug.utils import secure_filename

from .utils import get_config, content_hash, clean_filename_to_title, derive_source_and_category, generate_download_url, setup_logging
from .status import StatusDB
from .deployment_config import get_deployment_config

logger = setup_logging('recon.api')

# ── Background cache warmer ──
# All expensive queries run proactively so API endpoints never block.
_cache = {
    'knowledge_stats': None,
    'pt_dashboard': None,
    'qdrant_scroll': None,
    'qdrant_scroll_ts': 0,
    'quick_stats': None,
    'kiwix_sources': None,
}

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'),
            static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'))

app.config['MAX_CONTENT_LENGTH'] = None  # ZIM files can be multi-GB


# ── Large ZIM upload support ──
# Override stream factory so ZIM uploads write directly to /mnt/kiwix/
# instead of /tmp (which is on the 96GB root disk and can't hold 100GB+ ZIMs).
from flask import Request as _FlaskRequest

class _LargeZimRequest(_FlaskRequest):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        if filename and filename.lower().endswith('.zim'):
            return tempfile.NamedTemporaryFile('wb+', dir='/mnt/kiwix', prefix='.upload_', suffix='.tmp', delete=False)
        return super()._get_file_stream(total_content_length, content_type, filename, content_length)

app.request_class = _LargeZimRequest
# ── Netsyms Blueprint ──
from .netsyms_api import netsyms_bp
app.register_blueprint(netsyms_bp)

# ── Wiki-enrich Blueprint (extraction #5 prep — HTTP wrapper over wiki_index) ──
from .wiki_enrich_api import wiki_enrich_bp
app.register_blueprint(wiki_enrich_bp)

# ── Wiki-rewrite Blueprint (extraction #5 prep — HTTP wrapper over rewrite_wiki_link) ──
from .wiki_rewrite_api import wiki_rewrite_bp
app.register_blueprint(wiki_rewrite_bp)



# ── Navigation Constants ──

KNOWLEDGE_SUBNAV = [
    {'href': '/', 'label': 'Dashboard'},
    {'href': '/catalogue', 'label': 'Catalogue'},
    {'href': '/upload', 'label': 'Upload'},
    {'href': '/web-ingest', 'label': 'Web Ingest'},
    {'href': '/failures', 'label': 'Failures'},
]

PEERTUBE_SUBNAV = [
    {'href': '/peertube', 'label': 'Dashboard'},
    {'href': '/peertube/channels', 'label': 'Channels'},
]


KIWIX_SUBNAV = [
    {'href': '/kiwix', 'label': 'Library'},
    {'href': '/kiwix/scraper', 'label': 'Scraper'},
]
SETTINGS_SUBNAV = [
    {'href': '/settings/keys', 'label': 'API Keys'},
    {'href': '/settings/cookies', 'label': 'YouTube Cookies'},
    {'href': '/settings/vpn', 'label': 'NordVPN'},
    {'href': '/settings/health', 'label': 'Service Health'},
]


def _format_source_citation(payload):
    """Format a human-readable citation from a search result payload."""
    book = payload.get('book_title', '')
    if not book:
        book = clean_filename_to_title(payload.get('filename', 'Unknown'))
    page = payload.get('page_ref', '')
    if page:
        page_str = str(page)
        if not page_str.startswith('p'):
            page_str = f"p. {page_str}"
        return f"{book}, {page_str}"
    return book


ALLOWED_EXTENSIONS = {'.pdf', '.txt', '.epub', '.doc', '.docx', '.mobi'}

HOPPER_ROUTING = {
    '.pdf':  '/opt/recon/data/acquired/pdf/',
    '.txt':  '/opt/recon/data/acquired/text/',
    '.epub': '/opt/recon/data/acquired/pdf/',
    '.doc':  '/opt/recon/data/acquired/pdf/',
    '.docx': '/opt/recon/data/acquired/pdf/',
    '.mobi': '/opt/recon/data/acquired/pdf/',
}


def _process_upload(filepath, original_filename, ext, category, config, db):
    """Process an upload: hash, dedup, drop into hopper for dispatcher pickup."""
    file_hash = content_hash(filepath)

    conn = db._get_conn()
    existing = conn.execute("SELECT * FROM catalogue WHERE hash = ?", (file_hash,)).fetchone()
    if existing:
        raise ValueError(f"Duplicate: file already catalogued as {existing['filename']}")

    # Also check if already sitting in a hopper dir awaiting dispatch
    for hopper in HOPPER_ROUTING.values():
        if any(os.path.exists(os.path.join(hopper, file_hash + e)) for e in ALLOWED_EXTENSIONS):
            raise ValueError("Duplicate: file already queued for processing")

    hopper_dir = HOPPER_ROUTING.get(ext, '/opt/recon/data/acquired/pdf/')
    os.makedirs(hopper_dir, exist_ok=True)

    target_path = os.path.join(hopper_dir, file_hash + ext)
    meta_path = os.path.join(hopper_dir, file_hash + '.meta.json')

    stem = os.path.splitext(original_filename)[0]
    sidecar = {
        'title': stem,
        'source': 'dashboard_upload',
        'source_type': ext.lstrip('.'),
        'category': category,
        'original_filename': original_filename,
    }

    # Write sidecar first (with .tmp safety), then content
    tmp_meta = meta_path + '.tmp'
    with open(tmp_meta, 'w', encoding='utf-8') as f:
        json.dump(sidecar, f, indent=2)
    os.rename(tmp_meta, meta_path)

    shutil.copy2(filepath, target_path)

    return {
        'hash': file_hash,
        'filename': original_filename,
        'source_type': ext.lstrip('.'),
        'status': 'queued',
    }


# ── Page Routes ──

@app.route('/')
def dashboard():
    return render_template('knowledge/dashboard.html',
                           domain='knowledge', subnav=KNOWLEDGE_SUBNAV, active_page='/')


@app.route('/search')
def search_page():
    query = request.args.get('q', '')
    if not query:
        return render_template('search.html', domain='search', subnav=None, active_page='/search')

    config = get_config()
    limit = int(request.args.get('limit', 20))
    source_filter = request.args.get('source_type', None)

    try:
        from .embedder import get_embedding_single
        query_vector = get_embedding_single(query, config)

        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=60
        )

        search_filter = None
        if source_filter:
            search_filter = Filter(must=[
                FieldCondition(key="source_type", match=MatchValue(value=source_filter))
            ])

        results = qdrant.query_points(
            collection_name=config['vector_db']['collection'],
            query=query_vector,
            limit=limit,
            query_filter=search_filter
        ).points

        formatted = []
        for r in results:
            p = r.payload
            raw_dom = p.get('domain', [])
            if isinstance(raw_dom, str):
                domains = [raw_dom] if raw_dom else []
            elif isinstance(raw_dom, list):
                domains = raw_dom
            else:
                domains = []
            formatted.append({
                'score': r.score,
                'title': p.get('title', 'Untitled'),
                'summary': p.get('summary', p.get('content', '')[:200]),
                'citation': _format_source_citation(p),
                'download_url': p.get('download_url', ''),
                'source_type': p.get('source_type', 'document'),
                'knowledge_type': p.get('knowledge_type', ''),
                'complexity': p.get('complexity', ''),
                'domains': domains,
            })

        return render_template('search.html', domain='search', subnav=None, active_page='/search',
                               query=query, results=formatted)

    except Exception as e:
        return render_template('search.html', domain='search', subnav=None, active_page='/search',
                               query=query, error=str(e))


@app.route('/catalogue')
def catalogue_page():
    db = StatusDB()
    source = request.args.get('source', None)
    category = request.args.get('category', None)
    per_page = int(request.args.get('per_page', 50))
    page = int(request.args.get('page', 1))
    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    total_count = db.count_documents(source=source, category=category)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    docs = db.get_all_documents(source=source, category=category, limit=per_page, offset=offset)
    sources = db.get_sources()

    return render_template('knowledge/catalogue.html',
                           domain='knowledge', subnav=KNOWLEDGE_SUBNAV, active_page='/catalogue',
                           docs=docs, sources=sources, current_source=source,
                           page=page, per_page=per_page, total_pages=total_pages, total_count=total_count)


@app.route('/upload')
def upload_page():
    db = StatusDB()
    config = get_config()

    upload_paths = config.get('upload_paths', {})
    categories = sorted(k for k in upload_paths if k != 'default')
    db_sources = db.get_sources()
    for s in db_sources:
        if s not in categories:
            categories.append(s)

    options_html = ''.join(f'<option value="{c}">' for c in categories)
    recent = db.get_all_documents(limit=20)

    return render_template('knowledge/upload.html',
                           domain='knowledge', subnav=KNOWLEDGE_SUBNAV, active_page='/upload',
                           options_html=options_html, recent=recent)


@app.route('/web-ingest')
def web_ingest_page():
    db = StatusDB()
    config = get_config()

    upload_paths = config.get('upload_paths', {})
    categories = sorted(k for k in upload_paths if k != 'default')
    db_sources = db.get_sources()
    for s in db_sources:
        if s not in categories:
            categories.append(s)
    if 'Web' not in categories:
        categories.insert(0, 'Web')
    options_html = ''.join(f'<option value="{c}">' for c in categories)

    conn = db._get_conn()
    web_docs = [dict(r) for r in conn.execute(
        """SELECT d.*, c.source, c.category FROM documents d
           LEFT JOIN catalogue c ON d.hash = c.hash
           WHERE d.path LIKE 'http%'
           ORDER BY d.discovered_at DESC LIMIT 20"""
    ).fetchall()]

    return render_template('knowledge/web_ingest.html',
                           domain='knowledge', subnav=KNOWLEDGE_SUBNAV, active_page='/web-ingest',
                           options_html=options_html, web_docs=web_docs)


@app.route('/failures')
def failures_page():
    db = StatusDB()
    failures = db.get_failures()
    return render_template('knowledge/failures.html',
                           domain='knowledge', subnav=KNOWLEDGE_SUBNAV, active_page='/failures',
                           failures=failures)


@app.route('/peertube')
def peertube_dashboard():
    return render_template('peertube/dashboard.html',
                           domain='peertube', subnav=PEERTUBE_SUBNAV, active_page='/peertube')


@app.route('/peertube/channels')
def peertube_channels():
    return render_template('peertube/channels.html',
                           domain='peertube', subnav=PEERTUBE_SUBNAV, active_page='/peertube/channels')


@app.route('/settings/keys')
def settings_keys():
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    keys_data = km.get_masked_keys()
    return render_template('settings/keys.html',
                           domain='settings', subnav=SETTINGS_SUBNAV, active_page='/settings/keys',
                           keys_data=keys_data)


@app.route('/settings/cookies')
def settings_cookies():
    return render_template('settings/cookies.html',
                           domain='settings', subnav=SETTINGS_SUBNAV, active_page='/settings/cookies')


@app.route('/settings/vpn')
def settings_vpn():
    return render_template('settings/vpn.html',
                           domain='settings', subnav=SETTINGS_SUBNAV, active_page='/settings/vpn')


@app.route('/settings/health')
def settings_health():
    return render_template('settings/health.html',
                           domain='settings', subnav=SETTINGS_SUBNAV, active_page='/settings/health')


# ── Backward-compat redirects ──

@app.route('/keys')
def keys_redirect():
    return redirect('/settings/keys', code=301)


# ── API Endpoints ──

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'error': f'Unsupported file type: {ext}'}), 400

    category = request.form.get('category', '').strip()

    config = get_config()
    db = StatusDB()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
    try:
        file.save(tmp_path)

        if os.path.getsize(tmp_path) == 0:
            return jsonify({'error': 'Uploaded file is empty'}), 400

        result = _process_upload(tmp_path, file.filename, ext, category, config, db)
        return jsonify(result), 201

    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return jsonify({'error': f'Upload failed: {e}'}), 500
    finally:
        os.close(tmp_fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.route('/api/upload/<doc_hash>/status')
def api_upload_status(doc_hash):
    db = StatusDB()
    config = get_config()
    doc = db.get_document(doc_hash)

    if not doc:
        conn = db._get_conn()
        cat = conn.execute("SELECT * FROM catalogue WHERE hash = ?", (doc_hash,)).fetchone()
        if cat:
            return jsonify({
                'hash': doc_hash,
                'filename': cat['filename'],
                'status': cat['status'],
            })

        # Check hopper dirs for files awaiting dispatcher pickup
        for hopper in ('/opt/recon/data/acquired/pdf/', '/opt/recon/data/acquired/text/'):
            if glob.glob(os.path.join(hopper, doc_hash + '.*')):
                return jsonify({
                    'hash': doc_hash,
                    'status': 'pending',
                    'message': 'Waiting for dispatcher',
                })

        # Check processing dir
        proc_dir = os.path.join(
            config.get('pipeline', {}).get('processing_root', '/opt/recon/data/processing'),
            doc_hash,
        )
        if os.path.isdir(proc_dir):
            return jsonify({
                'hash': doc_hash,
                'status': 'processing',
                'message': 'Being processed',
            })

        return jsonify({'error': 'Document not found'}), 404

    result = {
        'hash': doc_hash,
        'filename': doc['filename'],
        'status': doc['status'],
        'book_title': doc.get('book_title'),
        'concepts_extracted': doc.get('concepts_extracted', 0),
        'vectors_inserted': doc.get('vectors_inserted', 0),
        'error_message': doc.get('error_message'),
    }

    if doc.get('path'):
        library_root = config['library_root']
        book_server = config.get('book_server', {})
        base_url = book_server.get('base_url', 'https://files.echo6.co')
        if not doc['path'].startswith('http'):
            result['download_url'] = generate_download_url(doc['path'], library_root, base_url)
        else:
            result['source_url'] = doc['path']

    return jsonify(result)


@app.route('/api/upload/categories')
def api_upload_categories():
    config = get_config()
    db = StatusDB()

    upload_paths = config.get('upload_paths', {})
    categories = {}

    for name in upload_paths:
        if name != 'default':
            categories[name] = {'name': name, 'configured': True, 'count': 0}

    sources = db.source_breakdown()
    for s in sources:
        name = s['source']
        if name in categories:
            categories[name]['count'] = s['count']
        else:
            categories[name] = {'name': name, 'configured': False, 'count': s['count']}

    result = sorted(categories.values(), key=lambda x: x['name'])
    return jsonify(result)


@app.route('/api/quick-stats')
def api_quick_stats():
    """Serve pre-cached quick stats (never blocks)."""
    if _cache['quick_stats'] is None:
        return jsonify({'catalogued': 0, 'in_pipeline': 0, 'vectors': 0})
    return jsonify(_cache['quick_stats'])


@app.route('/api/retry-all', methods=['POST'])
def api_retry_all():
    """Retry all failed documents."""
    db = StatusDB()
    failures = db.get_failures()
    count = 0
    for f in failures:
        db.increment_retry(f['hash'])
        count += 1
    return jsonify({'ok': True, 'count': count})


@app.route('/api/ingest-url', methods=['POST'])
def api_ingest_url():
    """Ingest content from a URL."""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'url is required'}), 400

    url = data['url'].strip()
    category = data.get('category', 'Web')

    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL — must start with http:// or https://'}), 400

    process = data.get('process', False)

    try:
        from .web_scraper import ingest_url
        result = ingest_url(url, category=category, source='web')

        if result['status'] == 'duplicate':
            return jsonify(result), 409

        if process and result['status'] != 'duplicate':
            from .enricher import run_enrichment
            from .embedder import run_embedding
            enriched = run_enrichment()
            embedded = run_embedding()
            result['pipeline'] = {'enriched': enriched, 'embedded': embedded}

        return jsonify(result), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        logger.error(f"URL ingestion failed: {e}")
        return jsonify({'error': f'Ingestion failed: {str(e)}'}), 500


@app.route('/api/ingest-urls', methods=['POST'])
def api_ingest_urls():
    """Batch ingest content from multiple URLs."""
    data = request.get_json()
    if not data or 'urls' not in data:
        return jsonify({'error': 'urls array is required'}), 400

    urls = data['urls']
    category = data.get('category', 'Web')

    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({'error': 'urls must be a non-empty array'}), 400

    if len(urls) > 50:
        return jsonify({'error': 'Maximum 50 URLs per batch'}), 400

    process = data.get('process', False)

    from .web_scraper import ingest_urls
    results = ingest_urls(urls, category=category, source='web', delay=0.5)

    pipeline_info = {}
    if process:
        new_count = sum(1 for r in results if r.get('status') not in ('failed', 'duplicate'))
        if new_count > 0:
            from .enricher import run_enrichment
            from .embedder import run_embedding
            enriched = run_enrichment()
            embedded = run_embedding()
            pipeline_info = {'enriched': enriched, 'embedded': embedded}

    return jsonify({
        'results': results,
        'pipeline': pipeline_info,
        'summary': {
            'total': len(results),
            'succeeded': sum(1 for r in results if r.get('status') not in ('failed', 'duplicate')),
            'duplicates': sum(1 for r in results if r.get('status') == 'duplicate'),
            'failed': sum(1 for r in results if r.get('status') == 'failed')
        }
    }), 200


@app.route('/api/crawl', methods=['POST'])
def api_crawl():
    """Crawl a site and ingest discovered pages."""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'url is required'}), 400

    base_url = data['url'].strip()
    if not base_url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL — must start with http:// or https://'}), 400

    category = data.get('category', 'Web')
    source = data.get('source')
    include = data.get('include')
    exclude = data.get('exclude')
    max_pages = data.get('max_pages', 500)
    max_depth = data.get('max_depth', 3)
    delay = data.get('delay', 1.0)
    dry_run = data.get('dry_run', False)
    use_sitemap = data.get('use_sitemap', True)

    from .crawler import crawl_site

    if dry_run:
        result = crawl_site(
            base_url=base_url, category=category, source=source,
            include=include, exclude=exclude, max_pages=max_pages,
            max_depth=max_depth, delay=delay, dry_run=True, use_sitemap=use_sitemap,
        )
        return jsonify(result), 200

    crawl_id = f"crawl_{hash(base_url) & 0xFFFFFFFF:08x}_{int(__import__('time').time())}"

    def _run_crawl():
        try:
            _crawl_results[crawl_id] = {'status': 'running', 'stage': 'ingesting', 'site': base_url}
            result = crawl_site(
                base_url=base_url, category=category, source=source,
                include=include, exclude=exclude, max_pages=max_pages,
                max_depth=max_depth, delay=delay, dry_run=False, use_sitemap=use_sitemap,
            )

            _crawl_results[crawl_id] = {'status': 'running', 'stage': 'enriching', 'site': base_url,
                                         'crawl_summary': result.get('summary', {})}
            logger.info(f"Crawl {crawl_id}: ingestion done, running enrichment...")
            from .enricher import run_enrichment
            enriched = run_enrichment()
            logger.info(f"Crawl {crawl_id}: enriched {enriched} documents")

            _crawl_results[crawl_id] = {'status': 'running', 'stage': 'embedding', 'site': base_url,
                                         'crawl_summary': result.get('summary', {}), 'enriched': enriched}
            logger.info(f"Crawl {crawl_id}: running embedding...")
            from .embedder import run_embedding
            embedded = run_embedding()
            logger.info(f"Crawl {crawl_id}: embedded {embedded} documents")

            result['pipeline'] = {'enriched': enriched, 'embedded': embedded}
            _crawl_results[crawl_id] = result
            logger.info(f"Crawl {crawl_id} complete: {result.get('summary', {})}, enriched={enriched}, embedded={embedded}")
        except Exception as e:
            _crawl_results[crawl_id] = {'error': str(e), 'status': 'failed'}
            logger.error(f"Crawl {crawl_id} failed: {e}")

    _crawl_results[crawl_id] = {'status': 'running', 'stage': 'ingesting', 'site': base_url}
    t = threading.Thread(target=_run_crawl, daemon=True)
    t.start()

    return jsonify({
        'crawl_id': crawl_id,
        'status': 'started',
        'site': base_url,
        'message': f'Crawl started in background. Check /api/crawl/{crawl_id}/status'
    }), 202


_crawl_results = {}


@app.route('/api/crawl/<crawl_id>/status')
def api_crawl_status(crawl_id):
    """Check the status of a background crawl."""
    if crawl_id not in _crawl_results:
        return jsonify({'error': 'Crawl not found'}), 404
    return jsonify(_crawl_results[crawl_id]), 200


_peertube_results = {}


@app.route('/api/ingest-peertube', methods=['POST'])
def api_ingest_peertube():
    """Ingest PeerTube video transcripts."""
    data = request.get_json() or {}
    channel = data.get('channel')
    since = data.get('since')
    process = data.get('process', False)

    from .peertube_scraper import ingest_channel, ingest_all

    job_id = f"pt_{hash(channel or 'all') & 0xFFFFFFFF:08x}_{int(__import__('time').time())}"

    def _run_ingest():
        try:
            _peertube_results[job_id] = {'status': 'running', 'stage': 'ingesting',
                                          'channel': channel or 'all'}
            if channel:
                result = ingest_channel(channel, since=since)
            else:
                result = ingest_all(since=since)

            summary = result.get('summary', {})
            _peertube_results[job_id] = {
                'status': 'running', 'stage': 'enriching',
                'channel': channel or 'all', 'ingest_summary': summary,
            }

            if process:
                logger.info(f"PeerTube {job_id}: ingestion done, running enrichment...")
                from .enricher import run_enrichment
                enriched = run_enrichment()
                logger.info(f"PeerTube {job_id}: enriched {enriched} documents")

                _peertube_results[job_id]['stage'] = 'embedding'
                from .embedder import run_embedding
                embedded = run_embedding()
                logger.info(f"PeerTube {job_id}: embedded {embedded} documents")

                summary['enriched'] = enriched
                summary['embedded'] = embedded

            result['status'] = 'complete'
            _peertube_results[job_id] = result

        except Exception as e:
            logger.error(f"PeerTube ingestion {job_id} failed: {e}", exc_info=True)
            _peertube_results[job_id] = {'error': str(e), 'status': 'failed'}

    _peertube_results[job_id] = {'status': 'running', 'stage': 'starting',
                                  'channel': channel or 'all'}
    t = threading.Thread(target=_run_ingest, daemon=True)
    t.start()

    return jsonify({
        'job_id': job_id,
        'status': 'started',
        'channel': channel or 'all',
        'message': f'PeerTube ingestion started. Check /api/ingest-peertube/{job_id}/status'
    }), 202


@app.route('/api/ingest-peertube/<job_id>/status')
def api_peertube_status(job_id):
    """Check status of a PeerTube ingestion job."""
    if job_id not in _peertube_results:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(_peertube_results[job_id]), 200


@app.route('/api/peertube/stats')
def api_peertube_stats():
    """Get PeerTube instance and ingestion statistics."""
    from .peertube_scraper import get_instance_stats
    stats = get_instance_stats()
    return jsonify(stats), 200


@app.route('/api/search', methods=['POST'])
def api_search():
    config = get_config()
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({'error': 'Missing query'}), 400

    query = data['query']
    limit = data.get('limit', 20)
    source_type = data.get('source_type', None)

    try:
        from .embedder import get_embedding_single
        query_vector = get_embedding_single(query, config)

        qdrant = QdrantClient(
            host=config['vector_db']['host'],
            port=config['vector_db']['port'],
            timeout=60
        )

        search_filter = None
        if source_type:
            search_filter = Filter(must=[
                FieldCondition(key="source_type", match=MatchValue(value=source_type))
            ])

        results = qdrant.query_points(
            collection_name=config['vector_db']['collection'],
            query=query_vector,
            limit=limit,
            query_filter=search_filter
        ).points

        formatted = []
        for r in results:
            p = r.payload
            formatted.append({
                'score': r.score,
                'citation': _format_source_citation(p),
                'title': p.get('title', ''),
                'summary': p.get('summary', ''),
                'content': p.get('content', ''),
                'book_title': p.get('book_title', ''),
                'book_author': p.get('book_author', ''),
                'page_ref': p.get('page_ref', ''),
                'download_url': p.get('download_url', ''),
                'domain': p.get('domain', []),
                'subdomain': p.get('subdomain', []),
                'keywords': p.get('keywords', []),
                'knowledge_type': p.get('knowledge_type', ''),
                'complexity': p.get('complexity', ''),
                'key_facts': p.get('key_facts', []),
                'source': p.get('source', ''),
                'source_type': p.get('source_type', 'document'),
                'doc_hash': p.get('doc_hash', ''),
            })

        return jsonify({'query': query, 'results': formatted})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/status')
def api_status():
    db = StatusDB()
    return jsonify(db.get_status_counts())


@app.route('/api/retry/<file_hash>', methods=['POST'])
def api_retry(file_hash):
    db = StatusDB()
    db.increment_retry(file_hash)
    return redirect('/failures')


@app.route('/api/ingest', methods=['POST'])
def api_ingest():
    from .ingester import ingest_intel
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    config = get_config()
    result = ingest_intel(data, config)
    if result is not None:
        return jsonify({'intel_id': result})
    return jsonify({'error': 'Ingestion failed'}), 500


def _qdrant_scroll(host, port, collection, req):
    """Full scroll of Qdrant vectors for domain/knowledge_type/complexity counts. Cached externally."""
    domain_counts = {}
    knowledge_type_counts = {}
    complexity_counts = {}
    source_type_counts = {}
    sample_size = 0
    try:
        offset = None
        while True:
            body = {"limit": 500, "with_payload": ["domain", "knowledge_type", "complexity", "source_type"]}
            if offset is not None:
                body["offset"] = offset
            resp = req.post(
                f"http://{host}:{port}/collections/{collection}/points/scroll",
                json=body, timeout=15
            )
            if resp.status_code != 200:
                break
            result = resp.json().get('result', {})
            points = result.get('points', [])
            if not points:
                break
            sample_size += len(points)
            for p in points:
                payload = p.get('payload', {})
                raw_domain = payload.get('domain')
                if isinstance(raw_domain, str):
                    domain_list = [raw_domain] if raw_domain else []
                elif isinstance(raw_domain, list):
                    domain_list = raw_domain
                else:
                    domain_list = []
                for d in domain_list:
                    domain_counts[d] = domain_counts.get(d, 0) + 1
                kt = payload.get('knowledge_type')
                if kt:
                    knowledge_type_counts[kt] = knowledge_type_counts.get(kt, 0) + 1
                cx = payload.get('complexity')
                if cx:
                    complexity_counts[cx] = complexity_counts.get(cx, 0) + 1
                st = payload.get('source_type', 'unknown')
                source_type_counts[st] = source_type_counts.get(st, 0) + 1
            next_offset = result.get('next_page_offset')
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.debug(f"Qdrant scroll failed: {e}")
    return {'domains': domain_counts, 'knowledge_types': knowledge_type_counts, 'complexities': complexity_counts, 'source_types': source_type_counts, 'sample_size': sample_size}


def _build_knowledge_stats():
    """Build full knowledge stats (runs in background warmer)."""
    import requests as req
    import time as _time

    config = get_config()
    db = StatusDB()
    conn = db._get_conn()

    totals = conn.execute("""
        SELECT
            COUNT(*) as total_docs,
            SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete_docs,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_docs,
            SUM(CASE WHEN status NOT IN ('complete', 'failed') THEN 1 ELSE 0 END) as in_pipeline,
            SUM(COALESCE(concepts_extracted, 0)) as total_concepts,
            SUM(COALESCE(vectors_inserted, 0)) as total_vectors,
            SUM(COALESCE(pages_extracted, 0)) as total_pages
        FROM documents
    """).fetchone()

    pipeline = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM documents
        GROUP BY status
        ORDER BY CASE status
            WHEN 'queued' THEN 1 WHEN 'extracting' THEN 2 WHEN 'extracted' THEN 3
            WHEN 'enriching' THEN 4 WHEN 'enriched' THEN 5 WHEN 'embedding' THEN 6
            WHEN 'complete' THEN 7 WHEN 'failed' THEN 8 ELSE 9
        END
    """).fetchall()

    sources = conn.execute("""
        SELECT
            c.source,
            CASE
              WHEN c.source = 'stream.echo6.co' THEN 'transcript'
              WHEN c.source = 'kiwix' THEN 'wiki'
              WHEN c.path LIKE 'http%' THEN 'web'
              ELSE 'pdf'
            END as type,
            COUNT(DISTINCT c.hash) as catalogued,
            COUNT(DISTINCT CASE WHEN d.status = 'complete' THEN d.hash END) as complete,
            COUNT(DISTINCT CASE WHEN d.status NOT IN ('complete', 'failed') AND d.status IS NOT NULL THEN d.hash END) as in_pipeline,
            COALESCE(SUM(CASE WHEN d.status = 'complete' THEN d.concepts_extracted ELSE 0 END), 0) as concepts,
            COALESCE(SUM(CASE WHEN d.status = 'complete' THEN d.vectors_inserted ELSE 0 END), 0) as vectors,
            COALESCE(SUM(CASE WHEN d.status = 'complete' THEN d.pages_extracted ELSE 0 END), 0) as pages
        FROM catalogue c
        LEFT JOIN documents d ON c.hash = d.hash
        GROUP BY c.source, type
        ORDER BY catalogued DESC
    """).fetchall()

    qdrant_host = config.get('vector_db', {}).get('host', '100.64.0.14')
    qdrant_port = config.get('vector_db', {}).get('port', 6333)
    collection = config.get('vector_db', {}).get('collection', 'recon_knowledge')

    qdrant_stats = {}
    try:
        resp = req.get(f"http://{qdrant_host}:{qdrant_port}/collections/{collection}", timeout=5)
        if resp.status_code == 200:
            result = resp.json().get('result', {})
            vec_count = result.get('points_count', 0)
            qdrant_stats = {
                'vectors': vec_count,
                'indexed': result.get('indexed_vectors_count', 0),
                'status': result.get('status', 'unknown'),
                'segments': result.get('segments_count', 0),
                'index_type': 'HNSW' if vec_count >= 20000 else 'brute-force',
            }
    except Exception as e:
        qdrant_stats = {'error': str(e)}

    # Qdrant scroll — only re-run every 10 min
    now = _time.time()
    if _cache['qdrant_scroll'] is None or (now - _cache['qdrant_scroll_ts']) > 600:
        _cache['qdrant_scroll'] = _qdrant_scroll(qdrant_host, qdrant_port, collection, req)
        _cache['qdrant_scroll_ts'] = now

    cached = _cache['qdrant_scroll'] or {}
    domain_counts = cached.get('domains', {})
    knowledge_type_counts = cached.get('knowledge_types', {})
    complexity_counts = cached.get('complexities', {})
    source_type_counts = cached.get('source_types', {})
    sample_size = cached.get('sample_size', 0)

    catalogue_total = conn.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
    not_started = conn.execute("""
        SELECT COUNT(*) FROM catalogue
        WHERE hash NOT IN (SELECT hash FROM documents)
    """).fetchone()[0]

    recent = conn.execute("""
        SELECT COALESCE(d.book_title, c.filename) as title,
               d.status, d.concepts_extracted, d.vectors_inserted,
               CASE
                 WHEN c.source = 'stream.echo6.co' THEN 'transcript'
                 WHEN c.source = 'kiwix' THEN 'wiki'
                 WHEN d.path LIKE 'http%' THEN 'web'
                 ELSE 'pdf'
               END as type
        FROM documents d
        JOIN catalogue c ON d.hash = c.hash
        WHERE d.status = 'complete'
        ORDER BY d.embedded_at DESC
        LIMIT 10
    """).fetchall()

    active_titles = {}
    for active_status in ('extracting', 'enriching', 'embedding'):
        rows = conn.execute(
            "SELECT COALESCE(book_title, filename) as title FROM documents WHERE status = ? LIMIT 5",
            (active_status,)
        ).fetchall()
        if rows:
            active_titles[active_status] = [r['title'] for r in rows]

    return {
        'totals': {
            'documents': totals['total_docs'],
            'complete': totals['complete_docs'],
            'failed': totals['failed_docs'],
            'in_pipeline': totals['in_pipeline'],
            'not_started': not_started,
            'concepts': totals['total_concepts'],
            'vectors': totals['total_vectors'],
            'pages_processed': totals['total_pages'],
            'catalogued': catalogue_total,
        },
        'active_titles': active_titles,
        'pipeline': [{'status': r['status'], 'count': r['count']} for r in pipeline],
        'sources': [{
            'name': r['source'], 'type': r['type'],
            'catalogued': r['catalogued'], 'complete': r['complete'],
            'in_pipeline': r['in_pipeline'],
            'concepts': r['concepts'], 'vectors': r['vectors'], 'pages': r['pages'],
        } for r in sources],
        'qdrant': qdrant_stats,
        'domains': dict(sorted(domain_counts.items(), key=lambda x: -x[1])),
        'knowledge_types': dict(sorted(knowledge_type_counts.items(), key=lambda x: -x[1])),
        'complexities': dict(sorted(complexity_counts.items(), key=lambda x: -x[1])),
        'source_types': dict(sorted(source_type_counts.items(), key=lambda x: -x[1])),
        'sample_size': sample_size,
        'recent_complete': [{
            'title': r['title'] or 'Untitled',
            'concepts': r['concepts_extracted'] or 0,
            'vectors': r['vectors_inserted'] or 0,
            'type': r['type'],
        } for r in recent],
    }


def _build_quick_stats():
    """Build quick stats (runs in background warmer)."""
    config = get_config()
    db = StatusDB()
    conn = db._get_conn()

    catalogued = conn.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
    in_pipeline = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status NOT IN ('complete', 'failed')"
    ).fetchone()[0]

    vectors = 0
    try:
        vdb = config['vector_db']
        resp = http_requests.get(
            f"http://{vdb['host']}:{vdb['port']}/collections/{vdb['collection']}",
            timeout=3
        )
        if resp.status_code == 200:
            vectors = resp.json().get('result', {}).get('points_count', 0)
    except Exception:
        pass

    return {'catalogued': catalogued, 'in_pipeline': in_pipeline, 'vectors': vectors}


def start_cache_warmer(stop_event=None):
    """Background thread that keeps all dashboard caches warm."""
    def _run():
        import time as _time
        logger.info("Cache warmer starting — initial fetch...")

        # Initial warm-up: fetch everything before first user request
        try:
            _cache['knowledge_stats'] = _build_knowledge_stats()
            logger.info("  Knowledge stats cached")
        except Exception as e:
            logger.warning(f"  Knowledge stats warm-up failed: {e}")

        try:
            _cache['pt_dashboard'] = _fetch_pt_dashboard()
            logger.info("  PeerTube dashboard cached")
        except Exception as e:
            logger.warning(f"  PeerTube dashboard warm-up failed: {e}")

        try:
            _cache['quick_stats'] = _build_quick_stats()
            logger.info("  Quick stats cached")
        except Exception as e:
            logger.warning(f"  Quick stats warm-up failed: {e}")

        try:
            _cache['kiwix_sources'] = _build_kiwix_sources()
            logger.info("  Kiwix sources cached")
        except Exception as e:
            logger.warning(f"  Kiwix sources warm-up failed: {e}")

        logger.info("Cache warmer ready — all data pre-loaded")

        # Continuous refresh loop
        cycle = 0
        while True:
            if stop_event and stop_event.is_set():
                break
            if stop_event:
                stop_event.wait(15)
                if stop_event.is_set():
                    break
            else:
                _time.sleep(15)

            cycle += 1

            # Knowledge stats + quick stats: every 30s (cycle 2)
            if cycle % 2 == 0:
                try:
                    _cache['knowledge_stats'] = _build_knowledge_stats()
                except Exception as e:
                    logger.debug(f"Knowledge stats refresh failed: {e}")
                try:
                    _cache['quick_stats'] = _build_quick_stats()
                except Exception:
                    pass
                try:
                    _cache['kiwix_sources'] = _build_kiwix_sources()
                except Exception:
                    pass

            # PeerTube dashboard: every 30s (cycle 2, offset)
            if cycle % 2 == 1:
                try:
                    _cache['pt_dashboard'] = _fetch_pt_dashboard()
                except Exception as e:
                    logger.debug(f"PT dashboard refresh failed: {e}")

        logger.info("Cache warmer stopped")

    t = threading.Thread(target=_run, daemon=True, name='cache-warmer')
    t.start()
    return t


@app.route('/api/knowledge-stats')
def api_knowledge_stats():
    """Serve pre-cached knowledge stats (never blocks)."""
    if _cache['knowledge_stats'] is None:
        return jsonify({'error': 'Warming up, try again in a few seconds'}), 503
    return jsonify(_cache['knowledge_stats'])


@app.route('/api/health')
def api_health():
    """Health check endpoint for monitoring."""
    import time as _time
    config = get_config()
    health = {
        'status': 'healthy',
        'timestamp': _time.time(),
        'uptime': _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime()),
        'components': {},
        'pipeline': {},
    }

    try:
        vdb = config['vector_db']
        resp = http_requests.get(
            f"http://{vdb['host']}:{vdb['port']}/collections/{vdb['collection']}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()['result']
            health['components']['qdrant'] = {
                'status': 'up',
                'vectors': data['points_count'],
            }
        else:
            health['components']['qdrant'] = {'status': 'down', 'error': f'HTTP {resp.status_code}'}
            health['status'] = 'degraded'
    except Exception as e:
        health['components']['qdrant'] = {'status': 'down', 'error': str(e)[:100]}
        health['status'] = 'degraded'

    try:
        emb = config['embedding']
        resp = http_requests.get(
            f"http://{emb['tei_host']}:{emb['tei_port']}/health",
            timeout=5
        )
        health['components']['tei'] = {'status': 'up' if resp.status_code == 200 else 'down'}
    except Exception as e:
        health['components']['tei'] = {'status': 'down', 'error': str(e)[:100]}
        health['status'] = 'degraded'

    try:
        nfs_ok = os.path.exists('/mnt/library') and len(os.listdir('/mnt/library')) > 0
    except Exception:
        nfs_ok = False
    health['components']['nfs'] = {'status': 'up' if nfs_ok else 'down'}
    if not nfs_ok:
        health['status'] = 'unhealthy'

    gemini_keys = config.get('gemini_keys', [])
    health['components']['gemini'] = {
        'status': 'configured' if gemini_keys else 'missing',
        'keys': len(gemini_keys),
    }

    try:
        db = StatusDB()
        raw = db.get_status_counts()
        health['pipeline'] = raw.get('documents', {})
    except Exception as e:
        health['pipeline'] = {'error': str(e)[:100]}

    code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), code


def run_server(stop_event=None):
    config = get_config()
    host = config['web']['host']
    port = config['web']['port']
    # Start cache warmer before Flask so data is ready when users hit the dashboard
    start_cache_warmer(stop_event)
    logger.info(f"Starting RECON web dashboard on {host}:{port}")
    app.run(host=host, port=port, debug=False)


@app.route('/api/service/restart', methods=['POST'])
def api_service_restart():
    import subprocess
    logger.info("Service restart requested via dashboard")
    subprocess.Popen(
        ['sudo', 'systemd-run', '--scope', '--', 'bash', '-c', 'sleep 1 && systemctl restart recon'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return jsonify({'ok': True, 'message': 'Restart scheduled'})


# ── Key Management API ──

@app.route('/api/keys', methods=['GET'])
def api_keys_list():
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    return jsonify({'keys': km.get_masked_keys()})

@app.route('/api/keys', methods=['POST'])
def api_keys_add():
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    data = request.get_json(force=True)
    key = data.get('key', '').strip()
    if not key:
        return jsonify({'error': 'Key cannot be empty'}), 400
    try:
        idx = km.add_gemini_key(key)
        return jsonify({'index': idx, 'count': km.get_gemini_key_count()})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/keys/<int:index>', methods=['PUT'])
def api_keys_replace(index):
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    data = request.get_json(force=True)
    key = data.get('key', '').strip()
    if not key:
        return jsonify({'error': 'Key cannot be empty'}), 400
    try:
        km.replace_gemini_key(index, key)
        return jsonify({'ok': True, 'count': km.get_gemini_key_count()})
    except (ValueError, IndexError) as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/keys/<int:index>', methods=['DELETE'])
def api_keys_remove(index):
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    try:
        masked = km.remove_gemini_key(index)
        return jsonify({'removed': masked, 'count': km.get_gemini_key_count()})
    except (ValueError, IndexError) as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/keys/validate', methods=['POST'])
def api_keys_validate_all():
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    results = km.validate_all()
    return jsonify({'results': results})

@app.route('/api/keys/<int:index>/validate', methods=['POST'])
def api_keys_validate_one(index):
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    key = km.get_gemini_key(index)
    if key is None:
        return jsonify({'error': f'Key index {index} not found'}), 404
    valid, message = km.validate_key(key)
    return jsonify({'index': index, 'valid': valid, 'message': message})

@app.route('/api/keys/reload', methods=['POST'])
def api_keys_reload():
    from lib.key_manager import get_key_manager
    km = get_key_manager()
    count = km.reload_from_env()
    return jsonify({'count': count})





# ── YouTube Cookie Management ──

PEERTUBE_HOST = '192.168.1.170'
PEERTUBE_USER = 'zvx'
COOKIES_PATH = '/opt/bulk-import/config/cookies.txt'
CHANNEL_MAP_PATH = '/opt/bulk-import/config/channel-map.json'

def _ssh_peertube(cmd, timeout=30):
    """Run a command on CT 110 via SSH."""
    import subprocess
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
         f'{PEERTUBE_USER}@{PEERTUBE_HOST}', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr

@app.route('/api/cookies/status')
def api_cookies_status():
    try:
        rc, out, err = _ssh_peertube(f'stat -c "%Y" {COOKIES_PATH} 2>/dev/null')
        if rc != 0 or not out.strip():
            return jsonify({'error': 'Could not stat cookies file', 'detail': err.strip()}), 500
        mtime = int(out.strip())
        import time
        age_seconds = int(time.time()) - mtime
        age_hours = round(age_seconds / 3600, 1)
        is_stale = age_hours > (14 * 24)

        rc2, out2, _ = _ssh_peertube(
            'systemctl is-active pt-downloader 2>/dev/null; '
            'journalctl -u pt-downloader --no-pager -n 20 --since "30 min ago" 2>/dev/null '
            '| grep -c "Rate limited" 2>/dev/null'
        )
        lines = out2.strip().split('\n')
        dl_active = lines[0].strip() if lines else 'unknown'
        rate_limits = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip().isdigit() else 0

        return jsonify({
            'mtime': mtime,
            'age_hours': age_hours,
            'is_stale': is_stale,
            'downloader_active': dl_active == 'active',
            'recent_rate_limits': rate_limits,
        })
    except Exception as e:
        logger.error(f"Cookie status check failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/cookies/upload', methods=['POST'])
def api_cookies_upload():
    import subprocess, tempfile
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    content = file.read().decode('utf-8', errors='replace')

    # Log upload details for debugging
    sapisid = ""
    for line in content.split("\n"):
        if "SAPISID\t" in line and not line.startswith("#"):
            parts = line.split("\t")
            if len(parts) >= 7:
                sapisid = parts[6][:20] + "..."
                break
    logger.info("Cookie upload: filename=%s, size=%d, lines=%d, SAPISID=%s" % (file.filename, len(content), content.count(chr(10)), sapisid or "unknown"))

    if 'youtube.com' not in content.lower() and '.youtube.com' not in content.lower():
        return jsonify({'error': 'Invalid cookies file - no youtube.com entries found'}), 400

    data_lines = [l for l in content.strip().split('\n') if l.strip() and not l.startswith('#')]
    if len(data_lines) < 1:
        return jsonify({'error': 'Cookies file appears empty (no data lines)'}), 400

    try:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(content)
        tmp.close()

        rc = subprocess.run(
            ['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
             tmp.name, f'{PEERTUBE_USER}@{PEERTUBE_HOST}:/tmp/cookies-upload.txt'],
            capture_output=True, text=True, timeout=15
        ).returncode
        os.unlink(tmp.name)

        if rc != 0:
            return jsonify({'error': 'SCP to PeerTube host failed'}), 500

        rc, _, err = _ssh_peertube(
            'sudo -u peertube /usr/bin/tee /opt/bulk-import/config/cookies.txt < /tmp/cookies-upload.txt > /dev/null '
            '&& rm /tmp/cookies-upload.txt'
        )
        if rc != 0:
            return jsonify({'error': f'Failed to install cookies: {err.strip()}'}), 500

        logger.info("Testing uploaded YouTube cookies...")
        rc, out, err = _ssh_peertube(
            'sudo -u peertube /usr/local/bin/yt-dlp '
            '--cookies /opt/bulk-import/config/cookies.txt '
            '--simulate "https://www.youtube.com/watch?v=dQw4w9WgXcQ" 2>&1',
            timeout=45
        )
        test_output = (out + err).strip()
        if rc == 0:
            logger.info("YouTube cookie test passed")
            return jsonify({
                'ok': True,
                'message': 'Cookies updated and verified',
                'test_output': test_output[:500],
                'data_lines': len(data_lines),
            })
        else:
            logger.warning(f"YouTube cookie test failed: {test_output[:200]}")
            return jsonify({
                'ok': False,
                'message': 'Cookies installed but verification failed',
                'test_output': test_output[:500],
                'data_lines': len(data_lines),
            }), 422

    except Exception as e:
        logger.error(f"Cookie upload failed: {e}")
        return jsonify({'error': f'Upload failed: {e}'}), 500

# ── NordVPN Management ──

VPN_ROTATE_SCRIPT = '/opt/bulk-import/config/vpn/vpn-rotate.sh'
VPN_LOG = '/opt/bulk-import/logs/vpn.log'

@app.route('/api/vpn/status')
def api_vpn_status():
    try:
        rc, out, err = _ssh_peertube('sudo nordvpn status 2>&1', timeout=15)
        status_text = out.strip()
        connected = 'Connected' in status_text and 'Disconnected' not in status_text

        country = ''
        server = ''
        for line in status_text.split('\n'):
            if line.startswith('Country:'):
                country = line.split(':', 1)[1].strip()
            if line.startswith('Server:'):
                server = line.split(':', 1)[1].strip()

        ip = ''
        if connected:
            rc2, out2, _ = _ssh_peertube('curl -s --connect-timeout 5 https://ifconfig.me 2>/dev/null', timeout=15)
            ip = out2.strip()

        rotations_today = 0
        rc3, out3, _ = _ssh_peertube(
            'grep -c "$(date +%Y-%m-%d).*Connecting" ' + VPN_LOG + ' 2>/dev/null',
            timeout=10
        )
        if rc3 == 0 and out3.strip().isdigit():
            rotations_today = int(out3.strip())

        return jsonify({
            'connected': connected,
            'country': country,
            'server': server,
            'ip': ip,
            'rotations_today': rotations_today,
            'raw_status': status_text,
        })
    except Exception as e:
        logger.error(f"VPN status check failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vpn/rotate', methods=['POST'])
def api_vpn_rotate():
    try:
        rc, out, err = _ssh_peertube(f'sudo {VPN_ROTATE_SCRIPT} rotate 2>&1', timeout=60)
        ip = out.strip()
        rc2, out2, _ = _ssh_peertube('sudo nordvpn status 2>&1', timeout=15)
        country = ''
        for line in out2.strip().split('\n'):
            if line.startswith('Country:'):
                country = line.split(':', 1)[1].strip()
        logger.info(f"VPN rotated to {country} ({ip})")
        return jsonify({'ok': True, 'ip': ip, 'country': country})
    except Exception as e:
        logger.error(f"VPN rotate failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vpn/connect', methods=['POST'])
def api_vpn_connect():
    data = request.get_json(silent=True) or {}
    country = data.get('country', 'United_States')
    import re as _re
    if not _re.match(r'^[A-Za-z_]+$', country):
        return jsonify({'error': 'Invalid country name'}), 400
    try:
        rc, out, err = _ssh_peertube(f'sudo {VPN_ROTATE_SCRIPT} connect {country} 2>&1', timeout=60)
        ip = out.strip()
        logger.info(f"VPN connected to {country} ({ip})")
        return jsonify({'ok': True, 'ip': ip, 'country': country})
    except Exception as e:
        logger.error(f"VPN connect failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vpn/disconnect', methods=['POST'])
def api_vpn_disconnect():
    try:
        rc, out, err = _ssh_peertube(f'sudo {VPN_ROTATE_SCRIPT} disconnect 2>&1', timeout=30)
        logger.info("VPN disconnected")
        return jsonify({'ok': True, 'message': 'Disconnected'})
    except Exception as e:
        logger.error(f"VPN disconnect failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vpn/login', methods=['POST'])
def api_vpn_login():
    data = request.get_json(silent=True) or {}
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token required'}), 400
    try:
        rc, out, err = _ssh_peertube(f'sudo nordvpn login --token {token} 2>&1', timeout=30)
        result = (out + err).strip()
        if rc == 0 or 'already logged in' in result.lower():
            logger.info("NordVPN login successful")
            return jsonify({'ok': True, 'message': result})
        else:
            return jsonify({'ok': False, 'message': result}), 400
    except Exception as e:
        logger.error(f"VPN login failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── PeerTube Channel Management ──

@app.route('/api/peertube/channels/stats')
def api_peertube_channel_stats():
    try:
        rc, out, _ = _ssh_peertube(f'cat {CHANNEL_MAP_PATH}', timeout=10)
        if rc != 0:
            return jsonify({'error': 'Cannot read channel map'}), 500
        import json as _json
        channels = _json.loads(out)
        total_channels = len(channels)

        rc2, out2, _ = _ssh_peertube(
            'sudo -u peertube psql peertube_prod -t -A -c "SELECT COUNT(*) FROM video;"',
            timeout=15
        )
        total_videos = int(out2.strip()) if rc2 == 0 and out2.strip().isdigit() else 0

        rc3, out3, _ = _ssh_peertube('systemctl is-active pt-downloader 2>/dev/null', timeout=10)
        dl_active = out3.strip() == 'active'

        zero_count = sum(1 for c in channels if c.get('video_count', 0) == 0)

        return jsonify({
            'total_channels': total_channels,
            'total_videos': total_videos,
            'channels_with_zero_videos': zero_count,
            'downloader_active': dl_active
        })
    except Exception as e:
        logger.error(f"PeerTube stats failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/peertube/channels')
def api_peertube_channels():
    try:
        rc, out, err = _ssh_peertube(f'cat {CHANNEL_MAP_PATH}', timeout=10)
        if rc != 0:
            return jsonify({'error': f'Cannot read channel map: {err.strip()}'}), 500
        import json as _json
        channels = _json.loads(out)

        rc2, out2, _ = _ssh_peertube(
            'sudo -u peertube psql peertube_prod -t -A -c '
            '"SELECT vc.name, COUNT(v.id) FROM \\"videoChannel\\" vc '
            'LEFT JOIN video v ON v.\\"channelId\\" = vc.id GROUP BY vc.name;"',
            timeout=15
        )
        video_counts = {}
        if rc2 == 0:
            for line in out2.strip().split('\n'):
                if '|' in line:
                    parts = line.split('|')
                    video_counts[parts[0]] = int(parts[1]) if parts[1].isdigit() else 0

        for ch in channels:
            ch['videos_in_peertube'] = video_counts.get(ch.get('actor_name', ''), 0)

        return jsonify(channels)
    except Exception as e:
        logger.error(f"PeerTube channels list failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/peertube/channels/add', methods=['POST'])
def api_peertube_add_channel():
    data = request.get_json(silent=True) or {}
    yt_url = data.get('youtube_url', '').strip()
    category = data.get('category', '').strip()
    priority = data.get('priority', 'M').strip().upper()
    if priority not in ('H', 'M', 'L'):
        priority = 'M'

    if not yt_url:
        return jsonify({'error': 'youtube_url is required'}), 400

    import re, json as _json

    try:
        yt_dlp_base = (
            f'sudo -u peertube /usr/local/bin/yt-dlp '
            f'--cookies {COOKIES_PATH} '
            f'--print channel --print channel_url --print channel_id '
            f'--skip-download'
        )
        rc, out, err = _ssh_peertube(
            f'{yt_dlp_base} --playlist-items 1 "{yt_url}"',
            timeout=60
        )
        if rc != 0 or len(out.strip().split('\n')) < 3:
            videos_url = yt_url.rstrip('/') + '/videos'
            rc, out, err = _ssh_peertube(
                f'{yt_dlp_base} --ignore-errors --playlist-items 1:5 "{videos_url}" 2>/dev/null',
                timeout=60
            )
            if rc != 0 and not out.strip():
                return jsonify({'error': f'yt-dlp failed: {err.strip() or "Could not resolve channel"}'}), 400

        lines = [l.strip() for l in out.strip().split('\n') if l.strip()]
        if len(lines) < 3:
            return jsonify({'error': f'yt-dlp returned incomplete data: {out.strip()}'}), 400

        channel_name = lines[0]
        channel_url = lines[1]
        channel_id = lines[2]

        if not channel_name or channel_name == 'NA':
            return jsonify({'error': 'Could not resolve channel name'}), 400

        actor_name = re.sub(r'[^a-z0-9]+', '-', channel_name.lower()).strip('-')[:50]
        if not actor_name:
            return jsonify({'error': 'Could not generate actor name'}), 400

        rc_r, out_r, _ = _ssh_peertube(f'cat {CHANNEL_MAP_PATH}', timeout=10)
        existing = _json.loads(out_r) if rc_r == 0 else []

        for ch in existing:
            if ch.get('youtube_channel_id') == channel_id:
                return jsonify({'error': f'Channel already exists: {ch.get("channel_name")}'}), 409
            if ch.get('actor_name') == actor_name:
                return jsonify({'error': f'Actor name conflict: {actor_name}'}), 409

        rc_c, out_c, _ = _ssh_peertube(
            'curl -s http://localhost:9000/api/v1/oauth-clients/local -H "Host: stream.echo6.co"',
            timeout=15
        )
        if rc_c != 0:
            return jsonify({'error': 'Failed to get OAuth client info'}), 500
        client_info = _json.loads(out_c)
        client_id = client_info.get('client_id')
        client_secret = client_info.get('client_secret')

        rc_t, out_t, _ = _ssh_peertube(
            f'curl -s http://localhost:9000/api/v1/users/token -H "Host: stream.echo6.co" '
            f'--data "client_id={client_id}&client_secret={client_secret}'
            f'&grant_type=password&username=root&password=7redditGold"',
            timeout=15
        )
        if rc_t != 0:
            return jsonify({'error': 'Failed to get OAuth token'}), 500
        token_data = _json.loads(out_t)
        access_token = token_data.get('access_token')
        if not access_token:
            return jsonify({'error': f'No access token returned: {out_t.strip()}'}), 500

        display_name = f'(YT){channel_name}'
        payload = _json.dumps({'name': actor_name, 'displayName': display_name})
        rc_ch, out_ch, _ = _ssh_peertube(
            f"curl -s -X POST http://localhost:9000/api/v1/video-channels "
            f"-H 'Host: stream.echo6.co' -H 'Authorization: Bearer {access_token}' "
            f"-H 'Content-Type: application/json' "
            f"-d '{payload}'",
            timeout=15
        )
        if rc_ch != 0:
            return jsonify({'error': f'Failed to create PeerTube channel: {out_ch.strip()}'}), 500

        ch_result = _json.loads(out_ch)
        if 'error' in ch_result:
            return jsonify({'error': f'PeerTube error: {ch_result["error"]}'}), 400
        pt_channel_id = ch_result.get('videoChannel', {}).get('id', 0)

        new_entry = {
            'category': category or 'Uncategorized',
            'channel_name': f'(YT){channel_name}',
            'actor_name': actor_name,
            'youtube_url': channel_url,
            'youtube_channel_id': channel_id,
            'peertube_channel_id': pt_channel_id,
            'video_count': 0,
            'priority': priority,
            'est_videos': 0,
            'est_gb': 0
        }
        existing.append(new_entry)
        json_str = _json.dumps(existing, indent=2)
        rc_w, _, err_w = _ssh_peertube(
            f'echo {_quote(json_str)} > /tmp/channel-map-new.json && '
            f'sudo -u peertube tee {CHANNEL_MAP_PATH} < /tmp/channel-map-new.json > /dev/null && '
            f'rm -f /tmp/channel-map-new.json',
            timeout=15
        )
        if rc_w != 0:
            return jsonify({'error': f'Failed to write channel map: {err_w.strip()}'}), 500

        logger.info(f"Added PeerTube channel: {actor_name} ({channel_name})")
        return jsonify({
            'ok': True,
            'channel_name': channel_name,
            'actor_name': actor_name,
            'peertube_channel_id': pt_channel_id
        })
    except _json.JSONDecodeError as e:
        logger.error(f"JSON parse error adding channel: {e}")
        return jsonify({'error': f'JSON parse error: {e}'}), 500
    except Exception as e:
        logger.error(f"Add channel failed: {e}")
        return jsonify({'error': str(e)}), 500


def _quote(s):
    """Shell-safe quoting for passing strings via SSH."""
    import shlex
    return shlex.quote(s)


@app.route('/api/peertube/channels/<actor_name>', methods=['DELETE'])
def api_peertube_delete_channel(actor_name):
    import re, json as _json
    if not re.match(r'^[a-z0-9-]+$', actor_name):
        return jsonify({'error': 'Invalid actor name'}), 400

    try:
        rc, out, err = _ssh_peertube(f'cat {CHANNEL_MAP_PATH}', timeout=10)
        if rc != 0:
            return jsonify({'error': f'Cannot read channel map: {err.strip()}'}), 500
        channels = _json.loads(out)

        found = None
        remaining = []
        for ch in channels:
            if ch.get('actor_name') == actor_name:
                found = ch
            else:
                remaining.append(ch)

        if not found:
            return jsonify({'error': f'Channel not found: {actor_name}'}), 404

        json_str = _json.dumps(remaining, indent=2)
        rc_w, _, err_w = _ssh_peertube(
            f'echo {_quote(json_str)} > /tmp/channel-map-new.json && '
            f'sudo -u peertube tee {CHANNEL_MAP_PATH} < /tmp/channel-map-new.json > /dev/null && '
            f'rm -f /tmp/channel-map-new.json',
            timeout=15
        )
        if rc_w != 0:
            return jsonify({'error': f'Failed to write channel map: {err_w.strip()}'}), 500

        pt_id = found.get('peertube_channel_id', 0)
        if pt_id:
            try:
                rc_c, out_c, _ = _ssh_peertube(
                    'curl -s http://localhost:9000/api/v1/oauth-clients/local -H "Host: stream.echo6.co"',
                    timeout=15
                )
                client_info = _json.loads(out_c)
                rc_t, out_t, _ = _ssh_peertube(
                    f'curl -s http://localhost:9000/api/v1/users/token -H "Host: stream.echo6.co" '
                    f'--data "client_id={client_info["client_id"]}&client_secret={client_info["client_secret"]}'
                    f'&grant_type=password&username=root&password=7redditGold"',
                    timeout=15
                )
                token = _json.loads(out_t).get('access_token', '')
                if token:
                    _ssh_peertube(
                        f"curl -s -X DELETE http://localhost:9000/api/v1/video-channels/{actor_name} "
                        f"-H 'Host: stream.echo6.co' -H 'Authorization: Bearer {token}'",
                        timeout=15
                    )
            except Exception as del_err:
                logger.warning(f"Could not delete PeerTube channel {actor_name}: {del_err}")

        logger.info(f"Removed PeerTube channel: {actor_name}")
        return jsonify({'ok': True, 'message': f'Removed {actor_name}'})
    except Exception as e:
        logger.error(f"Delete channel failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── PeerTube Dashboard API ──

CORTEX_HOST = '192.168.1.150'
CORTEX_USER = 'zvx'

def _ssh_cortex(cmd, timeout=15):
    """Run a command on cortex via SSH."""
    import subprocess
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
         f'{CORTEX_USER}@{CORTEX_HOST}', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def _fetch_pt_dashboard():
    """Fetch PeerTube dashboard data from CT 110 + cortex (slow: SSH + NFS)."""
    import json as _json

    result = {
        'video_states': {},
        'pipeline_dirs': {},
        'services': {},
        'gpu': {},
        'downloader_state': {},
        'recent_errors': [],
        'imports_last_hour': 0,
    }

    # CT 110: video states + pipeline dirs + services + downloader state
    try:
        rc, out, _ = _ssh_peertube(
            'sudo -u peertube psql peertube_prod -t -A -c "SELECT state, COUNT(*) FROM video GROUP BY state;" 2>/dev/null; '
            'echo "---DELIM---"; '
            'for d in staging completed transcoded failed; do '
            '  dir="/opt/bulk-import/$d"; '
            '  if [ -d "$dir" ]; then '
            '    find -L "$dir" -type f -printf "%s %f\n" 2>/dev/null | '
            "    awk '{bytes+=$1; files++; if($2~/\\.(mp4|webm|mkv)$/)vids++} "
            "    END{printf \"%s|%d|%d|%.0f\\n\",d,vids+0,files+0,bytes+0}' d=\"$d\"; "
            '  else echo "$d|0|0|0"; fi; '
            'done; '
            'echo "---DELIM---"; '
            'systemctl is-active pt-downloader 2>/dev/null; '
            'systemctl is-active pt-importer 2>/dev/null; '
            'echo "---DELIM---"; '
            'cat /opt/bulk-import/config/downloader-state.json 2>/dev/null || echo "{}"',
            timeout=60
        )
        if rc == 0 or out.strip():
            sections = out.split('---DELIM---')

            if len(sections) > 0:
                for line in sections[0].strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|')
                        if len(parts) == 2 and parts[1].isdigit():
                            result['video_states'][parts[0]] = int(parts[1])

            if len(sections) > 1:
                for line in sections[1].strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|')
                        if len(parts) == 4:
                            result['pipeline_dirs'][parts[0]] = {
                                'videos': int(parts[1]) if parts[1].isdigit() else 0,
                                'files': int(parts[2]) if parts[2].isdigit() else 0,
                                'bytes': int(parts[3]) if parts[3].isdigit() else 0,
                            }

            if len(sections) > 2:
                svc_lines = sections[2].strip().split('\n')
                result['services']['downloader'] = svc_lines[0].strip() if len(svc_lines) > 0 else 'unknown'
                result['services']['importer'] = svc_lines[1].strip() if len(svc_lines) > 1 else 'unknown'

            if len(sections) > 3:
                try:
                    result['downloader_state'] = _json.loads(sections[3].strip())
                except Exception:
                    result['downloader_state'] = {}

    except Exception as e:
        logger.warning(f"PT dashboard CT 110 query failed: {e}")

    # CT 110: recent errors
    try:
        rc, out, _ = _ssh_peertube(
            'journalctl -u pt-downloader -u pt-importer --no-pager --since "1 hour ago" 2>/dev/null '
            '| grep -iE "error|fail" | tail -10',
            timeout=15
        )
        if rc == 0 and out.strip():
            result['recent_errors'] = [line.strip() for line in out.strip().split('\n') if line.strip()]
    except Exception:
        pass

    # Cortex: GPU stats
    try:
        rc, out, _ = _ssh_cortex(
            'nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu '
            '--format=csv,noheader,nounits 2>/dev/null',
            timeout=10
        )
        if rc == 0 and out.strip():
            parts = [p.strip() for p in out.strip().split(',')]
            if len(parts) >= 5:
                result['gpu'] = {
                    'name': parts[0],
                    'memory_used': parts[1],
                    'memory_total': parts[2],
                    'utilization_gpu': parts[3],
                    'temperature_gpu': parts[4],
                }
    except Exception as e:
        logger.debug(f"Cortex GPU query failed: {e}")

    # Cortex: services
    try:
        rc, out, _ = _ssh_cortex(
            'systemctl is-active pt-transcoder 2>/dev/null; '
            'systemctl is-active peertube-runner 2>/dev/null',
            timeout=10
        )
        if rc == 0 or out.strip():
            lines = out.strip().split('\n')
            result['services']['transcoder'] = lines[0].strip() if len(lines) > 0 else 'unknown'
            result['services']['runner'] = lines[1].strip() if len(lines) > 1 else 'unknown'
    except Exception as e:
        logger.debug(f"Cortex service query failed: {e}")
        result['services']['transcoder'] = 'unavailable'
        result['services']['runner'] = 'unavailable'

    return result


@app.route('/api/peertube/dashboard')
def api_peertube_dashboard():
    """Serve pre-cached PeerTube dashboard (never blocks)."""
    if _cache['pt_dashboard'] is None:
        return jsonify({'error': 'Warming up, try again in a few seconds'}), 503
    return jsonify(_cache['pt_dashboard'])



# ── Kiwix Dashboard ──

@app.route('/kiwix')
def kiwix_dashboard():
    return render_template('kiwix/dashboard.html',
                           domain='kiwix', subnav=KIWIX_SUBNAV, active_page='/kiwix')


@app.route('/kiwix/scraper')
def kiwix_scraper():
    return render_template('kiwix/scraper.html',
                           domain='kiwix', subnav=KIWIX_SUBNAV, active_page='/kiwix/scraper')


@app.route('/api/kiwix/sources')
def api_kiwix_sources():
    """Serve pre-cached Kiwix sources data (never blocks)."""
    if _cache['kiwix_sources'] is None:
        return jsonify({'error': 'Warming up, try again in a few seconds'}), 503
    return jsonify(_cache['kiwix_sources'])


@app.route('/api/kiwix/toggle-ingest/<int:source_id>', methods=['POST'])
def api_kiwix_toggle_ingest(source_id):
    """Toggle ingest_enabled on a ZIM source."""
    db = StatusDB()
    conn = db._get_conn()
    row = conn.execute("SELECT id, status, ingest_enabled FROM zim_sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Source not found'}), 404

    data = request.get_json(silent=True) or {}
    new_val = 1 if data.get('enabled', not row['ingest_enabled']) else 0
    conn.execute("UPDATE zim_sources SET ingest_enabled = ? WHERE id = ?", (new_val, source_id))
    conn.commit()

    # If toggling ON and source is eligible, spawn ingest in background
    if new_val == 1 and row['status'] == 'detected':
        _spawn_zim_ingest(source_id)

    return jsonify({'ok': True, 'ingest_enabled': new_val})


@app.route('/api/kiwix/trigger-ingest/<int:source_id>', methods=['POST'])
def api_kiwix_trigger_ingest(source_id):
    """Explicit one-shot ingest trigger."""
    db = StatusDB()
    conn = db._get_conn()
    row = conn.execute("SELECT id FROM zim_sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Source not found'}), 404

    _spawn_zim_ingest(source_id)
    return jsonify({'ok': True})


@app.route('/api/kiwix/upload', methods=['POST'])
def api_kiwix_upload():
    """Accept ZIM file upload, register with kiwix-serve, scan."""
    import subprocess
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename or not f.filename.endswith('.zim'):
        return jsonify({'error': 'File must be a .zim file'}), 400

    filename = secure_filename(f.filename)
    dest = os.path.join('/mnt/kiwix', filename)

    try:
        # Stream was written directly to /mnt/kiwix/ by _LargeZimRequest —
        # rename in-place instead of copying 100GB+ through f.save()
        if hasattr(f.stream, 'name') and f.stream.name:
            tmp_path = f.stream.name
            f.stream.close()
            os.rename(tmp_path, dest)
        else:
            tmp_dest = dest + '.tmp'
            f.save(tmp_dest)
            os.rename(tmp_dest, dest)
    except Exception as e:
        # Clean up any temp files on failure
        for p in [locals().get('tmp_path', ''), locals().get('tmp_dest', '')]:
            if p and os.path.exists(p):
                os.remove(p)
        return jsonify({'error': f'Save failed: {e}'}), 500

    # Register with kiwix-serve library
    try:
        subprocess.run(
            ['/opt/recon/bin/kiwix-manage', '/mnt/kiwix/library.xml', 'add', dest],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        logger.warning(f"kiwix-manage add failed: {e}")

    # Scan for new entry (retry — monitorLibrary may need a moment to reload)
    import time as _time
    from .zim_monitor import scan_zims
    for attempt in range(3):
        try:
            scan_zims()
            break
        except Exception as e:
            logger.warning(f"scan_zims attempt {attempt+1} failed: {e}")
            _time.sleep(2)

    # Refresh cache
    try:
        _cache['kiwix_sources'] = _build_kiwix_sources()
    except Exception:
        pass

    return jsonify({'ok': True, 'filename': filename})



def _full_zim_cleanup(source_id):
    """Full ZIM cleanup: Qdrant vectors, DB records, kiwix-manage, SIGHUP, file delete.
    Returns dict with results. Caller handles cache refresh."""
    import subprocess
    import signal
    import requests as req

    db = StatusDB()
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM zim_sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return None

    zim_source = dict(row)
    zim_filename = zim_source['zim_filename']
    zim_path = zim_source['zim_path']
    zim_title = zim_source.get('title', zim_filename)
    results = {'vectors_deleted': 0, 'docs_deleted': 0, 'file_deleted': False, 'scrape_jobs_deleted': 0}

    # Step 1: Find all document hashes for this ZIM source
    doc_hashes = [r['hash'] for r in conn.execute(
        "SELECT c.hash FROM catalogue c WHERE c.source = 'kiwix' AND c.category = ?",
        (zim_title,)
    ).fetchall()]

    # Step 2: Delete vectors from Qdrant
    if doc_hashes:
        config = get_config()
        qdrant_host = config.get('vector_db', {}).get('host', '100.64.0.14')
        qdrant_port = config.get('vector_db', {}).get('port', 6333)
        collection = config.get('vector_db', {}).get('collection', 'recon_knowledge')

        # Delete in batches of 100 hashes
        for i in range(0, len(doc_hashes), 100):
            batch = doc_hashes[i:i+100]
            try:
                resp = req.post(
                    f"http://{qdrant_host}:{qdrant_port}/collections/{collection}/points/delete",
                    json={
                        "filter": {
                            "must": [{
                                "key": "doc_hash",
                                "match": {"any": batch}
                            }]
                        }
                    },
                    timeout=30
                )
                if resp.status_code == 200:
                    results['vectors_deleted'] += len(batch)
            except Exception as e:
                logger.warning(f"Qdrant delete batch failed: {e}")

    # Step 3: Delete DB records
    for h in doc_hashes:
        # Delete processing directory if it exists
        text_dir_row = conn.execute("SELECT text_dir FROM documents WHERE hash = ?", (h,)).fetchone()
        if text_dir_row and text_dir_row['text_dir']:
            try:
                import shutil
                shutil.rmtree(text_dir_row['text_dir'], ignore_errors=True)
            except Exception:
                pass
        conn.execute("DELETE FROM documents WHERE hash = ?", (h,))
        conn.execute("DELETE FROM catalogue WHERE hash = ?", (h,))
    results['docs_deleted'] = len(doc_hashes)

    # Delete zim_articles records
    conn.execute("DELETE FROM zim_articles WHERE zim_source_id = ?", (source_id,))

    # Delete zim_sources record
    conn.execute("DELETE FROM zim_sources WHERE id = ?", (source_id,))
    conn.commit()

    # Step 4: Remove from kiwix-serve library
    try:
        subprocess.run(
            ['/opt/recon/bin/kiwix-manage', '/mnt/kiwix/library.xml', 'remove', zim_filename.replace('.zim', '')],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        logger.warning(f"kiwix-manage remove failed: {e}")

    # Step 4b: SIGHUP kiwix-serve to reload library
    try:
        result = subprocess.run(['pidof', 'kiwix-serve'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split()[0])
            os.kill(pid, signal.SIGHUP)
            logger.info(f"Sent SIGHUP to kiwix-serve (pid {pid})")
    except Exception as e:
        logger.warning(f"Failed to signal kiwix-serve: {e}")

    # Step 5: Delete the ZIM file
    if os.path.isfile(zim_path):
        try:
            os.remove(zim_path)
            results['file_deleted'] = True
        except Exception as e:
            logger.warning(f"ZIM file delete failed: {e}")
            results['file_deleted'] = False

    # Step 6: Delete any linked scrape_jobs rows
    try:
        res = conn.execute("DELETE FROM scrape_jobs WHERE zim_source_id = ?", (source_id,))
        conn.commit()
        results['scrape_jobs_deleted'] = res.rowcount
    except Exception as e:
        logger.warning(f"scrape_jobs cleanup failed: {e}")

    logger.info(f"Full ZIM cleanup for source {source_id} ('{zim_title}'): {results}")
    return results


@app.route('/api/kiwix/remove/<int:source_id>', methods=['POST'])
def api_kiwix_remove(source_id):
    """Remove a ZIM source: delete vectors, DB records, library entry, and file."""
    db = StatusDB()
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM zim_sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Source not found'}), 404

    results = _full_zim_cleanup(source_id)
    if results is None:
        return jsonify({'error': 'Source not found during cleanup'}), 404

    # Refresh cache
    try:
        _cache['kiwix_sources'] = _build_kiwix_sources()
    except Exception:
        pass

    return jsonify({'ok': True, 'results': results})


def _spawn_zim_ingest(source_id):
    """Start ZIM ingestion in a background thread."""
    def _run():
        try:
            from .processors.zim_processor import ingest_zim
            config = get_config()
            db = StatusDB()
            logger.info(f"Starting ZIM ingest for source {source_id}")
            result = ingest_zim(source_id, db, config)
            logger.info(f"ZIM ingest complete for source {source_id}: {result}")
            # Refresh cache after completion
            try:
                _cache['kiwix_sources'] = _build_kiwix_sources()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"ZIM ingest failed for source {source_id}: {e}")

    t = threading.Thread(target=_run, daemon=True, name=f'zim-ingest-{source_id}')
    t.start()


def _build_kiwix_sources():
    """Build Kiwix sources data for the dashboard cache."""
    import urllib.request

    db = StatusDB()
    conn = db._get_conn()

    # Get all ZIM sources
    rows = conn.execute("""
        SELECT id, zim_filename, title, description, language, category,
               article_count, status, processed_count, skipped_count, error_count,
               ingest_enabled, detected_at, started_at, completed_at
        FROM zim_sources
        ORDER BY detected_at DESC
    """).fetchall()

    sources = []
    total_articles = 0
    total_processed = 0
    total_in_pipeline = 0

    for r in rows:
        source = dict(r)
        zim_title = r['title'] or r['zim_filename']
        total_articles += r['article_count'] or 0
        total_processed += r['processed_count'] or 0

        # Get pipeline stats for THIS source's documents (filtered by category)
        pipeline = {}
        try:
            pipe_rows = conn.execute("""
                SELECT d.status, COUNT(*) as cnt
                FROM documents d
                JOIN catalogue c ON d.hash = c.hash
                WHERE c.source = 'kiwix' AND c.category = ?
                GROUP BY d.status
            """, (zim_title,)).fetchall()
            for pr in pipe_rows:
                pipeline[pr['status']] = pr['cnt']
        except Exception:
            pass

        in_pipe = sum(v for k, v in pipeline.items() if k not in ('complete', 'failed'))
        total_in_pipeline += in_pipe
        source['pipeline'] = pipeline

        # Compute effective status reflecting full pipeline state
        db_status = r['status']
        if db_status == 'complete' and pipeline:
            if in_pipe > 0:
                source['effective_status'] = 'processing'
            else:
                source['effective_status'] = 'complete'
        elif db_status == 'ingesting':
            source['effective_status'] = 'extracting'
        else:
            source['effective_status'] = db_status  # 'detected'

        sources.append(source)

    # Check kiwix-serve health
    kiwix_status = 'inactive'
    try:
        resp = urllib.request.urlopen("http://localhost:8430", timeout=3)
        if resp.status == 200:
            kiwix_status = 'active'
    except Exception:
        pass

    return {
        'sources': sources,
        'kiwix_serve': {'status': kiwix_status, 'url': 'https://wiki.echo6.co'},
        'totals': {
            'sources': len(sources),
            'articles': total_articles,
            'processed': total_processed,
            'in_pipeline': total_in_pipeline,
        }
    }




# ── Scraper API ──

@app.route('/api/scraper/submit', methods=['POST'])
def api_scraper_submit():
    """Submit a new web scrape job."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'url is required'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'URL must start with http:// or https://'}), 400

    config = get_config()
    scraper_cfg = config.get('scraper', {})
    language = data.get('language') or scraper_cfg.get('default_language', 'eng')
    title = data.get('title', '').strip() or None
    category = data.get('category', '').strip() or None

    db = StatusDB()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scrape_jobs (url, title, language, category, crawl_mode) VALUES (?, ?, ?, ?, ?)",
        (url, title, language, category, 'zimit')
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    logger.info(f"Scraper job {job_id} submitted: {url}")
    return jsonify({'ok': True, 'job_id': job_id}), 201


@app.route('/api/scraper/jobs')
def api_scraper_jobs():
    """List scrape jobs, optionally filtered by status."""
    status_filter = request.args.get('status')
    db = StatusDB()
    jobs = db.get_scrape_jobs(status=status_filter)
    return jsonify({'jobs': jobs})


@app.route('/api/scraper/cancel/<int:job_id>', methods=['POST'])
def api_scraper_cancel(job_id):
    """Cancel a scrape job."""

    db = StatusDB()
    job = db.get_scrape_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job['status'] in ('complete', 'cancelled'):
        return jsonify({'error': f"Job already {job['status']}"}), 400

    # Set cancelled in DB — the runner loop checks this between phases
    db.update_scrape_job(job_id, status='cancelled')

    # Stop the Docker container if running
    container_name = f'recon-scraper-{job_id}'
    try:
        import subprocess as _subprocess
        _subprocess.run(['docker', 'rm', '-f', container_name],
                        capture_output=True, timeout=10)
    except Exception:
        pass

    logger.info(f"Scraper job {job_id} cancelled")
    return jsonify({'ok': True})


@app.route('/api/scraper/retry/<int:job_id>', methods=['POST'])
def api_scraper_retry(job_id):
    """Retry a failed or cancelled scrape job."""
    db = StatusDB()
    job = db.get_scrape_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job['status'] not in ('failed', 'cancelled'):
        return jsonify({'error': f"Job status is '{job['status']}', can only retry failed or cancelled jobs"}), 400

    db.update_scrape_job(job_id,
                         status='pending',
                         error_message=None,
                         subprocess_pid=None,
                         crawl_mode=None,
                         started_at=None,
                         completed_at=None)

    logger.info(f"Scraper job {job_id} reset to pending for retry")
    return jsonify({'ok': True})


@app.route('/api/scraper/delete/<int:job_id>', methods=['POST'])
def api_scraper_delete(job_id):
    """Delete a scrape job and clean up any associated ZIM artifacts."""
    import subprocess
    import signal

    db = StatusDB()
    job = db.get_scrape_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job['status'] == 'running':
        return jsonify({'error': 'Cannot delete a running job \u2014 cancel it first'}), 400

    zim_cleanup_results = None

    # If the job has a linked zim_source, do full cleanup
    if job.get('zim_source_id'):
        zim_cleanup_results = _full_zim_cleanup(job['zim_source_id'])
        try:
            _cache['kiwix_sources'] = _build_kiwix_sources()
        except Exception:
            pass
    elif job.get('zim_filename'):
        # No zim_source row, but there may be an orphan file + library entry
        zim_path = os.path.join('/mnt/kiwix', job['zim_filename'])
        if os.path.isfile(zim_path):
            try:
                os.remove(zim_path)
                logger.info(f"Deleted orphan ZIM file: {zim_path}")
            except Exception as e:
                logger.warning(f"Failed to delete orphan ZIM file {zim_path}: {e}")
            try:
                subprocess.run(
                    ['/opt/recon/bin/kiwix-manage', '/mnt/kiwix/library.xml', 'remove',
                     job['zim_filename'].replace('.zim', '')],
                    capture_output=True, text=True, timeout=10
                )
            except Exception as e:
                logger.warning(f"kiwix-manage remove failed for orphan: {e}")
            try:
                result = subprocess.run(['pidof', 'kiwix-serve'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    pid = int(result.stdout.strip().split()[0])
                    os.kill(pid, signal.SIGHUP)
                    logger.info(f"Sent SIGHUP to kiwix-serve (pid {pid})")
            except Exception as e:
                logger.warning(f"Failed to signal kiwix-serve: {e}")

    # Delete the scrape_jobs row (may already be gone if _full_zim_cleanup deleted it)
    conn = db._get_conn()
    conn.execute("DELETE FROM scrape_jobs WHERE id = ?", (job_id,))
    conn.commit()

    logger.info(f"Scraper job {job_id} deleted (zim_cleanup={zim_cleanup_results})")
    return jsonify({'ok': True, 'zim_cleanup': zim_cleanup_results})


@app.route('/api/scraper/clear-failed', methods=['POST'])
def api_scraper_clear_failed():
    """Delete all failed and cancelled scrape jobs."""
    db = StatusDB()
    conn = db._get_conn()
    result = conn.execute("DELETE FROM scrape_jobs WHERE status IN ('failed', 'cancelled')")
    conn.commit()
    count = result.rowcount
    logger.info(f"Cleared {count} failed/cancelled scraper jobs")
    return jsonify({'ok': True, 'deleted': count})


# ── Metrics API ──

@app.route('/api/metrics/history')
def api_metrics_history():
    """Return time-series metric snapshots."""
    metric_type = request.args.get('type', 'knowledge')
    hours = min(int(request.args.get('hours', 24)), 168)

    db = StatusDB()
    conn = db._get_conn()

    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT timestamp, data FROM metrics_snapshots WHERE metric_type = ? AND timestamp > ? ORDER BY timestamp",
            (metric_type, cutoff)
        ).fetchall()

        points = []
        for r in rows:
            try:
                points.append({
                    'timestamp': r['timestamp'],
                    'data': json.loads(r['data']),
                })
            except Exception:
                pass

        return jsonify({'type': metric_type, 'hours': hours, 'points': points})
    except Exception as e:
        return jsonify({'type': metric_type, 'hours': hours, 'points': [], 'error': str(e)})


# ── Auth state endpoint ─────────────────────────────────────────────────────
# Returns current auth state for frontend consumption.
# This endpoint must be behind Caddy forward_auth to receive X-Authentik-* headers.
@app.route('/api/auth/whoami')
def api_auth_whoami():
    """Return auth state for frontend. Behind forward_auth, so headers are present when authenticated."""
    username = request.headers.get('X-Authentik-Username')
    if username:
        return jsonify({
            'authenticated': True,
            'username': username,
        })
    return jsonify({
        'authenticated': False,
        'username': None,
    })
