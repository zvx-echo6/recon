/* RECON Kiwix Dashboard JS */
(function() {
    'use strict';

    function loadKiwixDashboard() {
        return RECON.fetchJSON('/api/kiwix/sources').then(function(data) {
            // Update stat cards
            var t = data.totals || {};
            RECON.set('kx-sources', RECON.fmt(t.sources));
            RECON.set('kx-articles', RECON.fmt(t.articles));
            RECON.set('kx-processed', RECON.fmt(t.processed));
            RECON.set('kx-pipeline', RECON.fmt(t.in_pipeline));

            // Kiwix-serve status dot
            var ks = data.kiwix_serve || {};
            var dot = document.getElementById('svc-kiwix-serve');
            dot.className = 'svc-dot ' + (ks.status === 'active' ? 'active' : 'inactive');

            // ZIM table
            var sources = data.sources || [];
            var html = '';
            sources.forEach(function(s) {
                var es = s.effective_status || s.status;
                var pipe = s.pipeline || {};
                var pipeComplete = pipe.complete || 0;
                var pipeTotal = 0;
                for (var k in pipe) pipeTotal += pipe[k];
                var pctDone = pipeTotal > 0 ? (pipeComplete / pipeTotal * 100).toFixed(1) : 0;
                var statusBadge = es === 'complete' ? '<span class="badge-complete">COMPLETE</span>' :
                    es === 'processing' ? '<span class="badge-processing">PROCESSING</span>' :
                    es === 'extracting' ? '<span class="badge-extracting">EXTRACTING</span>' :
                    '<span class="badge-detected">DETECTED</span>';
                // Derive browse URL from zim_filename
                var zimName = s.zim_filename.replace(/_(?:maxi|mini|nopic)_[\d-]+\.zim$/, '');
                var browseUrl = 'https://wiki.echo6.co/' + zimName + '/';
                // Toggle switch
                var checked = s.ingest_enabled ? ' checked' : '';
                var toggle = '<label class="toggle-switch"><input type="checkbox"' + checked +
                    ' onchange="KIWIX.toggleIngest(' + s.id + ', this.checked)">' +
                    '<span class="toggle-slider"></span></label>';

                html += '<tr>' +
                    '<td><strong>' + (s.title || s.zim_filename) + '</strong>' +
                    '<div class="text-small text-muted">' + s.zim_filename + '</div></td>' +
                    '<td>' + (s.language || '\u2014') + '</td>' +
                    '<td>' + RECON.fmt(s.article_count) + '</td>' +
                    '<td>' + (es === 'complete' && pipeComplete > 0 ?
                        RECON.fmt(pipeComplete) + ' in Qdrant' :
                        es === 'processing' ?
                        RECON.fmt(pipeComplete) + ' / ' + RECON.fmt(pipeTotal) + ' in Qdrant (' + pctDone + '%)' :
                        es === 'extracting' ?
                        RECON.fmt(s.processed_count) + ' / ' + RECON.fmt(s.article_count) + ' extracted' :
                        '\u2014') + '</td>' +
                    '<td>' + statusBadge + '</td>' +
                    '<td>' + toggle + '</td>' +
                    '<td><a href="' + browseUrl + '" target="_blank">Browse</a></td>' +
                    '<td><button class="btn btn-danger" onclick="KIWIX.remove(' + s.id + ', \'' + (s.title || s.zim_filename).replace(/'/g, "\\'") + '\')">Remove</button></td>' +
                    '</tr>';
            });
            if (!html) html = '<tr><td colspan="8" class="text-muted">No ZIM sources detected</td></tr>';
            RECON.setHTML('kx-table-body', html);
        }).catch(function(err) {
            console.error('Kiwix dashboard error:', err);
        });
    }

    function toggleIngest(id, enabled) {
        RECON.postJSON('/api/kiwix/toggle-ingest/' + id, {enabled: enabled}).then(function(data) {
            if (data.ok) loadKiwixDashboard();
        });
    }

    function removeSource(id, title) {
        if (!confirm('Remove "' + title + '"?\n\nThis will delete the ZIM file, all ingested documents, and associated vectors from Qdrant. This cannot be undone.')) return;
        RECON.postJSON('/api/kiwix/remove/' + id).then(function(data) {
            if (data.ok) {
                var r = data.results || {};
                alert('Removed: ' + r.docs_deleted + ' docs, ~' + r.vectors_deleted + ' vector batches deleted, file ' + (r.file_deleted ? 'deleted' : 'not found'));
                loadKiwixDashboard();
            }
        });
    }

    function triggerIngest(id) {
        RECON.postJSON('/api/kiwix/trigger-ingest/' + id).then(function(data) {
            if (data.ok) loadKiwixDashboard();
        });
    }

    function uploadZim() {
        var input = document.getElementById('kx-file-input');
        var file = input.files[0];
        if (!file) return;

        var statusEl = document.getElementById('kx-upload-status');
        var progressDiv = document.getElementById('kx-upload-progress');
        var progressBar = document.getElementById('kx-progress-bar');
        var progressText = document.getElementById('kx-progress-text');

        statusEl.textContent = 'Uploading ' + file.name + '...';
        progressDiv.style.display = 'block';

        var formData = new FormData();
        formData.append('file', file);

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/kiwix/upload', true);

        xhr.upload.onprogress = function(e) {
            if (e.lengthComputable) {
                var pct = (e.loaded / e.total * 100).toFixed(1);
                progressBar.style.width = pct + '%';
                progressText.textContent = RECON.fmtBytes(e.loaded) + ' / ' + RECON.fmtBytes(e.total) + ' (' + pct + '%)';
            }
        };

        xhr.onload = function() {
            if (xhr.status === 200) {
                var resp = JSON.parse(xhr.responseText);
                statusEl.textContent = resp.ok ? 'Upload complete: ' + resp.filename : 'Error: ' + (resp.error || 'Unknown');
                progressBar.style.width = '100%';
                progressBar.style.background = resp.ok ? '#16a34a' : '#dc2626';
                if (resp.ok) loadKiwixDashboard();
            } else {
                statusEl.textContent = 'Upload failed (HTTP ' + xhr.status + ')';
                progressBar.style.background = '#dc2626';
            }
            input.value = '';
        };

        xhr.onerror = function() {
            statusEl.textContent = 'Upload failed (network error)';
            progressBar.style.background = '#dc2626';
            input.value = '';
        };

        xhr.send(formData);
    }

    // Expose for inline onclick
    window.KIWIX = { toggleIngest: toggleIngest, triggerIngest: triggerIngest, remove: removeSource };

    document.addEventListener('DOMContentLoaded', function() {
        RECON.startRefresh(loadKiwixDashboard, 30000);
        document.getElementById('kx-file-input').addEventListener('change', uploadZim);
    });
})();
