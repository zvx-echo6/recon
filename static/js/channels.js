/* RECON PeerTube Channels page JS */
(function() {
    'use strict';

    async function loadChannelStats() {
        try {
            var resp = await fetch('/api/peertube/channels/stats');
            var data = await resp.json();
            if (resp.ok) {
                document.getElementById('pt-total-ch').textContent = data.total_channels;
                document.getElementById('pt-total-vid').textContent = data.total_videos;
                var dlEl = document.getElementById('pt-dl-status');
                dlEl.textContent = data.downloader_active ? 'Active' : 'Stopped';
                dlEl.style.color = data.downloader_active ? '#00ff41' : '#ff4444';
            }
        } catch(e) {
            console.error('Stats error:', e);
        }
    }

    async function loadChannels() {
        try {
            var resp = await fetch('/api/peertube/channels');
            var data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Failed');
            var tbody = document.getElementById('pt-channel-tbody');
            if (!data.length) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:#555;">No channels configured</td></tr>';
                return;
            }
            var cats = [];
            var catSet = {};
            data.forEach(function(c) { if (c.category && !catSet[c.category]) { catSet[c.category] = true; cats.push(c.category); } });
            document.getElementById('pt-cat-list').innerHTML = cats.map(function(c) { return '<option value="' + c + '">'; }).join('');

            var html = '';
            data.forEach(function(ch) {
                var vids = ch.videos_in_peertube || 0;
                var statusColor = vids > 0 ? '#00ff41' : '#ffa500';
                var statusText = vids > 0 ? 'syncing' : 'new';
                var ytLink = ch.youtube_url ? '<a href="' + ch.youtube_url + '" target="_blank" style="color:#00a0d0;text-decoration:none;">' + ch.channel_name + '</a>' : ch.channel_name;
                html += '<tr style="border-bottom:1px solid #1a1a1a;">' +
                    '<td style="padding:8px 10px;">' + ytLink + '</td>' +
                    '<td style="padding:8px 10px;text-align:center;">' + vids + '</td>' +
                    '<td style="padding:8px 10px;color:#888;">' + (ch.category || '') + '</td>' +
                    '<td style="padding:8px 10px;text-align:center;">' + (ch.priority || 'M') + '</td>' +
                    '<td style="padding:8px 10px;text-align:center;"><span style="color:' + statusColor + ';">' + statusText + '</span></td>' +
                    '<td style="padding:8px 10px;text-align:center;"><button onclick="removeChannel(\'' + ch.actor_name + '\')" style="background:none;border:1px solid #333;color:#ff4444;cursor:pointer;padding:2px 8px;font-size:11px;font-family:inherit;">x</button></td>' +
                    '</tr>';
            });
            tbody.innerHTML = html;
        } catch(e) {
            document.getElementById('pt-channel-tbody').innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:#ff4444;">Error: ' + e.message + '</td></tr>';
        }
    }

    window.addChannel = async function() {
        var fb = document.getElementById('pt-feedback');
        var url = document.getElementById('pt-yt-url').value.trim();
        if (!url) {
            fb.style.color = '#ff4444';
            fb.textContent = 'Enter a YouTube channel URL';
            return;
        }
        var category = document.getElementById('pt-category').value.trim();
        var priority = document.getElementById('pt-priority').value;
        var btn = document.getElementById('pt-add-btn');
        btn.disabled = true;
        fb.style.color = '#ffa500';
        fb.textContent = 'Resolving channel...';
        try {
            var resp = await fetch('/api/peertube/channels/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({youtube_url: url, category: category, priority: priority})
            });
            var data = await resp.json();
            if (resp.ok) {
                fb.style.color = '#00ff41';
                fb.textContent = 'Added: ' + (data.channel_name || 'channel');
                document.getElementById('pt-yt-url').value = '';
                loadChannels();
                loadChannelStats();
            } else {
                fb.style.color = '#ff4444';
                fb.textContent = data.error || 'Failed to add channel';
            }
        } catch(e) {
            fb.style.color = '#ff4444';
            fb.textContent = 'Error: ' + e.message;
        }
        btn.disabled = false;
    };

    window.removeChannel = async function(actorName) {
        if (!confirm('Remove channel ' + actorName + '?')) return;
        var fb = document.getElementById('pt-feedback');
        fb.style.color = '#ffa500';
        fb.textContent = 'Removing...';
        try {
            var resp = await fetch('/api/peertube/channels/' + encodeURIComponent(actorName), {method: 'DELETE'});
            var data = await resp.json();
            if (resp.ok) {
                fb.style.color = '#00ff41';
                fb.textContent = data.message || 'Removed';
                loadChannels();
                loadChannelStats();
            } else {
                fb.style.color = '#ff4444';
                fb.textContent = data.error || 'Failed';
            }
        } catch(e) {
            fb.style.color = '#ff4444';
            fb.textContent = 'Error: ' + e.message;
        }
    };

    loadChannelStats();
    loadChannels();
})();
