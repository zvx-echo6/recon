/* RECON Scraper Dashboard JS */
(function() {
    'use strict';

    function loadJobs() {
        return RECON.fetchJSON('/api/scraper/jobs').then(function(data) {
            var jobs = data.jobs || [];

            // Stats
            var total = jobs.length;
            var active = 0, complete = 0, failed = 0;
            jobs.forEach(function(j) {
                if (j.status === 'complete') complete++;
                else if (j.status === 'failed') failed++;
                else if (j.status === 'running' || j.status === 'pending') active++;
            });
            RECON.set('sc-total', RECON.fmt(total));
            RECON.set('sc-active', RECON.fmt(active));
            RECON.set('sc-complete', RECON.fmt(complete));
            RECON.set('sc-failed', RECON.fmt(failed));

            // Table
            var html = '';
            jobs.forEach(function(j) {
                var badge = statusBadge(j.status);
                var mode = j.crawl_mode ?
                    '<span class="text-small">' + j.crawl_mode + '</span>' : '<span class="text-muted">\u2014</span>';
                var pages = j.page_count ? RECON.fmt(j.page_count) : '\u2014';
                var zim = j.zim_filename ?
                    '<span class="text-small">' + j.zim_filename + '</span>' : '\u2014';
                var actions = '';

                if (j.status === 'running' || j.status === 'pending') {
                    actions = '<button class="btn btn-danger" onclick="SCRAPER.cancel(' + j.id + ')">Cancel</button>';
                } else if (j.status === 'failed' || j.status === 'cancelled') {
                    actions = '<button class="btn" onclick="SCRAPER.retry(' + j.id + ')">Retry</button>';
                }

                // Truncate URL for display
                var displayUrl = j.url.length > 40 ? j.url.substring(0, 40) + '\u2026' : j.url;

                html += '<tr>' +
                    '<td>' + j.id + '</td>' +
                    '<td><a href="' + escHtml(j.url) + '" target="_blank" title="' + escHtml(j.url) + '">' + escHtml(displayUrl) + '</a></td>' +
                    '<td>' + escHtml(j.title || '\u2014') + '</td>' +
                    '<td>' + mode + '</td>' +
                    '<td>' + pages + '</td>' +
                    '<td>' + badge + errorTooltip(j) + '</td>' +
                    '<td>' + zim + '</td>' +
                    '<td>' + actions + '</td>' +
                    '</tr>';
            });
            if (!html) html = '<tr><td colspan="8" class="text-muted">No scrape jobs</td></tr>';
            RECON.setHTML('sc-table-body', html);
        }).catch(function(err) {
            console.error('Scraper dashboard error:', err);
        });
    }

    function statusBadge(status) {
        var map = {
            'pending': '<span class="badge-detected">PENDING</span>',
            'running': '<span class="badge-processing">RUNNING</span>',
            'complete': '<span class="badge-complete">COMPLETE</span>',
            'failed': '<span class="badge-failed">FAILED</span>',
            'cancelled': '<span class="badge-detected">CANCELLED</span>'
        };
        return map[status] || '<span class="badge-detected">' + (status || 'UNKNOWN').toUpperCase() + '</span>';
    }

    function errorTooltip(job) {
        if (!job.error_message) return '';
        var short = job.error_message.length > 80 ?
            job.error_message.substring(0, 80) + '\u2026' : job.error_message;
        return '<div class="text-small text-muted" style="max-width:200px;word-break:break-all;" title="' +
            escHtml(job.error_message) + '">' + escHtml(short) + '</div>';
    }

    function escHtml(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function submit(e) {
        e.preventDefault();
        var url = document.getElementById('sf-url').value.trim();
        if (!url) return false;

        var body = { url: url };
        var title = document.getElementById('sf-title').value.trim();
        var lang = document.getElementById('sf-lang').value;
        var category = document.getElementById('sf-category').value.trim();
        var mode = document.getElementById('sf-mode').value;

        if (title) body.title = title;
        if (lang) body.language = lang;
        if (category) body.category = category;
        if (mode) body.crawl_mode = mode;

        var btn = document.getElementById('sf-submit-btn');
        var feedback = document.getElementById('sf-feedback');
        btn.disabled = true;
        btn.textContent = 'Submitting...';

        RECON.postJSON('/api/scraper/submit', body).then(function(data) {
            btn.disabled = false;
            btn.textContent = 'Submit';
            if (data.ok) {
                feedback.style.display = 'block';
                feedback.style.color = '#00ff41';
                feedback.textContent = 'Job #' + data.job_id + ' submitted successfully';
                document.getElementById('sf-url').value = '';
                document.getElementById('sf-title').value = '';
                document.getElementById('sf-category').value = '';
                setTimeout(function() { feedback.style.display = 'none'; }, 4000);
                loadJobs();
            } else {
                feedback.style.display = 'block';
                feedback.style.color = '#ff4444';
                feedback.textContent = 'Error: ' + (data.error || 'Unknown error');
            }
        }).catch(function(err) {
            btn.disabled = false;
            btn.textContent = 'Submit';
            feedback.style.display = 'block';
            feedback.style.color = '#ff4444';
            feedback.textContent = 'Network error: ' + err.message;
        });

        return false;
    }

    function cancel(jobId) {
        if (!confirm('Cancel job #' + jobId + '?')) return;
        RECON.postJSON('/api/scraper/cancel/' + jobId).then(function(data) {
            if (data.ok) loadJobs();
            else alert('Error: ' + (data.error || 'Unknown'));
        });
    }

    function retry(jobId) {
        RECON.postJSON('/api/scraper/retry/' + jobId).then(function(data) {
            if (data.ok) loadJobs();
            else alert('Error: ' + (data.error || 'Unknown'));
        });
    }

    // Expose for inline onclick
    window.SCRAPER = { submit: submit, cancel: cancel, retry: retry };

    document.addEventListener('DOMContentLoaded', function() {
        RECON.startRefresh(loadJobs, 10000);
    });
})();
