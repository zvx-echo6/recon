/* RECON Web Ingest page JS */
(function() {
    'use strict';

    window.showSection = function(name) {
        document.getElementById('section-single').style.display = name === 'single' ? '' : 'none';
        document.getElementById('section-crawl').style.display = name === 'crawl' ? '' : 'none';
        document.getElementById('tab-single').className = 'btn' + (name === 'single' ? ' active' : '');
        document.getElementById('tab-crawl').className = 'btn' + (name === 'crawl' ? ' active' : '');
    };

    window.doWebIngest = async function() {
        var btn = document.getElementById('wi-btn');
        var status = document.getElementById('wi-status');
        var resultsDiv = document.getElementById('wi-results');
        var urlText = document.getElementById('wi-urls').value.trim();
        var category = document.getElementById('wi-category').value.trim() || 'Web';

        if (!urlText) {
            status.style.color = '#ff4444';
            status.textContent = 'Enter at least one URL';
            return;
        }

        var urls = urlText.split('\n').map(function(u) { return u.trim(); }).filter(function(u) { return u && !u.startsWith('#'); });
        if (urls.length === 0) {
            status.style.color = '#ff4444';
            status.textContent = 'No valid URLs';
            return;
        }

        btn.disabled = true;
        status.style.color = '#ffa500';
        resultsDiv.style.display = 'none';

        if (urls.length === 1) {
            status.textContent = 'Fetching and extracting...';
            try {
                var resp = await fetch('/api/ingest-url', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ url: urls[0], category: category, process: true })
                });
                var data = await resp.json();
                if (resp.ok || resp.status === 409) {
                    var color = data.status === 'duplicate' ? '#888' : '#00ff41';
                    status.style.color = color;
                    status.textContent = data.status.toUpperCase() + ': ' + (data.title || urls[0]);
                    resultsDiv.style.display = 'block';
                    resultsDiv.innerHTML = '<span style="color:' + color + ';">' + data.status.toUpperCase() + '</span><br>' +
                        '<span class="text-dim">Hash: ' + data.hash + '</span><br>' +
                        (data.page_count ? '<span class="text-dim">Pages: ' + data.page_count + '</span><br>' : '') +
                        (data.title ? '<span class="text-dim">Title: ' + data.title + '</span><br>' : '') +
                        (data.pipeline ? '<span style="color:#00ff41;">Pipeline: enriched ' + (data.pipeline.enriched || 0) + ', embedded ' + (data.pipeline.embedded || 0) + '</span>' : '');
                } else {
                    status.style.color = '#ff4444';
                    status.textContent = data.error || 'Ingestion failed';
                }
            } catch (err) {
                status.style.color = '#ff4444';
                status.textContent = 'Network error: ' + err.message;
            }
        } else {
            status.textContent = 'Processing ' + urls.length + ' URLs...';
            try {
                var resp = await fetch('/api/ingest-urls', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ urls: urls, category: category, process: true })
                });
                var data = await resp.json();
                if (resp.ok) {
                    var s = data.summary;
                    status.style.color = '#00ff41';
                    var batchPipe = data.pipeline && data.pipeline.enriched ? ' | enriched: ' + data.pipeline.enriched + ', embedded: ' + data.pipeline.embedded : '';
                    status.textContent = s.succeeded + ' new, ' + s.duplicates + ' dupes, ' + s.failed + ' failed' + batchPipe;
                    resultsDiv.style.display = 'block';
                    var html = '';
                    for (var i = 0; i < data.results.length; i++) {
                        var r = data.results[i];
                        var c = r.status === 'failed' ? '#ff4444' : r.status === 'duplicate' ? '#888' : '#00ff41';
                        html += '<div style="margin-bottom:4px;"><span style="color:' + c + ';">' +
                            r.status.toUpperCase() + '</span> ' + (r.title || r.url) + '</div>';
                    }
                    resultsDiv.innerHTML = html;
                } else {
                    status.style.color = '#ff4444';
                    status.textContent = data.error || 'Batch ingestion failed';
                }
            } catch (err) {
                status.style.color = '#ff4444';
                status.textContent = 'Network error: ' + err.message;
            }
        }
        btn.disabled = false;
    };

    window.doCrawl = async function(dryRun) {
        var status = document.getElementById('crawl-status');
        var resultsDiv = document.getElementById('crawl-results');
        var url = document.getElementById('crawl-url').value.trim();
        var category = document.getElementById('crawl-category').value.trim() || 'Web';
        var maxPages = parseInt(document.getElementById('crawl-max-pages').value) || 500;
        var includeRaw = document.getElementById('crawl-include').value.trim();
        var excludeRaw = document.getElementById('crawl-exclude').value.trim();

        if (!url) {
            status.style.color = '#ff4444';
            status.textContent = 'Enter a site URL';
            return;
        }

        var include = includeRaw ? includeRaw.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : null;
        var exclude = excludeRaw ? excludeRaw.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : null;

        var btnP = document.getElementById('crawl-preview-btn');
        var btnC = document.getElementById('crawl-btn');
        btnP.disabled = true;
        btnC.disabled = true;
        status.style.color = '#ffa500';
        status.textContent = dryRun ? 'Discovering URLs...' : 'Starting crawl...';
        resultsDiv.style.display = 'none';

        try {
            var body = { url: url, category: category, max_pages: maxPages, dry_run: dryRun };
            if (include) body.include = include;
            if (exclude) body.exclude = exclude;

            var resp = await fetch('/api/crawl', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            var data = await resp.json();

            if (dryRun) {
                var urls = data.urls || [];
                status.style.color = '#00ff41';
                status.textContent = urls.length + ' URLs found (' + (data.discovery_method || 'unknown') + ')';
                resultsDiv.style.display = 'block';
                var html = '<div style="color:#00ff41;margin-bottom:8px;">Discovery: ' + (data.discovery_method || 'unknown') + ' — ' + urls.length + ' URLs</div>';
                urls.forEach(function(u, i) {
                    html += '<div class="text-muted">' + (i+1) + '. ' + u + '</div>';
                });
                resultsDiv.innerHTML = html;
            } else if (data.crawl_id) {
                status.style.color = '#00ff41';
                status.textContent = 'Crawl started — ID: ' + data.crawl_id;
                resultsDiv.style.display = 'block';
                resultsDiv.innerHTML = '<div style="color:#ffa500;">Crawl running in background...</div>' +
                    '<div class="text-dim" style="margin-top:4px;">ID: ' + data.crawl_id + '</div>';
                pollCrawl(data.crawl_id, resultsDiv);
            } else {
                status.style.color = '#ff4444';
                status.textContent = data.error || 'Crawl failed';
            }
        } catch (err) {
            status.style.color = '#ff4444';
            status.textContent = 'Network error: ' + err.message;
        }
        btnP.disabled = false;
        btnC.disabled = false;
    };

    function pollCrawl(crawlId, resultsDiv) {
        var check = async function() {
            try {
                var resp = await fetch('/api/crawl/' + crawlId + '/status');
                var data = await resp.json();
                if (data.status === 'running') {
                    var stageText = data.stage ? ' (' + data.stage + ')' : '';
                    resultsDiv.innerHTML = '<div style="color:#ffa500;">Pipeline running' + stageText + '...</div>' +
                        '<div class="text-dim">Site: ' + (data.site || '') + '</div>';
                    setTimeout(check, 5000);
                } else if (data.summary) {
                    var s = data.summary;
                    var pipeInfo = data.pipeline ? ' | Enriched: ' + (data.pipeline.enriched || 0) + ' | Embedded: ' + (data.pipeline.embedded || 0) : '';
                    resultsDiv.innerHTML = '<div style="color:#00ff41;">Pipeline complete!</div>' +
                        '<div class="text-dim" style="margin-top:4px;">New: ' + s.succeeded + ' | Duplicates: ' + s.duplicates + ' | Failed: ' + s.failed + ' | Total: ' + s.total + pipeInfo + '</div>';
                    document.getElementById('crawl-status').style.color = '#00ff41';
                    document.getElementById('crawl-status').textContent = 'Complete: ' + s.succeeded + ' new' + pipeInfo;
                } else if (data.error) {
                    resultsDiv.innerHTML = '<div style="color:#ff4444;">Crawl failed: ' + data.error + '</div>';
                }
            } catch (err) {
                resultsDiv.innerHTML += '<div style="color:#ff4444;">Poll error: ' + err.message + '</div>';
            }
        };
        setTimeout(check, 5000);
    }

    showSection('single');
})();
