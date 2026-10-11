/* RECON Kiwix Updates JS */
(function() {
    'use strict';

    var state = {items: [], tz: 'America/Boise', view: 'list', month: null, cal: null, day: null, editKey: null};
    var DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                  'September', 'October', 'November', 'December'];

    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function fmtTime(iso) {
        if (!iso) return '—';
        try {
            return new Intl.DateTimeFormat('en-US', {
                timeZone: state.tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
            }).format(new Date(iso));
        } catch (e) { return iso; }
    }

    function setTime(t) {
        var p = String(t || 02:00).split(:);
        document.getElementById(ks-time-h).value = (0 + (parseInt(p[0], 10) || 0)).slice(-2);
        document.getElementById(ks-time-m).value = (0 + (parseInt(p[1], 10) || 0)).slice(-2);
    }

    function getTime() {
        return document.getElementById(ks-time-h).value + : + document.getElementById(ks-time-m).value;
    }

    function feedback(msg, isErr) {
        var el = document.getElementById('ku-feedback');
        el.style.color = isErr ? '#ff4444' : '#00ff41';
        el.textContent = msg || '';
        if (msg) setTimeout(function() { if (el.textContent === msg) el.textContent = ''; }, 5000);
    }

    // ── labels ──
    var STATUS_BADGE = {
        'current': 'badge-complete', 'update available': 'badge-processing', 'updating': 'badge-ingesting',
        'not in catalog': 'badge-detected', 'self-crawl': 'badge-detected', 'not checked': 'badge-detected',
        'check failed': 'badge-failed'
    };
    var RESULT_LABEL = {updated: 'Updated', current: 'Already current', skipped: 'Skipped', failed: 'Failed',
                        running: 'Running', scheduled: 'Scheduled'};
    var ACTION_LABEL = {update: 'Update', rollback: 'Rollback', recrawl: 'Re-crawl'};

    function resultBadge(status, action) {
        var label = RESULT_LABEL[status] || status;
        if (action === 'rollback' && status === 'updated') label = 'Rolled back';
        if (action === 'recrawl' && status === 'updated') label = 'Re-crawled';
        return '<span class="badge-' + esc(status) + '">' + esc(label) + '</span>';
    }

    function scheduleText(s) {
        if (!s || !s.enabled) return '<span class="text-muted">Off</span>';
        var t = s.run_time || '02:00', txt;
        if (s.frequency === 'monthly') {
            txt = (s.interval_n > 1 ? 'Every ' + s.interval_n + ' months' : 'Monthly') + ', day ' + s.monthday + ' ' + t;
        } else {
            txt = 'Weekly, ' + DOW[s.weekday] + ' ' + t;
        }
        return esc(txt) + '<div class="text-xs text-muted">Next ' + esc(fmtTime(s.next_run_at)) + '</div>';
    }

    // ── updates table ──
    function render(data) {
        state.items = data.items || [];
        state.tz = data.timezone || state.tz;
        var avail = 0, auto = 0;
        state.items.forEach(function(it) {
            if (it.status === 'update available') avail++;
            if (it.schedule && it.schedule.enabled) auto++;
        });
        RECON.set('ku-total', RECON.fmt(state.items.length));
        RECON.set('ku-avail', RECON.fmt(avail));
        RECON.set('ku-auto', RECON.fmt(auto));
        RECON.set('ku-checked', data.last_check_at ? fmtTime(data.last_check_at) : 'Never');
        var btn = document.getElementById('ku-check-all');
        btn.disabled = !!data.checking;
        btn.textContent = data.checking ? 'Checking...' : 'Check for updates';

        var html = '';
        state.items.forEach(function(it) {
            var run = it.running_run;
            var busy = !!run;
            var st = it.status;
            var stHtml = '<span class="' + (STATUS_BADGE[st] || 'badge-detected') + '">' + esc(st.toUpperCase()) + '</span>';
            if (run) {
                var pct = run.bytes_total ? Math.min(100, run.bytes / run.bytes_total * 100).toFixed(0) + '%' : '';
                stHtml += '<div class="text-xs text-muted">' + esc(run.action) +
                    (run.bytes_total ? ': ' + RECON.fmtBytes(run.bytes) + ' / ' + RECON.fmtBytes(run.bytes_total) + ' (' + pct + ')' : '') + '</div>';
            }
            var lr = it.last_run;
            var lrHtml = lr ? resultBadge(lr.status, lr.action) +
                '<div class="text-xs text-muted"><a href="#" class="link" data-act="run" data-run="' + lr.id + '">' + esc(fmtTime(lr.started_at)) + '</a></div>' :
                '<span class="text-muted">—</span>';
            var checked = it.schedule && it.schedule.enabled ? ' checked' : '';
            var autoCell = '<label class="toggle-switch"><input type="checkbox"' + checked +
                ' data-act="auto" data-key="' + esc(it.key) + '"><span class="toggle-slider"></span></label>';
            var actions = '';
            if (it.kind === 'catalog') {
                actions += '<button class="btn btn-sm" data-act="check" data-key="' + esc(it.key) + '"' + (busy ? ' disabled' : '') + '>Check now</button>';
                actions += '<button class="btn btn-sm" data-act="update" data-key="' + esc(it.key) + '"' +
                    (busy || st !== 'update available' ? ' disabled' : '') + '>Update now</button>';
            } else {
                actions += '<button class="btn btn-sm" data-act="recrawl" data-key="' + esc(it.key) + '"' +
                    (busy || !it.can_recrawl ? ' disabled' : '') +
                    (it.recrawl_reason ? ' title="' + esc(it.recrawl_reason) + '"' : '') + '>Re-crawl</button>';
            }
            if (it.has_previous) {
                actions += '<button class="btn btn-sm btn-warn" data-act="rollback" data-key="' + esc(it.key) + '"' +
                    (busy ? ' disabled' : '') + ' title="Previous: ' + esc(it.previous_version) + '">Rollback</button>';
            }
            var latest = it.kind === 'self-crawl' ? '—' : (it.latest_version ? esc(it.latest_version) : '—');
            var dl = it.kind === 'self-crawl' ? '—' : (it.download_size ? RECON.fmtBytes(it.download_size) : '—');
            var errLine = it.check_error ? '<div class="text-xs text-red">' + esc(it.check_error) + '</div>' : '';
            html += '<tr>' +
                '<td><strong>' + esc(it.title) + '</strong><div class="text-small text-muted">' + esc(it.filename) + '</div></td>' +
                '<td>' + esc(it.current_version || '—') + '</td>' +
                '<td class="nowrap">' + latest + '</td>' +
                '<td class="nowrap">' + dl + '</td>' +
                '<td>' + stHtml + errLine + '</td>' +
                '<td>' + autoCell + '</td>' +
                '<td>' + scheduleText(it.schedule) + '<div><a href="#" class="link text-xs" data-act="sched" data-key="' + esc(it.key) + '">Edit</a></div></td>' +
                '<td>' + lrHtml + '</td>' +
                '<td><div class="btn-row">' + actions + '</div></td>' +
                '</tr>';
        });
        if (!html) html = '<tr><td colspan="9" class="text-muted">No ZIMs in the library</td></tr>';
        RECON.setHTML('ku-body', html);
    }

    function load() {
        return RECON.fetchJSON('/api/kiwix/updates').then(function(data) {
            render(data);
            if (state.view === 'cal') loadCalendar();
        });
    }

    // ── actions ──
    function post(url, okMsg) {
        return RECON.postJSON(url).then(function(d) {
            if (d.error) feedback(d.error, true); else feedback(okMsg || 'Started');
            return load().then(function() { return d; });
        }).catch(function(e) { feedback('Request failed: ' + e.message, true); });
    }

    function findItem(key) {
        return state.items.filter(function(i) { return i.key === key; })[0];
    }

    function doAction(act, key) {
        var it = findItem(key);
        if (act === 'check') return post('/api/kiwix/updates/' + encodeURIComponent(key) + '/check', 'Checked');
        if (act === 'update') {
            if (!confirm('Update "' + it.title + '" from ' + it.current_version + ' to ' + it.latest_version + '?')) return;
            return post('/api/kiwix/updates/' + encodeURIComponent(key) + '/update', 'Update started');
        }
        if (act === 'rollback') {
            if (!confirm('Roll back "' + it.title + '" to ' + it.previous_version + '?')) return;
            return post('/api/kiwix/updates/' + encodeURIComponent(key) + '/rollback', 'Rollback started');
        }
        if (act === 'recrawl') {
            if (!confirm('Re-crawl "' + it.title + '"?')) return;
            return post('/api/kiwix/updates/' + encodeURIComponent(key) + '/recrawl', 'Re-crawl queued');
        }
    }

    // ── schedule editor ──
    function openSchedule(key, enable) {
        var it = findItem(key);
        if (!it) return;
        var s = it.schedule || {frequency: 'weekly', weekday: 6, monthday: 1, interval_n: 1, run_time: '02:00', enabled: 0};
        state.editKey = key;
        document.getElementById('ks-title').textContent = it.title;
        document.getElementById('ks-tz').textContent = state.tz;
        document.getElementById('ks-freq').value = s.frequency || 'weekly';
        document.getElementById('ks-wd').value = String(s.weekday === null || s.weekday === undefined ? 6 : s.weekday);
        document.getElementById('ks-md').value = s.monthday || 1;
        document.getElementById('ks-n').value = s.interval_n || 1;
        setTime(s.run_time || '02:00');
        document.getElementById('ks-enabled').checked = enable ? true : !!s.enabled;
        document.getElementById('ks-feedback').textContent = '';
        syncFreq();
        var panel = document.getElementById('ku-sched');
        panel.style.display = 'block';
        panel.scrollIntoView({block: 'nearest'});
    }

    function syncFreq() {
        var monthly = document.getElementById('ks-freq').value === 'monthly';
        document.getElementById('ks-wd-wrap').style.display = monthly ? 'none' : '';
        document.getElementById('ks-md-wrap').style.display = monthly ? '' : 'none';
        document.getElementById('ks-n-wrap').style.display = monthly ? '' : 'none';
    }

    function putSchedule(key, body) {
        return fetch('/api/kiwix/updates/' + encodeURIComponent(key) + '/schedule', {
            method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        }).then(function(r) { return r.json(); });
    }

    function saveSchedule() {
        var body = {
            enabled: document.getElementById('ks-enabled').checked,
            frequency: document.getElementById('ks-freq').value,
            weekday: parseInt(document.getElementById('ks-wd').value, 10),
            monthday: parseInt(document.getElementById('ks-md').value, 10),
            interval_n: parseInt(document.getElementById('ks-n').value, 10),
            run_time: getTime()
        };
        putSchedule(state.editKey, body).then(function(d) {
            var fb = document.getElementById('ks-feedback');
            if (d.error) { fb.style.color = '#ff4444'; fb.textContent = d.error; return; }
            document.getElementById('ku-sched').style.display = 'none';
            state.editKey = null;
            feedback('Schedule saved');
            load();
        });
    }

    function toggleAuto(key, on) {
        var it = findItem(key);
        var s = it && it.schedule;
        if (on && !s) { openSchedule(key, true); return; }
        var body = s ? {enabled: on, frequency: s.frequency, weekday: s.weekday, monthday: s.monthday,
                        interval_n: s.interval_n, run_time: s.run_time} : {enabled: on};
        putSchedule(key, body).then(function(d) {
            if (d.error) feedback(d.error, true); else feedback(on ? 'Auto-update on' : 'Auto-update off');
            load();
        });
    }

    // ── calendar ──
    function monthStr(y, m) { return y + '-' + (m < 10 ? '0' : '') + m; }

    function loadCalendar() {
        if (!state.month) {
            var n = new Date();
            state.month = {y: n.getFullYear(), m: n.getMonth() + 1};
        }
        return RECON.fetchJSON('/api/kiwix/updates/calendar?month=' + monthStr(state.month.y, state.month.m)).then(function(d) {
            state.cal = d;
            if (!state.today) state.today = d.today;
            renderCalendar();
        });
    }

    function renderCalendar() {
        var y = state.month.y, m = state.month.m;
        RECON.set('kc-title', MONTHS[m - 1] + ' ' + y);
        var first = new Date(Date.UTC(y, m - 1, 1));
        var lead = (first.getUTCDay() + 6) % 7;            // Monday-first grid
        var days = new Date(Date.UTC(y, m, 0)).getUTCDate();
        var byDate = {};
        (state.cal.events || []).forEach(function(e) { (byDate[e.date] = byDate[e.date] || []).push(e); });
        var html = '';
        DOW.forEach(function(d) { html += '<div class="cal-dow">' + d + '</div>'; });
        var total = Math.ceil((lead + days) / 7) * 7;
        for (var i = 0; i < total; i++) {
            var dt = new Date(Date.UTC(y, m - 1, 1 - lead + i));
            var ds = dt.getUTCFullYear() + '-' + String(dt.getUTCMonth() + 1).padStart(2, '0') + '-' + String(dt.getUTCDate()).padStart(2, '0');
            var inMonth = dt.getUTCMonth() === m - 1;
            var cls = 'cal-day' + (inMonth ? '' : ' other') + (ds === state.cal.today ? ' today' : '') + (ds === state.day ? ' sel' : '');
            var evs = inMonth ? (byDate[ds] || []) : [];
            var cell = '<div class="cal-num">' + dt.getUTCDate() + '</div>';
            evs.slice(0, 3).forEach(function(e) {
                cell += '<span class="cal-ev ' + esc(e.status) + '" title="' + esc(e.title + ' - ' + (RESULT_LABEL[e.status] || e.status)) + '">' +
                    esc(e.time) + ' ' + esc(e.title) + '</span>';
            });
            if (evs.length > 3) cell += '<div class="cal-more">+' + (evs.length - 3) + ' more</div>';
            html += '<div class="' + cls + '" data-act="day" data-date="' + ds + '">' + cell + '</div>';
        }
        RECON.setHTML('kc-grid', html);
        if (state.day) renderDay();
    }

    function renderDay() {
        var evs = (state.cal.events || []).filter(function(e) { return e.date === state.day; });
        var panel = document.getElementById('kc-day');
        panel.style.display = 'block';
        RECON.set('kc-day-title', state.day);
        var html = '';
        evs.forEach(function(e, idx) {
            var btns = '';
            if (e.run_id) btns += '<button class="btn btn-sm" data-act="run" data-run="' + e.run_id + '">Details</button>';
            if (e.kind === 'scheduled') {
                btns += '<button class="btn btn-sm" data-act="run-now" data-key="' + esc(e.key) + '" data-crawl="' + (e.is_crawl ? 1 : 0) + '">Run now</button>';
            }
            html += '<tr><td>' + esc(e.time) + '</td><td>' + esc(e.title) + '</td><td>' +
                esc(ACTION_LABEL[e.action] || e.action) + '</td><td>' + esc(e.trigger === 'schedule' ? 'Schedule' : 'Manual') + '</td><td>' +
                (e.kind === 'scheduled' ? '<span class="badge-scheduled">Scheduled</span>' : resultBadge(e.status, e.action)) +
                (e.message ? '<div class="text-xs text-muted">' + esc(e.message) + '</div>' : '') +
                '</td><td><div class="btn-row">' + btns + '</div></td></tr>';
        });
        if (!html) html = '<tr><td colspan="6" class="text-muted">Nothing on this day</td></tr>';
        RECON.setHTML('kc-day-body', html);
    }

    function showRun(id) {
        RECON.fetchJSON('/api/kiwix/updates/runs/' + id).then(function(r) {
            var panel = document.getElementById('kc-run');
            var ver = (r.old_version || '—') + ' → ' + (r.new_version || '—');
            panel.innerHTML = '<h3 class="section-title" style="margin-bottom:12px;">Run #' + r.id + ': ' + esc(r.title || r.zim_key) + '</h3>' +
                '<div class="grid-3 mb-16 text-small">' +
                '<div><span class="text-muted">Action</span><br>' + esc(ACTION_LABEL[r.action] || r.action) + '</div>' +
                '<div><span class="text-muted">Result</span><br>' + resultBadge(r.status, r.action) + '</div>' +
                '<div><span class="text-muted">Versions</span><br>' + esc(ver) + '</div>' +
                '<div><span class="text-muted">Started</span><br>' + esc(fmtTime(r.started_at)) + '</div>' +
                '<div><span class="text-muted">Ended</span><br>' + esc(fmtTime(r.ended_at)) + '</div>' +
                '<div><span class="text-muted">Downloaded</span><br>' + (r.bytes ? RECON.fmtBytes(r.bytes) : '—') + '</div>' +
                '</div>' +
                '<div class="text-small mb-16">' + esc(r.message || '') + '</div>' +
                '<div class="log-box">' + esc(r.log || '') + '</div>';
            panel.style.display = 'block';
            panel.scrollIntoView({block: 'nearest'});
        });
    }

    function setView(v) {
        state.view = v;
        document.getElementById('ku-view-list').style.display = v === 'list' ? '' : 'none';
        document.getElementById('ku-view-cal').style.display = v === 'cal' ? '' : 'none';
        document.getElementById('ku-tab-list').classList.toggle('active', v === 'list');
        document.getElementById('ku-tab-cal').classList.toggle('active', v === 'cal');
        if (v === 'cal') loadCalendar();
    }

    function shiftMonth(delta) {
        var m = state.month.m + delta, y = state.month.y;
        if (m < 1) { m = 12; y--; }
        if (m > 12) { m = 1; y++; }
        state.month = {y: y, m: m};
        state.day = null;
        document.getElementById('kc-day').style.display = 'none';
        loadCalendar();
    }

    // ── events ──
    document.addEventListener('click', function(ev) {
        var el = ev.target.closest('[data-act]');
        if (!el) return;
        var act = el.getAttribute('data-act'), key = el.getAttribute('data-key');
        if (act === 'auto') return;           // handled by change
        ev.preventDefault();
        if (act === 'view') return setView(el.getAttribute('data-view'));
        if (act === 'check-all') {
            return RECON.postJSON('/api/kiwix/updates/check').then(function() { feedback('Checking...'); load(); });
        }
        if (act === 'sched') return openSchedule(key, false);
        if (act === 'sched-save') return saveSchedule();
        if (act === 'sched-cancel') { document.getElementById('ku-sched').style.display = 'none'; load(); return; }
        if (act === 'run') return showRun(el.getAttribute('data-run'));
        if (act === 'cal-prev') return shiftMonth(-1);
        if (act === 'cal-next') return shiftMonth(1);
        if (act === 'cal-today') { state.month = null; state.day = null; document.getElementById('kc-day').style.display = 'none'; return loadCalendar(); }
        if (act === 'day') { state.day = el.getAttribute('data-date'); renderCalendar(); return; }
        if (act === 'run-now') {
            var crawl = el.getAttribute('data-crawl') === '1';
            if (!confirm('Run ' + (crawl ? 're-crawl' : 'update') + ' now?')) return;
            return post('/api/kiwix/updates/' + encodeURIComponent(key) + (crawl ? '/recrawl' : '/update'), 'Started');
        }
        doAction(act, key);
    });

    document.addEventListener('change', function(ev) {
        var el = ev.target;
        if (el.getAttribute && el.getAttribute('data-act') === 'auto') {
            toggleAuto(el.getAttribute('data-key'), el.checked);
        } else if (el.id === 'ks-freq') {
            syncFreq();
        }
    });

    document.addEventListener('DOMContentLoaded', function() {
        RECON.startRefresh(function() {
            if (state.editKey) return Promise.resolve();      // do not redraw while editing
            return load();
        }, 5000);
    });
})();
