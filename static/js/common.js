/* RECON Common Utilities
 * Shared fetch helpers, formatters, auto-refresh
 */

var RECON = (function() {
    'use strict';

    // Pipeline color/label maps
    var pipeColors = {
        queued: '#555', extracting: '#b45309', extracted: '#d97706',
        enriching: '#0284c7', enriched: '#0ea5e9', embedding: '#7c3aed',
        complete: '#16a34a', failed: '#dc2626'
    };
    var pipeLabels = {
        queued: 'Queued', extracting: 'Extracting', extracted: 'Extracted',
        enriching: 'Enriching', enriched: 'Enriched', embedding: 'Embedding',
        complete: 'Complete', failed: 'Failed'
    };

    var _refreshTimers = [];
    var _heartbeatEl = null;

    function fetchJSON(url) {
        return fetch(url).then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
    }

    function postJSON(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body || {})
        }).then(function(r) { return r.json(); });
    }

    function set(id, text) {
        var el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function setHTML(id, html) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = html;
    }

    function fmt(n) {
        if (typeof n !== 'number' || isNaN(n)) return '—';
        return n.toLocaleString();
    }

    function fmtBytes(bytes) {
        if (!bytes || bytes === 0) return '0 B';
        var units = ['B', 'KB', 'MB', 'GB', 'TB'];
        var i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
    }

    function pct(n, total) {
        if (!total || total === 0) return '0';
        return (n / total * 100).toFixed(1);
    }

    // Trend indicator: compare current to previous
    function trend(current, previous) {
        if (previous === undefined || previous === null) return '';
        var diff = current - previous;
        if (diff > 0) return '<span class="trend trend-up">+' + fmt(diff) + ' &#9650;</span>';
        if (diff < 0) return '<span class="trend trend-down">' + fmt(diff) + ' &#9660;</span>';
        return '<span class="trend trend-flat">&mdash; &#9654;</span>';
    }

    // Build a segmented pipeline progress bar
    function progressBar(segments, total) {
        var html = '';
        segments.forEach(function(seg) {
            var w = total > 0 ? (seg.count / total * 100) : 0;
            if (w > 0) {
                html += '<div class="segment" style="width:' + w + '%;background:' +
                    (seg.color || pipeColors[seg.status] || '#555') + ';" title="' +
                    (seg.label || pipeLabels[seg.status] || seg.status) + ': ' + fmt(seg.count) + '"></div>';
            }
        });
        return html;
    }

    // Build legend for pipeline bar
    function progressLegend(segments) {
        var html = '';
        segments.forEach(function(seg) {
            if (seg.count > 0) {
                html += '<span><span class="legend-dot" style="background:' +
                    (seg.color || pipeColors[seg.status] || '#555') + ';"></span>' +
                    (seg.label || pipeLabels[seg.status] || seg.status) + ': ' + fmt(seg.count) + '</span>';
            }
        });
        return html;
    }

    // Auto-refresh with heartbeat
    function startRefresh(callback, intervalMs) {
        _heartbeatEl = document.getElementById('heartbeat');

        function tick() {
            try {
                var result = callback();
                if (result && typeof result.then === 'function') {
                    result.then(function() {
                        if (_heartbeatEl) {
                            _heartbeatEl.classList.remove('dead');
                        }
                    }).catch(function() {
                        if (_heartbeatEl) {
                            _heartbeatEl.classList.add('dead');
                        }
                    });
                } else {
                    if (_heartbeatEl) _heartbeatEl.classList.remove('dead');
                }
            } catch(e) {
                if (_heartbeatEl) _heartbeatEl.classList.add('dead');
            }
        }

        // Initial load
        tick();
        var timer = setInterval(tick, intervalMs || 30000);
        _refreshTimers.push(timer);
        return timer;
    }

    function stopRefresh(timer) {
        if (timer) clearInterval(timer);
    }

    // Quick-stats loader for header
    function loadQuickStats() {
        fetchJSON('/api/quick-stats').then(function(data) {
            setHTML('qs-docs', fmt(data.catalogued));
            setHTML('qs-vectors', fmt(data.vectors));
            setHTML('qs-pipeline', fmt(data.in_pipeline));
        }).catch(function() {});
    }

    return {
        fetchJSON: fetchJSON,
        postJSON: postJSON,
        set: set,
        setHTML: setHTML,
        fmt: fmt,
        fmtBytes: fmtBytes,
        pct: pct,
        trend: trend,
        progressBar: progressBar,
        progressLegend: progressLegend,
        startRefresh: startRefresh,
        stopRefresh: stopRefresh,
        loadQuickStats: loadQuickStats,
        pipeColors: pipeColors,
        pipeLabels: pipeLabels
    };
})();
