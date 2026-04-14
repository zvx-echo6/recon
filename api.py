import json
import os

import requests as http_requests
from flask import Flask, request, jsonify, redirect
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from .utils import get_config, content_hash, setup_logging
from .status import StatusDB

logger = setup_logging('recon.api')

app = Flask(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>RECON</title>
<meta charset="utf-8">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Courier New', monospace; background: #0a0a0a; color: #c0c0c0; }
.header { background: #111; border-bottom: 1px solid #333; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { color: #00ff41; font-size: 18px; letter-spacing: 2px; }
.header .stats { font-size: 12px; color: #666; }
.nav { background: #0d0d0d; border-bottom: 1px solid #222; padding: 8px 24px; }
.nav a { color: #888; text-decoration: none; margin-right: 16px; font-size: 13px; }
.nav a:hover, .nav a.active { color: #00ff41; }
.content { padding: 24px; max-width: 1400px; margin: 0 auto; }
.search-box { width: 100%; padding: 10px 16px; background: #111; border: 1px solid #333; color: #c0c0c0; font-family: inherit; font-size: 14px; margin-bottom: 16px; }
.search-box:focus { outline: none; border-color: #00ff41; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #111; color: #00ff41; text-align: left; padding: 8px 12px; border-bottom: 1px solid #333; }
td { padding: 6px 12px; border-bottom: 1px solid #1a1a1a; }
tr:hover { background: #111; }
.status { padding: 2px 8px; border-radius: 3px; font-size: 11px; }
.status-complete { color: #00ff41; }
.status-enriched { color: #00bfff; }
.status-extracted { color: #ffa500; }
.status-failed { color: #ff4444; }
.status-queued { color: #888; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat-card { background: #111; border: 1px solid #222; padding: 16px; }
.stat-card .label { color: #666; font-size: 11px; text-transform: uppercase; }
.stat-card .value { color: #00ff41; font-size: 28px; margin-top: 4px; }
.result { background: #111; border: 1px solid #222; padding: 16px; margin-bottom: 12px; }
.result .title { color: #00ff41; font-size: 14px; margin-bottom: 4px; }
.result .meta { color: #666; font-size: 11px; margin-bottom: 8px; }
.result .content-text { color: #999; font-size: 12px; line-height: 1.5; }
.result .score { color: #ffa500; font-size: 12px; float: right; }
.btn { background: #1a1a1a; border: 1px solid #333; color: #c0c0c0; padding: 6px 14px; cursor: pointer; font-family: inherit; font-size: 12px; }
.btn:hover { border-color: #00ff41; color: #00ff41; }
.domain-tag { display: inline-block; background: #1a1a1a; border: 1px solid #333; padding: 1px 6px; margin: 1px; font-size: 10px; color: #888; }
</style>
</head>
<body>
<div class="header">
    <h1>RECON</h1>
    <div class="stats">Knowledge Base Management System</div>
</div>
<div class="nav">
    <a href="/" id="nav-dash">Dashboard</a>
    <a href="/search" id="nav-search">Search</a>
    <a href="/catalogue" id="nav-cat">Catalogue</a>
    <a href="/failures" id="nav-fail">Failures</a>
</div>
<div class="content" id="main">
    {{CONTENT}}
</div>
</body>
</html>"""


def render(content):
    return HTML_TEMPLATE.replace('{{CONTENT}}', content)


@app.route('/')
def dashboard():
    db = StatusDB()
    counts = db.get_status_counts()
    cat = counts.get('catalogue', {})
    doc = counts.get('documents', {})

    total_cat = sum(cat.values())
    total_doc = sum(doc.values())
    complete = doc.get('complete', 0)
    failed = doc.get('failed', 0)

    stats = f"""
    <div class="stat-grid">
        <div class="stat-card"><div class="label">Catalogued PDFs</div><div class="value">{total_cat}</div></div>
        <div class="stat-card"><div class="label">In Pipeline</div><div class="value">{total_doc}</div></div>
        <div class="stat-card"><div class="label">Complete</div><div class="value">{complete}</div></div>
        <div class="stat-card"><div class="label">Failed</div><div class="value">{failed}</div></div>
    </div>
    <h3 style="color:#00ff41;margin-bottom:12px;">Pipeline Status</h3>
    <table>
    <tr><th>Status</th><th>Count</th></tr>
    """
    for status in ['queued', 'extracting', 'extracted', 'enriching', 'enriched', 'embedding', 'complete', 'failed']:
        count = doc.get(status, 0)
        stats += f'<tr><td><span class="status status-{status}">{status}</span></td><td>{count}</td></tr>\n'

    stats += "</table>"

    sources = db.source_breakdown()
    if sources:
        stats += '<h3 style="color:#00ff41;margin:24px 0 12px;">Sources</h3><table><tr><th>Source</th><th>Count</th><th>Size</th></tr>'
        for s in sources:
            size_mb = (s.get('total_bytes', 0) or 0) / (1024 * 1024)
            stats += f"<tr><td>{s['source']}</td><td>{s['count']}</td><td>{size_mb:.1f} MB</td></tr>"
        stats += "</table>"

    return render(stats)


@app.route('/search')
def search_page():
    query = request.args.get('q', '')
    if not query:
        content = """
        <h3 style="color:#00ff41;margin-bottom:16px;">Semantic Search</h3>
        <form method="get" action="/search">
            <input type="text" name="q" class="search-box" placeholder="Search the knowledge base..." autofocus>
        </form>
        <p style="color:#666;font-size:12px;margin-top:8px;">Enter a query to search across all embedded concepts.</p>
        """
        return render(content)

    config = get_config()
    limit = int(request.args.get('limit', 20))
    source_filter = request.args.get('source_type', None)

    try:
        url = f"http://{config['embedding']['host']}:{config['embedding']['port']}/api/embed"
        resp = http_requests.post(url, json={
            "model": config['embedding']['model'],
            "input": query
        }, timeout=120)
        resp.raise_for_status()
        query_vector = resp.json()['embeddings'][0]

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

        content = f"""
        <h3 style="color:#00ff41;margin-bottom:16px;">Results for: {query}</h3>
        <form method="get" action="/search">
            <input type="text" name="q" class="search-box" value="{query}">
        </form>
        <p style="color:#666;font-size:12px;margin-bottom:16px;">{len(results)} results</p>
        """

        for r in results:
            p = r.payload
            title = p.get('title', 'Untitled')
            summary = p.get('summary', p.get('content', '')[:200])
            score = r.score
            domains = p.get('domain', [])
            book = p.get('book_title', p.get('filename', ''))
            source_type = p.get('source_type', 'document')

            domain_tags = ''.join(f'<span class="domain-tag">{d}</span>' for d in (domains if isinstance(domains, list) else []))

            content += f"""
            <div class="result">
                <span class="score">{score:.4f}</span>
                <div class="title">{title}</div>
                <div class="meta">{book} | {source_type} | {p.get('skill_level', 'unknown')}</div>
                <div class="content-text">{summary}</div>
                <div style="margin-top:6px;">{domain_tags}</div>
            </div>
            """

        return render(content)

    except Exception as e:
        return render(f'<p style="color:#ff4444;">Search error: {e}</p>')


@app.route('/catalogue')
def catalogue_page():
    db = StatusDB()
    source = request.args.get('source', None)
    category = request.args.get('category', None)
    limit = int(request.args.get('limit', 100))

    docs = db.get_all_documents(source=source, category=category, limit=limit)

    content = '<h3 style="color:#00ff41;margin-bottom:16px;">Document Catalogue</h3>'

    sources = db.get_sources()
    if sources:
        content += '<div style="margin-bottom:12px;">'
        content += '<a href="/catalogue" class="btn" style="margin-right:4px;">All</a>'
        for s in sources:
            content += f'<a href="/catalogue?source={s}" class="btn" style="margin-right:4px;">{s}</a>'
        content += '</div>'

    content += """<table>
    <tr><th>Filename</th><th>Source</th><th>Status</th><th>Pages</th><th>Concepts</th><th>Vectors</th></tr>"""

    for d in docs:
        status = d.get('status', 'unknown')
        content += f"""<tr>
            <td>{d.get('filename', '?')}</td>
            <td>{d.get('source', '')}</td>
            <td><span class="status status-{status}">{status}</span></td>
            <td>{d.get('pages_extracted', 0)}</td>
            <td>{d.get('concepts_extracted', 0)}</td>
            <td>{d.get('vectors_inserted', 0)}</td>
        </tr>"""

    content += "</table>"
    return render(content)


@app.route('/failures')
def failures_page():
    db = StatusDB()
    failures = db.get_failures()

    content = '<h3 style="color:#ff4444;margin-bottom:16px;">Failed Documents</h3>'

    if not failures:
        content += '<p style="color:#666;">No failures.</p>'
        return render(content)

    content += '<table><tr><th>Filename</th><th>Error</th><th>Retries</th><th>Actions</th></tr>'
    for f in failures:
        content += f"""<tr>
            <td>{f.get('filename', '?')}</td>
            <td style="color:#ff4444;font-size:11px;">{f.get('error_message', 'unknown')[:100]}</td>
            <td>{f.get('retry_count', 0)}</td>
            <td><form method="post" action="/api/retry/{f['hash']}" style="display:inline;">
                <button class="btn" type="submit">Retry</button>
            </form></td>
        </tr>"""

    content += "</table>"
    return render(content)


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
        url = f"http://{config['embedding']['host']}:{config['embedding']['port']}/api/embed"
        resp = http_requests.post(url, json={
            "model": config['embedding']['model'],
            "input": query
        }, timeout=120)
        resp.raise_for_status()
        query_vector = resp.json()['embeddings'][0]

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

        return jsonify({
            'query': query,
            'results': [
                {
                    'score': r.score,
                    'payload': r.payload
                }
                for r in results
            ]
        })

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


def run_server():
    config = get_config()
    host = config['web']['host']
    port = config['web']['port']
    logger.info(f"Starting RECON web dashboard on {host}:{port}")
    app.run(host=host, port=port, debug=False)
