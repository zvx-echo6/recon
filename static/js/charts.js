/* RECON Lightweight Canvas Line Chart
 * No dependencies. drawLineChart(canvasId, datasets, opts)
 * DPI-aware rendering for sharp lines on all displays.
 */
var ReconChart = (function() {
    'use strict';

    var COLORS = ['#00ff41', '#0ea5e9', '#ffa500', '#ff4444', '#7c3aed', '#fbbf24'];

    function drawLineChart(canvasId, datasets, opts) {
        opts = opts || {};
        var canvas = document.getElementById(canvasId);
        if (!canvas) return;

        // DPI-aware sizing — match canvas bitmap to actual CSS pixels
        var dpr = window.devicePixelRatio || 1;
        var rect = canvas.getBoundingClientRect();
        var cssW = rect.width || 800;
        var cssH = rect.height || 200;
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;

        var ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        var W = cssW;
        var H = cssH;
        var pad = {top: 20, right: 20, bottom: 30, left: 60};
        var plotW = W - pad.left - pad.right;
        var plotH = H - pad.top - pad.bottom;

        // Clear
        ctx.fillStyle = '#111';
        ctx.fillRect(0, 0, W, H);

        if (!datasets || datasets.length === 0) {
            ctx.fillStyle = '#666';
            ctx.font = '12px Courier New';
            ctx.textAlign = 'center';
            ctx.fillText('No data', W/2, H/2);
            return;
        }

        // Find global min/max Y
        var allY = [];
        var allX = [];
        datasets.forEach(function(ds) {
            ds.points.forEach(function(p) {
                allY.push(p.y);
                allX.push(p.x);
            });
        });
        if (allY.length === 0) return;

        var minY = Math.min.apply(null, allY);
        var maxY = Math.max.apply(null, allY);
        var minX = Math.min.apply(null, allX);
        var maxX = Math.max.apply(null, allX);

        // Add 10% padding to Y
        var yRange = maxY - minY || 1;
        minY = Math.max(0, minY - yRange * 0.05);
        maxY = maxY + yRange * 0.1;
        var xRange = maxX - minX || 1;

        function xToCanvas(x) { return pad.left + ((x - minX) / xRange) * plotW; }
        function yToCanvas(y) { return pad.top + plotH - ((y - minY) / (maxY - minY)) * plotH; }

        // Grid lines
        ctx.strokeStyle = '#222';
        ctx.lineWidth = 1;
        var ySteps = 5;
        for (var i = 0; i <= ySteps; i++) {
            var yVal = minY + (maxY - minY) * (i / ySteps);
            var cy = yToCanvas(yVal);
            ctx.beginPath();
            ctx.moveTo(pad.left, cy);
            ctx.lineTo(W - pad.right, cy);
            ctx.stroke();

            // Y labels
            ctx.fillStyle = '#666';
            ctx.font = '10px Courier New';
            ctx.textAlign = 'right';
            ctx.fillText(Math.round(yVal).toLocaleString(), pad.left - 6, cy + 3);
        }

        // X labels (time)
        ctx.textAlign = 'center';
        ctx.fillStyle = '#666';
        var xSteps = Math.min(6, allX.length);
        for (var j = 0; j < xSteps; j++) {
            var xVal = minX + xRange * (j / (xSteps - 1 || 1));
            var cx = xToCanvas(xVal);
            var d = new Date(xVal);
            var label = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
            ctx.fillText(label, cx, H - 8);
        }

        // Draw lines + dots at each data point
        datasets.forEach(function(ds, idx) {
            var color = ds.color || COLORS[idx % COLORS.length];
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.beginPath();
            var pts = ds.points.sort(function(a, b) { return a.x - b.x; });
            pts.forEach(function(p, i) {
                var x = xToCanvas(p.x);
                var y = yToCanvas(p.y);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();

            // Draw dots at each point for visibility with sparse data
            ctx.fillStyle = color;
            pts.forEach(function(p) {
                var x = xToCanvas(p.x);
                var y = yToCanvas(p.y);
                ctx.beginPath();
                ctx.arc(x, y, 3, 0, Math.PI * 2);
                ctx.fill();
            });

            // Legend label
            if (ds.label) {
                ctx.fillStyle = color;
                ctx.font = '10px Courier New';
                ctx.textAlign = 'left';
                ctx.fillText(ds.label, pad.left + idx * 100, 12);
            }
        });
    }

    function loadAndDraw(canvasId, metricType, keys, labels, hours) {
        hours = hours || 24;
        RECON.fetchJSON('/api/metrics/history?type=' + metricType + '&hours=' + hours).then(function(data) {
            if (!data.points || data.points.length < 2) {
                // Show "collecting data" message instead of hiding
                var canvas = document.getElementById(canvasId);
                if (!canvas) return;
                var container = canvas.parentElement;
                if (container) container.style.display = 'block';
                var dpr = window.devicePixelRatio || 1;
                var rect = canvas.getBoundingClientRect();
                canvas.width = (rect.width || 800) * dpr;
                canvas.height = (rect.height || 200) * dpr;
                var ctx = canvas.getContext('2d');
                ctx.scale(dpr, dpr);
                ctx.fillStyle = '#111';
                ctx.fillRect(0, 0, rect.width, rect.height);
                ctx.fillStyle = '#555';
                ctx.font = '12px Courier New';
                ctx.textAlign = 'center';
                var msg = data.points && data.points.length === 1
                    ? 'Collecting data... (1 snapshot, need 2+)'
                    : 'Collecting data... (snapshots every 2 min)';
                ctx.fillText(msg, (rect.width || 800) / 2, (rect.height || 200) / 2);
                return;
            }

            var container = document.getElementById(canvasId).parentElement;
            if (container) container.style.display = 'block';

            var datasets = keys.map(function(key, i) {
                return {
                    label: labels[i] || key,
                    color: COLORS[i % COLORS.length],
                    points: data.points.map(function(p) {
                        return {
                            x: new Date(p.timestamp).getTime(),
                            y: p.data[key] || 0
                        };
                    })
                };
            });

            drawLineChart(canvasId, datasets);
        }).catch(function() {});
    }

    return {
        drawLineChart: drawLineChart,
        loadAndDraw: loadAndDraw
    };
})();
