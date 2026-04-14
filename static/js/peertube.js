/* RECON PeerTube Dashboard JS */
(function() {
    'use strict';

    function loadPTDashboard() {
        return RECON.fetchJSON('/api/peertube/dashboard').then(function(data) {
            // Video states
            var vs = data.video_states || {};
            // PeerTube state codes: 1=published, 2=to_transcode, 3=to_import, 4=waiting_for_live, 5=live_ended, 6=to_move_to_external_storage, 7=transcoding_failed, 8=to_edit, 9=waiting_for_live_to_end
            var published = vs['1'] || 0;
            var inPipeline = (vs['2'] || 0) + (vs['3'] || 0) + (vs['6'] || 0) + (vs['8'] || 0);
            var failed = vs['7'] || 0;
            RECON.set('pt-published', RECON.fmt(published));
            RECON.set('pt-in-pipeline', RECON.fmt(inPipeline));
            var failEl = document.getElementById('pt-failed');
            failEl.textContent = RECON.fmt(failed);
            failEl.style.color = failed > 0 ? '#ff4444' : '#00ff41';

            // Import rate from downloader state
            var ds = data.downloader_state || {};
            var rate = ds.imports_last_hour || 0;
            RECON.set('pt-import-rate', RECON.fmt(rate));

            // GPU
            var gpu = data.gpu || {};
            if (gpu.name) {
                RECON.set('pt-gpu-util', gpu.utilization_gpu || '—');
                RECON.set('pt-gpu-temp', gpu.temperature_gpu || '—');
                var gpuPanel = document.getElementById('pt-gpu-panel');
                gpuPanel.style.display = 'block';
                document.getElementById('pt-gpu-detail').innerHTML =
                    '<strong>' + gpu.name + '</strong> | VRAM: ' +
                    RECON.fmt(parseInt(gpu.memory_used || 0)) + ' / ' + RECON.fmt(parseInt(gpu.memory_total || 0)) + ' MiB | ' +
                    'Util: ' + (gpu.utilization_gpu || '?') + '% | ' +
                    'Temp: ' + (gpu.temperature_gpu || '?') + '&deg;C';
            } else {
                RECON.set('pt-gpu-util', '—');
                RECON.set('pt-gpu-temp', '—');
                document.getElementById('pt-gpu-panel').style.display = 'none';
            }

            // Services
            var svcs = data.services || {};
            ['downloader', 'importer', 'transcoder', 'runner'].forEach(function(s) {
                var el = document.getElementById('svc-' + s);
                el.className = 'svc-dot ' + (svcs[s] === 'active' ? 'active' : svcs[s] === 'inactive' ? 'inactive' : 'unknown');
            });

            // Pipeline dirs
            var dirs = data.pipeline_dirs || {};
            var storageHtml = '';
            var dirOrder = ['staging', 'completed', 'transcoded', 'failed'];
            var dirLabels = {staging: 'Downloaded', completed: 'Awaiting Transcode', transcoded: 'Ready to Import', failed: 'Failed'};
            var dirColors = {staging: '#b45309', completed: '#0284c7', transcoded: '#7c3aed', failed: '#dc2626'};
            var totalVideos = 0;
            dirOrder.forEach(function(d) {
                var info = dirs[d] || {};
                var videos = info.videos || 0;
                var bytes = info.bytes || 0;
                totalVideos += videos;
                storageHtml += '<div class="flex-between" style="margin:4px 0;">' +
                    '<span><span class="legend-dot" style="background:' + (dirColors[d] || '#555') + ';"></span>' + (dirLabels[d] || d) + '</span>' +
                    '<span>' + videos + ' videos / ' + RECON.fmtBytes(bytes) + '</span></div>';
            });
            RECON.setHTML('pt-storage-content', storageHtml);

            // Pipeline bar (using video counts)
            var segments = dirOrder.map(function(d) {
                return {status: d, count: (dirs[d] || {}).videos || 0, color: dirColors[d], label: dirLabels[d] || d};
            });
            RECON.setHTML('pt-pipeline-bar', RECON.progressBar(segments, totalVideos || 1));
            RECON.setHTML('pt-pipeline-legend', RECON.progressLegend(segments));
            RECON.set('pt-pipeline-summary', totalVideos + ' videos in pipeline');

            // Errors
            var errors = data.recent_errors || [];
            var errPanel = document.getElementById('pt-errors-panel');
            RECON.set('pt-error-count', errors.length);
            if (errors.length > 0) {
                errPanel.classList.add('has-errors');
                var errHtml = '';
                errors.forEach(function(e) {
                    errHtml += '<div class="error-line">' + e + '</div>';
                });
                RECON.setHTML('pt-errors-content', errHtml);
            } else {
                errPanel.classList.remove('has-errors');
            }
        }).catch(function(err) {
            console.error('PT dashboard error:', err);
        });
    }

    function loadCharts() {
        if (typeof ReconChart !== 'undefined') {
            ReconChart.loadAndDraw('pt-chart', 'peertube',
                ['published', 'backlog'], ['Published', 'Backlog'], 24);
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        RECON.startRefresh(loadPTDashboard, 30000);
        loadCharts();
        setInterval(loadCharts, 300000);
    });
})();
