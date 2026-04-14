/* RECON Knowledge Dashboard */
(function() {
    'use strict';

    var pipeColors = RECON.pipeColors;
    var pipeLabels = RECON.pipeLabels;

    function loadDashboard() {
        return RECON.fetchJSON('/api/knowledge-stats').then(function(data) {
            var t = data.totals;

            // Top cards
            RECON.set('kv-catalogued', RECON.fmt(t.catalogued || 0));
            RECON.set('kv-pipeline', RECON.fmt(t.in_pipeline || 0));
            var pipeSub = document.getElementById('kv-pipeline-sub');
            if (t.in_pipeline > 0) {
                var active = data.pipeline.filter(function(p) { return ['extracting','enriching','embedding'].indexOf(p.status) >= 0; });
                var activeText = active.map(function(p) { return p.count + ' ' + p.status; }).join(', ');
                pipeSub.textContent = activeText || 'processing';
            } else { pipeSub.textContent = 'idle'; }
            RECON.set('kv-complete', RECON.fmt(t.complete || 0));
            var failEl = document.getElementById('kv-failed');
            failEl.textContent = RECON.fmt(t.failed || 0);
            failEl.style.color = t.failed > 0 ? '#ff4444' : '#00ff41';
            RECON.set('kv-concepts', RECON.fmt(t.concepts || 0));
            RECON.set('kv-vectors', RECON.fmt(t.vectors || 0));
            RECON.set('kv-pages', RECON.fmt(t.pages_processed || 0));

            // Progress bar
            var total = t.catalogued || 1;
            var notYetQueued = total - (t.documents || 0);
            var segments = [];
            if (notYetQueued > 0) {
                segments.push({status: 'unqueued', count: notYetQueued, color: '#1a1a1a', label: 'Not queued'});
            }
            data.pipeline.forEach(function(p) {
                if (p.count > 0) segments.push(p);
            });
            RECON.setHTML('progress-bar', RECON.progressBar(segments, total));
            var completePct = total > 0 ? (t.complete / total * 100).toFixed(1) : 0;
            RECON.set('progress-pct', completePct + '% complete (' + RECON.fmt(t.complete || 0) + ' / ' + RECON.fmt(total) + ')');

            // Legend
            var legendSegments = [];
            if (notYetQueued > 0) legendSegments.push({status: 'unqueued', count: notYetQueued, color: '#1a1a1a', label: 'Not queued'});
            data.pipeline.forEach(function(p) { if (p.count > 0) legendSegments.push(p); });
            RECON.setHTML('progress-legend', RECON.progressLegend(legendSegments));

            // Pipeline activity
            var activeStatuses = data.pipeline.filter(function(p) { return ['extracting','enriching','embedding'].indexOf(p.status) >= 0 && p.count > 0; });
            var actDiv = document.getElementById('pipeline-activity');
            if (activeStatuses.length > 0) {
                actDiv.style.display = 'block';
                var actHtml = '';
                activeStatuses.forEach(function(p) {
                    actHtml += '<div style="margin:4px 0;"><span style="color:' + (pipeColors[p.status]||'#ffa500') + ';">&#9679; ' + (pipeLabels[p.status]||p.status) + ':</span> ' + p.count + ' documents</div>';
                });
                if (data.active_titles) {
                    Object.keys(data.active_titles).forEach(function(st) {
                        var titles = data.active_titles[st];
                        if (titles.length > 0) actHtml += '<div style="color:#666;font-size:11px;margin-left:16px;">' + titles.slice(0,3).join(', ') + (titles.length > 3 ? ', ...' : '') + '</div>';
                    });
                }
                RECON.setHTML('activity-content', actHtml);
            } else { actDiv.style.display = 'none'; }

            // Qdrant health
            var q = data.qdrant;
            var qEl = document.getElementById('qdrant-status');
            if (q.error) {
                qEl.innerHTML = '<span style="color:#ff4444;">&#9679; Offline</span> &mdash; ' + q.error;
            } else {
                var idxType = q.index_type || (q.vectors >= 20000 ? 'HNSW' : 'brute-force');
                var idxColor = idxType === 'HNSW' ? '#00ff41' : '#ffa500';
                qEl.innerHTML = '<span style="color:#00ff41;">&#9679; Online</span> | ' +
                    RECON.fmt(q.vectors) + ' vectors | ' +
                    '<span style="color:' + idxColor + ';">' + idxType + '</span>' +
                    (idxType === 'HNSW' ? ' (' + RECON.fmt(q.indexed||0) + ' indexed)' : ' (HNSW auto-builds at 20K)') +
                    ' | <span style="color:#555;">recon_knowledge</span>';
            }

            // Sources table
            var tbody = document.getElementById('sources-tbody');
            var totalCat = 0, totalComp = 0, totalPipe = 0, totalConcepts = 0, totalVectors = 0;
            tbody.innerHTML = data.sources.map(function(s) {
                var catCount = s.catalogued || 0;
                var compCount = s.complete || 0;
                var pipeCount = s.in_pipeline || 0;
                totalCat += catCount; totalComp += compCount; totalPipe += pipeCount;
                totalConcepts += s.concepts; totalVectors += s.vectors;
                var badge = s.type === 'transcript' ? '<span class="badge-transcript">TRANSCRIPT</span>' : s.type === 'web' ? '<span class="badge-web">WEB</span>' : '<span class="badge-pdf">PDF</span>';
                var compPct = catCount > 0 ? (compCount / catCount * 100) : 0;
                var pipePct = catCount > 0 ? (pipeCount / catCount * 100) : 0;
                var compColor = compPct >= 100 ? '#00ff41' : compPct > 0 ? '#ffa500' : '#666';
                var pipeColor = pipeCount > 0 ? '#0ea5e9' : '#555';
                var barW = 80;
                var compW = (compPct / 100 * barW).toFixed(1);
                var pipeW = (pipePct / 100 * barW).toFixed(1);
                var miniBar = '<div style="display:flex;align-items:center;gap:6px;">' +
                    '<div style="width:' + barW + 'px;height:10px;background:#1a1a1a;border-radius:3px;overflow:hidden;display:flex;">' +
                    '<div style="width:' + compW + 'px;background:#16a34a;height:100%;"></div>' +
                    '<div style="width:' + pipeW + 'px;background:#0284c7;height:100%;"></div>' +
                    '</div><span style="color:#888;font-size:10px;">' + compPct.toFixed(0) + '%</span></div>';
                return '<tr><td>' + s.name + '</td><td>' + badge + '</td><td>' +
                    RECON.fmt(catCount) + '</td><td><span style="color:' + compColor + ';">' +
                    RECON.fmt(compCount) + '</span></td><td><span style="color:' + pipeColor + ';">' +
                    RECON.fmt(pipeCount) + '</span></td><td>' + miniBar + '</td><td>' +
                    RECON.fmt(s.concepts) + '</td><td>' + RECON.fmt(s.vectors) + '</td></tr>';
            }).join('');
            RECON.setHTML('sources-tfoot',
                '<tr style="border-top:1px solid #333;font-weight:bold;"><td>TOTAL</td><td></td><td>' +
                RECON.fmt(totalCat) + '</td><td>' + RECON.fmt(totalComp) + '</td><td>' +
                RECON.fmt(totalPipe) + '</td><td></td><td>' +
                RECON.fmt(totalConcepts) + '</td><td>' + RECON.fmt(totalVectors) + '</td></tr>');

            // Domain bars
            var dc = document.getElementById('domain-bars');
            var domEntries = Object.entries(data.domains);
            if (domEntries.length === 0) {
                dc.innerHTML = '<span class="text-dim">No domain data</span>';
            } else {
                var maxD = Math.max.apply(null, domEntries.map(function(e) { return e[1]; }));
                dc.innerHTML = domEntries.map(function(entry) {
                    var name = entry[0], count = entry[1];
                    var pct = (count / maxD * 100).toFixed(1);
                    return '<div style="display:flex;align-items:center;gap:10px;margin:5px 0;">' +
                        '<span style="width:160px;text-align:right;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + name + '</span>' +
                        '<div style="flex:1;height:18px;background:#1a1a1a;border-radius:3px;overflow:hidden;">' +
                        '<div style="height:100%;background:#00cc66;border-radius:3px;width:' + pct + '%;"></div></div>' +
                        '<span style="width:50px;color:#ccc;text-align:right;">' + RECON.fmt(count) + '</span></div>';
                }).join('');
            }

            // Knowledge Type bars
            var ktEl = document.getElementById('knowledge-type-bars');
            var ktEntries = Object.entries(data.knowledge_types || {});
            var totalKt = ktEntries.reduce(function(a, e) { return a + e[1]; }, 0);
            if (ktEntries.length === 0) {
                ktEl.innerHTML = '<span class="text-dim">No data yet (migration in progress)</span>';
            } else {
                var ktColors = {foundational: '#60a5fa', procedural: '#4ade80', operational: '#fbbf24'};
                var maxKt = Math.max.apply(null, ktEntries.map(function(e) { return e[1]; }));
                ktEl.innerHTML = ktEntries.map(function(entry) {
                    var name = entry[0], count = entry[1];
                    var pctVal = totalKt > 0 ? (count / totalKt * 100).toFixed(0) : 0;
                    var barPct = (count / maxKt * 100).toFixed(1);
                    var color = ktColors[name] || '#888';
                    return '<div style="display:flex;align-items:center;gap:10px;margin:5px 0;">' +
                        '<span style="width:100px;text-align:right;color:' + color + ';">' + name + '</span>' +
                        '<div style="flex:1;height:18px;background:#1a1a1a;border-radius:3px;overflow:hidden;">' +
                        '<div style="height:100%;background:' + color + ';opacity:0.6;border-radius:3px;width:' + barPct + '%;"></div></div>' +
                        '<span style="width:80px;color:#ccc;text-align:right;">' + RECON.fmt(count) + ' (' + pctVal + '%)</span></div>';
                }).join('');
            }
            var ktMig = document.getElementById('knowledge-type-migration');
            ktMig.textContent = RECON.fmt(totalKt) + ' / ' + RECON.fmt(data.sample_size) + ' migrated';

            // Complexity bars
            var cxEl = document.getElementById('complexity-bars');
            var cxEntries = Object.entries(data.complexities || {});
            var totalCx = cxEntries.reduce(function(a, e) { return a + e[1]; }, 0);
            if (cxEntries.length === 0) {
                cxEl.innerHTML = '<span class="text-dim">No data yet (migration in progress)</span>';
            } else {
                var cxColors = {basic: '#4ade80', intermediate: '#fbbf24', advanced: '#f87171'};
                var maxCx = Math.max.apply(null, cxEntries.map(function(e) { return e[1]; }));
                cxEl.innerHTML = cxEntries.map(function(entry) {
                    var name = entry[0], count = entry[1];
                    var pctVal = totalCx > 0 ? (count / totalCx * 100).toFixed(0) : 0;
                    var barPct = (count / maxCx * 100).toFixed(1);
                    var color = cxColors[name] || '#888';
                    return '<div style="display:flex;align-items:center;gap:10px;margin:5px 0;">' +
                        '<span style="width:100px;text-align:right;color:' + color + ';">' + name + '</span>' +
                        '<div style="flex:1;height:18px;background:#1a1a1a;border-radius:3px;overflow:hidden;">' +
                        '<div style="height:100%;background:' + color + ';opacity:0.6;border-radius:3px;width:' + barPct + '%;"></div></div>' +
                        '<span style="width:80px;color:#ccc;text-align:right;">' + RECON.fmt(count) + ' (' + pctVal + '%)</span></div>';
                }).join('');
            }
            var cxMig = document.getElementById('complexity-migration');
            cxMig.textContent = RECON.fmt(totalCx) + ' / ' + RECON.fmt(data.sample_size) + ' migrated';

            // Recent completions
            var rtb = document.getElementById('recent-tbody');
            if (data.recent_complete.length === 0) {
                rtb.innerHTML = '<tr><td colspan="4" class="text-dim">None yet</td></tr>';
            } else {
                rtb.innerHTML = data.recent_complete.map(function(r) {
                    var badge = r.type === 'transcript' ? '<span class="badge-transcript">TRANSCRIPT</span>' : r.type === 'web' ? '<span class="badge-web">WEB</span>' : '<span class="badge-pdf">PDF</span>';
                    return '<tr><td>' + r.title + '</td><td>' + badge + '</td><td>' +
                        r.concepts + '</td><td>' + r.vectors + '</td></tr>';
                }).join('');
            }
        });
    }

    function loadCharts() {
        if (typeof ReconChart !== 'undefined') {
            ReconChart.loadAndDraw('kb-chart', 'knowledge',
                ['complete', 'concepts'], ['Completed', 'Concepts'], 24);
        }
    }

    function initSourcesToggle() {
        var toggle = document.getElementById('sources-toggle');
        var arrow = document.getElementById('sources-arrow');
        var thead = document.getElementById('sources-thead');
        var tbody = document.getElementById('sources-tbody');
        var expanded = localStorage.getItem('recon-sources-expanded') === 'true';

        function apply() {
            var show = expanded ? '' : 'none';
            thead.style.display = show;
            tbody.style.display = show;
            arrow.innerHTML = expanded ? '&#9660;' : '&#9654;';
        }

        toggle.addEventListener('click', function() {
            expanded = !expanded;
            localStorage.setItem('recon-sources-expanded', expanded);
            apply();
        });

        apply();
    }

    document.addEventListener('DOMContentLoaded', function() {
        initSourcesToggle();
        RECON.startRefresh(loadDashboard, 30000);
        loadCharts();
        setInterval(loadCharts, 300000); // refresh charts every 5 min
    });
})();
