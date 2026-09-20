/* MeshCore Bot — landing page dashboard.
 *
 * Served as a static file so `script-src 'self'` covers it and it needs no CSP
 * nonce.  Server-provided configuration arrives in a
 * <script type="application/json" id="dashboard-boot"> block, which is data
 * rather than code and therefore also nonce-free.
 *
 * Everything on the page is driven by /api/dashboard/summary, a single
 * snapshot read.  Polling sends If-None-Match and normally gets a bodyless
 * 304, and pauses entirely while the tab is hidden — the old page kept
 * recomputing ~50 aggregate queries every 30s in background tabs forever.
 */
(function () {
    'use strict';

    const BOOT = readBoot();
    const SUMMARY_POLL_MS = 30000;
    const HEALTH_POLL_MS = 15000;

    // Palette shared with the multibyte rollout page so the same concept keeps
    // the same colour across screens.
    const COLOR = {
        multibyte: '#0891b2',
        singleByte: '#a8a29e',
        flood: '#0d6efd',
        direct: '#20c997',
        accent: '#6f42c1',
        series: '#0d6efd',
    };

    const MIX_COLORS = [
        '#0d6efd', '#20c997', '#fd7e14', '#6f42c1',
        '#d63384', '#198754', '#0dcaf0', '#adb5bd',
    ];

    // Payload buckets on the encoding chart.  Must stay in the same order as
    // PACKET_ENCODING_BUCKETS server-side: the slot index picks the colour, so a
    // type keeps its colour even on a day when it has no traffic and drops out
    // of the chart.  Colour by rank instead and one quiet day repaints the lot.
    // Append new types; inserting one would recolour everything after it.
    const PACKET_ENCODING_TYPES = [
        'GRP_TXT', 'RESPONSE', 'REQ', 'PATH', 'TXT_MSG', 'ANON_REQ', 'GRP_DATA', 'ADVERT',
        // The uncharted tail (ACK, TRACE, unmapped ordinals). Present so the
        // bars measure the whole day's traffic, not just the named types.
        'OTHER',
    ];

    // Eight categorical slots, each mode stepped for its own card surface
    // (#ffffff light, #2d2d2d dark) rather than flipped from the other.  Both
    // columns are validated for lightness band, chroma floor, colour-vision
    // separation (worst adjacent pair ΔE 9.1 light / 8.4 dark) and
    // normal-vision separation (19.6 / 19.3).  Several sit under 3:1 against
    // the surface, which is why the legend is not optional here — identity has
    // to survive without the colour.  Eight is the ceiling for hues: the ninth
    // slot is deliberately a neutral, because "everything else" is not a
    // category and must not compete with the ones that are.
    const SERIES_PALETTE = {
        light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948', '#a8a29e'],
        dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767', '#87817c'],
    };

    // Segments thinner than this fraction of the y-axis range are drawn without
    // the hairline that separates stacked bands — below it the border is
    // thicker than the band and erases the colour instead of framing it.
    const MIN_SEGMENT_FRACTION_FOR_GAP = 0.01;

    // The y-axis tops out at the tallest bar rounded up to the next multiple of
    // this, so a 24% peak gets a 25% axis and a 26% peak gets 30%.
    const AXIS_STEP_PCT = 5;

    function readBoot() {
        const el = document.getElementById('dashboard-boot');
        if (!el) return {};
        try {
            return JSON.parse(el.textContent) || {};
        } catch (err) {
            console.error('Invalid dashboard boot config', err);
            return {};
        }
    }

    // ── small DOM/format helpers ─────────────────────────────────────────

    function el(id) {
        return document.getElementById(id);
    }

    function setText(id, value) {
        const node = el(id);
        if (node) node.textContent = String(value ?? '');
    }

    function makeTextElement(tagName, value, className) {
        const node = document.createElement(tagName);
        if (className) node.className = className;
        node.textContent = String(value ?? '');
        return node;
    }

    function makeBadge(value, classes) {
        return makeTextElement('span', value, 'badge ' + classes);
    }

    /** Em dash for absent data. Zero is a real value and must not become one. */
    function formatNumber(value) {
        if (value === null || value === undefined || Number.isNaN(value)) return '—';
        const n = Number(value);
        if (!Number.isFinite(n)) return '—';
        return n.toLocaleString();
    }

    function formatPercent(value, digits) {
        if (value === null || value === undefined) return '—';
        const n = Number(value);
        if (!Number.isFinite(n)) return '—';
        return n.toFixed(digits === undefined ? 1 : digits) + '%';
    }

    function formatSigned(value) {
        if (value === null || value === undefined) return '—';
        const n = Number(value);
        if (!Number.isFinite(n)) return '—';
        return n.toFixed(1);
    }

    function formatAge(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        const s = Math.max(0, Math.round(seconds));
        if (s < 60) return s + 's ago';
        if (s < 3600) return Math.round(s / 60) + 'm ago';
        return Math.round(s / 3600) + 'h ago';
    }

    function formatUptime(seconds) {
        if (seconds === null || seconds === undefined || seconds <= 0) return 'bot down?';
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        if (days) return days + 'd ' + hours + 'h';
        if (hours) return hours + 'h ' + minutes + 'm';
        return minutes + 'm';
    }

    function formatBytes(bytes) {
        if (!bytes && bytes !== 0) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = Number(bytes);
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return value.toFixed(value >= 10 || unit === 0 ? 0 : 1) + ' ' + units[unit];
    }

    function isDark() {
        return document.documentElement.getAttribute('data-theme') === 'dark';
    }

    function themeColors() {
        const cs = getComputedStyle(document.documentElement);
        return {
            text: (cs.getPropertyValue('--text-color') || '#212529').trim(),
            muted: (cs.getPropertyValue('--text-muted') || '#6c757d').trim(),
            grid: isDark() ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)',
            empty: (cs.getPropertyValue('--bg-tertiary') || '#dee2e6').trim(),
            // The card the chart sits on. Stacked segments are separated by a
            // hairline of the surface itself rather than a drawn border, so the
            // gap reads as space instead of as an extra category.
            surface: (cs.getPropertyValue('--card-bg') || '#ffffff').trim(),
        };
    }

    function noAnimation(options) {
        options.animation = false;
        options.animations = false;
        options.transitions = {
            active: { animation: { duration: 0 } },
            resize: { animation: { duration: 0 } },
            show: { animation: { duration: 0 } },
            hide: { animation: { duration: 0 } },
        };
        return options;
    }

    // ── dashboard ────────────────────────────────────────────────────────

    class ModernDashboard {
        constructor() {
            this.charts = {};
            this.summaryEtag = null;
            this.lastGeneratedAt = null;
            this.botUptime = null;
            this.timers = [];
            this.windows = null;
            this.summary = null;
            this.init();
        }

        init() {
            this.wireSelectors();
            this.wireRefreshButton();
            this.watchTheme();
            this.loadWindows();
            this.loadHealth();
            this.loadSummary();
            this.timers.push(setInterval(() => this.whenVisible(() => this.loadHealth()), HEALTH_POLL_MS));
            this.timers.push(setInterval(() => this.whenVisible(() => this.loadSummary()), SUMMARY_POLL_MS));
            this.timers.push(setInterval(() => this.tickUptime(), 1000));
            // A tab returning to the foreground may have missed several polls.
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') {
                    this.loadHealth();
                    this.loadSummary();
                }
            });
        }

        /** Skip polling in a hidden tab: nobody is reading it and it costs SQLite. */
        whenVisible(fn) {
            if (document.visibilityState === 'visible') fn();
        }

        destroy() {
            this.timers.forEach(clearInterval);
            this.timers = [];
            Object.values(this.charts).forEach((chart) => chart && chart.destroy());
            this.charts = {};
        }

        // -- theme --------------------------------------------------------

        /**
         * DarkModeManager.applyTheme() emits no event, so observe the attribute
         * it writes and rebuild chart colours when it flips.
         */
        watchTheme() {
            const observer = new MutationObserver((mutations) => {
                if (mutations.some((m) => m.attributeName === 'data-theme')) {
                    this.rebuildCharts();
                }
            });
            observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
            this.themeObserver = observer;
        }

        rebuildCharts() {
            Object.values(this.charts).forEach((chart) => chart && chart.destroy());
            this.charts = {};
            if (this.summary) this.render(this.summary);
        }

        // -- data ---------------------------------------------------------

        async loadHealth() {
            try {
                const response = await fetch('/api/health');
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const data = await response.json();
                this.botUptime = data.bot_uptime > 0 ? data.bot_uptime : null;
                setText('health-clients', data.connected_clients ?? 0);
                setText('health-uptime', formatUptime(this.botUptime));
                const radio = el('health-radio');
                if (radio) {
                    const degraded = Boolean(data.radio_zombie);
                    radio.textContent = degraded ? 'radio degraded' : 'radio ok';
                    radio.className = 'health-strip__item ' + (degraded ? 'text-warning' : '');
                }
                this.setStatusDot('health-status-dot', data.status === 'healthy' ? 'connected' : 'warning');
                setText('health-status-text', data.status === 'healthy' ? 'Online' : 'Degraded');
            } catch (err) {
                this.setStatusDot('health-status-dot', 'disconnected');
                setText('health-status-text', 'Offline');
            }
        }

        async loadSummary() {
            try {
                const headers = {};
                if (this.summaryEtag) headers['If-None-Match'] = this.summaryEtag;
                const response = await fetch('/api/dashboard/summary', { headers });

                if (response.status === 304) {
                    this.updateSnapshotAge();
                    return;
                }
                if (response.status === 503) {
                    setText('health-snapshot', 'building…');
                    return;
                }
                if (!response.ok) throw new Error('HTTP ' + response.status);

                this.summaryEtag = response.headers.get('ETag');
                const data = await response.json();
                this.summary = data;
                this.lastGeneratedAt = data.generated_at;
                this.render(data);
                this.setDatabaseStatus(true);
            } catch (err) {
                console.error('Dashboard summary failed', err);
                this.setDatabaseStatus(false);
            }
        }

        async loadWindows() {
            try {
                const response = await fetch('/api/dashboard/windows');
                if (!response.ok) return;
                this.windows = await response.json();
                Object.entries(TOP_LISTS).forEach(([kind, config]) => {
                    this.populateWindowOptions(config.select, kind);
                });
            } catch (err) {
                console.warn('Window options unavailable', err);
            } finally {
                // Load the lists whether or not option derivation succeeded.
                Object.keys(TOP_LISTS).forEach((kind) => this.loadTop(kind));
            }
        }

        /**
         * Build the <option>s from retention rather than hard-coding them: the
         * old page offered "30d" and "All" against tables pruned at 7 days, so
         * three of its four choices returned the same number under a label that
         * claimed otherwise.
         */
        populateWindowOptions(selectId, kind) {
            const select = el(selectId);
            const options = this.windows && this.windows.windows && this.windows.windows[kind];
            if (!select || !options) return;
            const previous = select.value;
            select.replaceChildren();
            options.forEach((option) => {
                const node = document.createElement('option');
                node.value = option.value;
                node.textContent = option.label;
                select.appendChild(node);
            });
            const stillValid = options.some((option) => option.value === previous);
            select.value = stillValid ? previous : options[options.length - 1].value;
        }

        async loadTop(kind) {
            const config = TOP_LISTS[kind];
            if (!config) return;
            const container = el(config.container);
            if (!container) return;
            const select = el(config.select);
            const window_ = select ? select.value : 'all';
            try {
                const response = await fetch(
                    '/api/dashboard/top?kind=' + encodeURIComponent(kind) +
                    '&window=' + encodeURIComponent(window_) +
                    '&limit=' + config.limit
                );
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const data = await response.json();
                if (config.onLoad) config.onLoad(data);
                if (!data.items || data.items.length === 0) {
                    container.replaceChildren(
                        makeTextElement('div', config.empty, 'dashboard-empty')
                    );
                    return;
                }
                container.replaceChildren();
                data.items.forEach((item, index) => {
                    container.appendChild(config.render(item, index));
                });
            } catch (err) {
                container.replaceChildren(
                    makeTextElement('div', 'Failed to load.', 'dashboard-empty')
                );
            }
        }

        // -- rendering ----------------------------------------------------

        render(data) {
            const mesh = data.mesh || {};
            const bot = data.bot || {};
            const coverage = data.coverage || {};
            const series = data.series || {};
            const deltas = data.deltas || {};

            this.updateSnapshotAge();

            // Mesh tiles
            this.tile('nodes-24h', mesh.nodes_24h, deltas.nodes, series.nodes);
            this.tile('adverts-24h', mesh.adverts_24h, deltas.adverts, series.adverts);
            this.tile('new-nodes', mesh.new_nodes_24h, deltas.new_nodes, series.new_nodes);
            setText('new-nodes-7d', formatNumber(mesh.new_nodes_7d) + ' in 7d');
            setText('gone-quiet', formatNumber(mesh.gone_quiet_7d));
            setText('coverage-countries', formatNumber(mesh.countries));
            setText('coverage-known', formatNumber(mesh.contacts_known));
            setText('coverage-states', formatNumber(mesh.states));
            setText('coverage-cities', formatNumber(mesh.cities));

            // Bot tiles
            this.tile('messages-24h', bot.messages_24h, deltas.messages, series.messages);
            this.tile('commands-24h', bot.commands_24h, deltas.commands, series.commands);
            const replyRate = el('reply-rate-24h');
            if (replyRate) replyRate.textContent = formatPercent(bot.reply_rate_24h);
            setText('unique-users-24h', formatNumber(bot.unique_users_24h));
            setText('unique-channels-24h', formatNumber(bot.unique_channels_24h) + ' channels');

            // Routing, encoding, distributions
            this.renderRouteMix(mesh.route_mix, coverage.packets_window_label);
            this.renderMix('payload-mix', mesh.payload_mix);
            this.renderHopsHistogram(mesh.hops, coverage.packets_window_label);
            this.renderDoughnut('contactsEncodingChart', 'contacts-encoding-summary',
                (mesh.encoding || {}).contacts_7d, 'contacts');
            this.renderDoughnut('packetsEncodingChart', 'packets-encoding-summary',
                (mesh.encoding || {}).packets, 'packets');
            setText('packets-window-label', coverage.packets_window_label || 'no data');
            this.renderPacketEncodingTrend(data.packet_encoding);
            this.renderMix('role-mix', mesh.role_mix);

            this.renderSourceNotes(coverage);
        }

        /** Value + delta chip + sparkline for one tile. */
        tile(id, value, changePct, points) {
            const valueNode = el(id + '-value');
            if (valueNode) {
                valueNode.textContent = formatNumber(value);
                valueNode.classList.toggle('stat-tile__value--muted',
                    value === null || value === undefined);
            }
            this.renderDelta(id + '-delta', changePct);
            this.renderSparkline(id + '-spark', points);
        }

        renderDelta(id, changePct) {
            const node = el(id);
            if (!node) return;
            if (changePct === null || changePct === undefined) {
                node.className = 'delta-chip';
                node.textContent = '–';
                return;
            }
            const up = changePct >= 0;
            node.className = 'delta-chip ' + (up ? 'delta-chip--up' : 'delta-chip--down');
            node.textContent = (up ? '↑' : '↓') + Math.abs(changePct).toFixed(1) + '%';
        }

        /**
         * Shared 48px sparkline. Null points stay null so Chart.js leaves a gap
         * — a backfilled day rendered as zero looks like an outage.
         */
        renderSparkline(canvasId, points) {
            const canvas = el(canvasId);
            if (!canvas || typeof Chart === 'undefined') return;
            const list = Array.isArray(points) ? points : [];
            const colors = themeColors();

            if (this.charts[canvasId]) {
                this.charts[canvasId].destroy();
                delete this.charts[canvasId];
            }
            if (list.length === 0) return;

            this.charts[canvasId] = new Chart(canvas.getContext('2d'), {
                type: 'line',
                data: {
                    labels: list.map((p) => p.date),
                    datasets: [{
                        data: list.map((p) => p.value),
                        borderColor: COLOR.series,
                        backgroundColor: COLOR.series + '22',
                        borderWidth: 1.5,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        spanGaps: false,
                    }],
                },
                options: noAnimation({
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            displayColors: false,
                            callbacks: {
                                label: (ctx) => (ctx.parsed.y === null
                                    ? 'no data'
                                    : formatNumber(ctx.parsed.y)),
                            },
                        },
                    },
                    scales: {
                        x: { display: false },
                        y: { display: false, beginAtZero: true },
                    },
                    elements: { line: { borderJoinStyle: 'round' } },
                    layout: { padding: 0 },
                    color: colors.muted,
                }),
            });
        }

        renderRouteMix(routeMix, windowLabel) {
            const bar = el('route-mix-bar');
            const legend = el('route-mix-legend');
            if (!bar || !legend) return;
            const flood = Number((routeMix || {}).flood) || 0;
            const direct = Number((routeMix || {}).direct) || 0;
            const total = flood + direct;
            setText('route-mix-window', windowLabel || 'no data');

            bar.replaceChildren();
            legend.replaceChildren();
            if (total === 0) {
                bar.appendChild(makeTextElement('div', '', 'stacked-bar__segment'));
                legend.appendChild(makeTextElement('span', 'No dimensioned packets yet.'));
                return;
            }

            [['FLOOD', flood, COLOR.flood], ['DIRECT', direct, COLOR.direct]].forEach(
                ([label, value, color]) => {
                    const pct = (value / total) * 100;
                    const segment = makeTextElement(
                        'div', pct >= 12 ? pct.toFixed(0) + '%' : '', 'stacked-bar__segment'
                    );
                    segment.style.width = pct + '%';
                    segment.style.backgroundColor = color;
                    segment.title = label + ': ' + formatNumber(value);
                    bar.appendChild(segment);

                    const entry = document.createElement('span');
                    const swatch = document.createElement('span');
                    swatch.className = 'stacked-bar__swatch';
                    swatch.style.backgroundColor = color;
                    entry.append(swatch, document.createTextNode(
                        label + ' ' + formatNumber(value) + ' (' + pct.toFixed(1) + '%)'
                    ));
                    legend.appendChild(entry);
                }
            );
        }

        /**
         * Hop distance, two ways: where nodes sit, and where flood traffic
         * comes from.
         *
         * The series count different things — 2.8k nodes against 74k packets —
         * so plotting raw counts together would flatten the node series into
         * the axis. Both are shown as a share of their own total, which is what
         * makes the two shapes comparable; absolute counts stay in the tooltip.
         */
        renderHopsHistogram(hops, packetWindowLabel) {
            const canvasId = 'hopsHistogramChart';
            const canvas = el(canvasId);
            if (!canvas || typeof Chart === 'undefined') return;
            const nodes = Array.isArray((hops || {}).nodes) ? hops.nodes : [];
            const flood = Array.isArray((hops || {}).flood_packets) ? hops.flood_packets : [];
            const colors = themeColors();

            if (this.charts[canvasId]) {
                this.charts[canvasId].destroy();
                delete this.charts[canvasId];
            }
            const withheld = (hops || {}).flood_hidden || {};
            setText('hops-hidden-note', withheld.packets
                ? formatNumber(withheld.packets) + ' further flood packets (' +
                  withheld.share_pct + '%) sit in ' + withheld.buckets +
                  ' hop buckets below ' + withheld.threshold_pct + '% each, and are not drawn.'
                : '');

            if (nodes.length === 0 && flood.length === 0) return;

            const labels = (nodes.length ? nodes : flood).map((b) => b[0]);
            const totals = (hops || {}).totals || {};
            // Percentages are shares of the whole series, not of the bars that
            // survived the display threshold — otherwise hiding the tail would
            // silently inflate every remaining bar.
            const share = (series, total) => series.map(
                (b) => (b[1] === null || !total ? null : (b[1] / total) * 100)
            );
            const nodeShare = share(nodes, totals.nodes);
            const floodShare = share(flood, totals.flood_packets);

            // A path can be 64 hops at one byte per hop, so the axis may carry
            // 64 grouped categories. Rounded corners and default bar padding
            // both disappear at that width, so drop them and let the bars touch.
            const dense = labels.length > 24;
            const datasets = [];
            if (nodes.length) {
                datasets.push({
                    label: 'Nodes (adverts, 7d)',
                    data: nodeShare,
                    counts: nodes.map((b) => b[1]),
                    unit: 'nodes',
                    backgroundColor: COLOR.accent,
                    borderRadius: dense ? 0 : 3,
                    barPercentage: dense ? 1 : 0.9,
                    categoryPercentage: dense ? 1 : 0.8,
                });
            }
            if (flood.length) {
                datasets.push({
                    label: 'Flood packets (' + (packetWindowLabel || 'retained') + ')',
                    data: floodShare,
                    counts: flood.map((b) => b[1]),
                    unit: 'packets',
                    backgroundColor: COLOR.flood,
                    borderRadius: dense ? 0 : 3,
                    barPercentage: dense ? 1 : 0.9,
                    categoryPercentage: dense ? 1 : 0.8,
                });
            }

            this.charts[canvasId] = new Chart(canvas.getContext('2d'), {
                type: 'bar',
                data: { labels, datasets },
                options: noAnimation({
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: colors.muted, boxWidth: 12, font: { size: 10 } },
                        },
                        tooltip: {
                            callbacks: {
                                title: (items) => items[0].label + ' hops away',
                                label: (ctx) => {
                                    const count = ctx.dataset.counts[ctx.dataIndex];
                                    if (count === null || count === undefined) return null;
                                    return ctx.dataset.label + ': ' + formatNumber(count) + ' ' +
                                        ctx.dataset.unit + ' (' + ctx.parsed.y.toFixed(2) + '%)';
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            title: { display: true, text: 'Hops away', color: colors.muted,
                                     font: { size: 10 } },
                            ticks: {
                                color: colors.muted,
                                font: { size: 10 },
                                autoSkip: true,
                                maxTicksLimit: 12,
                                maxRotation: 0,
                            },
                            grid: { display: false },
                        },
                        y: {
                            beginAtZero: true,
                            title: { display: true, text: '% of series', color: colors.muted,
                                     font: { size: 10 } },
                            ticks: {
                                color: colors.muted,
                                font: { size: 10 },
                                callback: (v) => v + '%',
                            },
                            grid: { color: colors.grid },
                        },
                    },
                }),
            });
        }

        renderDoughnut(canvasId, summaryId, encoding, noun) {
            const canvas = el(canvasId);
            const summary = el(summaryId);
            if (!canvas || typeof Chart === 'undefined') return;

            const total = Number((encoding || {}).total) || 0;
            let multibyte = Number((encoding || {}).multibyte);
            if (!Number.isFinite(multibyte)) multibyte = 0;
            multibyte = Math.max(0, Math.min(multibyte, total));
            const colors = themeColors();

            if (this.charts[canvasId]) {
                this.charts[canvasId].destroy();
                delete this.charts[canvasId];
            }

            if (total === 0) {
                if (summary) summary.textContent = 'No ' + noun + ' in this window.';
                this.charts[canvasId] = new Chart(canvas.getContext('2d'), {
                    type: 'doughnut',
                    data: {
                        labels: ['—'],
                        datasets: [{ data: [1], backgroundColor: [colors.empty], borderWidth: 0 }],
                    },
                    options: noAnimation({
                        responsive: true,
                        maintainAspectRatio: true,
                        plugins: { legend: { display: false }, tooltip: { enabled: false } },
                        cutout: '55%',
                    }),
                });
                return;
            }

            const pct = Math.round((multibyte / total) * 1000) / 10;
            if (summary) {
                summary.textContent = pct + '% multibyte (' +
                    formatNumber(multibyte) + ' of ' + formatNumber(total) + ')';
            }

            this.charts[canvasId] = new Chart(canvas.getContext('2d'), {
                type: 'doughnut',
                data: {
                    labels: ['Multibyte paths', 'Other'],
                    datasets: [{
                        data: [multibyte, total - multibyte],
                        backgroundColor: [COLOR.multibyte, COLOR.singleByte],
                        borderWidth: 0,
                    }],
                },
                options: noAnimation({
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: colors.muted, boxWidth: 12, font: { size: 11 } },
                        },
                        tooltip: {
                            callbacks: {
                                label: (ctx) => {
                                    const n = ctx.raw;
                                    const p = total ? Math.round((n / total) * 1000) / 10 : 0;
                                    return ' ' + formatNumber(n) + ' (' + p + '%)';
                                },
                            },
                        },
                    },
                    cutout: '55%',
                }),
            });
        }

        /**
         * One bar per day.  The bar's full height is the share of that day's
         * packets that took a multibyte path, and it is divided by the payload
         * type that carried them — so a day where 24% of traffic went multibyte
         * draws a bar 24% tall, split eight ways.
         *
         * Every segment is a fraction of the same denominator, the day's whole
         * traffic.  That is what makes the stack legitimate: each type's *own*
         * multibyte share is a ratio over its own packet count, and eight such
         * ratios share no denominator, so stacking those would sum to whatever
         * it happened to sum to.  The per-type adoption figure is not lost —
         * the tooltip quotes both readings.
         */
        renderPacketEncodingTrend(days) {
            const canvasId = 'multibyteTrendChart';
            const canvas = el(canvasId);
            if (!canvas || typeof Chart === 'undefined') return;
            const list = Array.isArray(days) ? days : [];
            const colors = themeColors();
            const palette = SERIES_PALETTE[isDark() ? 'dark' : 'light'];

            if (this.charts[canvasId]) {
                this.charts[canvasId].destroy();
                delete this.charts[canvasId];
            }

            const counts = (day, name) => (day.types || {})[name] || null;
            // The denominator is every packet the day classified, including the
            // OTHER bucket and every single-byte packet — not just the
            // multibyte ones, which is what makes bar height a share of traffic.
            const dayPackets = list.map((day) => Object.values(day.types || {}).reduce(
                (sum, entry) => sum + ((entry && entry.total) || 0), 0
            ));
            const dayMultibyte = list.map((day) => Object.values(day.types || {}).reduce(
                (sum, entry) => sum + ((entry && entry.mb) || 0), 0
            ));
            const segment = (day, name, i) => {
                const entry = counts(day, name);
                if (!entry || !dayPackets[i]) return null;
                return (entry.mb / dayPackets[i]) * 100;
            };
            const barHeights = list.map((day, i) => PACKET_ENCODING_TYPES.reduce(
                (sum, name) => sum + (segment(day, name, i) || 0), 0
            ));

            // Round the tallest bar up to the next step so the bars fill the
            // plot: a 24% peak against a 0-100 axis wastes three quarters of the
            // card and flattens every difference worth seeing.
            const peak = barHeights.length ? Math.max(...barHeights) : 0;
            const axisMax = Math.min(100, Math.max(
                AXIS_STEP_PCT,
                // Nudge down before rounding so a peak landing exactly on a step
                // keeps that step rather than jumping to the next one.
                Math.ceil((peak - 1e-9) / AXIS_STEP_PCT) * AXIS_STEP_PCT
            ));

            const datasets = [];
            PACKET_ENCODING_TYPES.forEach((name, index) => {
                // A type that carried no multibyte traffic all window would take
                // a legend slot to describe an invisible segment.
                if (!list.some((day, i) => segment(day, name, i) > 0)) return;
                const color = palette[index % palette.length];
                datasets.push({
                    label: name === 'OTHER' ? 'Other' : name,
                    data: list.map((day, i) => segment(day, name, i)),
                    // Raw figures ride along so the tooltip can quote packet
                    // counts and each type's own adoption without a second fetch.
                    rawCounts: list.map((day) => counts(day, name)),
                    backgroundColor: color,
                    // A hairline of the card colour between segments; without it
                    // two adjacent hues read as one band.  Dropped on segments
                    // too thin to separate — GRP_DATA runs well under 1% of a
                    // day, and a border on a 1px band erases the colour
                    // entirely, turning a real category into a surface-coloured
                    // gap.  Measured against the axis, not the value, because
                    // the axis is what decides how many pixels a percent buys.
                    borderColor: colors.surface,
                    borderWidth: (ctx) => (
                        (ctx.raw ?? 0) / axisMax >= MIN_SEGMENT_FRACTION_FOR_GAP
                            ? { top: 1, bottom: 1 }
                            : 0
                    ),
                    borderSkipped: false,
                    // Now that the tooltip describes one segment, something has
                    // to say which.  Chart.js would answer by saturating the
                    // fill to a hue outside the validated palette; outline it
                    // instead, so identity keeps its colour and the cue reads as
                    // a pointer.  It also gives the sub-pixel slices a mark big
                    // enough to see once they are the ones being described.
                    hoverBackgroundColor: color,
                    hoverBorderColor: colors.text,
                    hoverBorderWidth: 1,
                });
            });

            const empty = el('multibyte-trend-empty');
            const hasData = datasets.length > 0 && barHeights.some((height) => height > 0);
            if (empty) empty.style.display = hasData ? 'none' : '';
            canvas.style.display = hasData ? '' : 'none';
            if (!hasData) return;

            this.charts[canvasId] = new Chart(canvas.getContext('2d'), {
                type: 'bar',
                data: { labels: list.map((day) => day.date), datasets },
                options: noAnimation({
                    responsive: true,
                    maintainAspectRatio: false,
                    // One segment at a time.  'nearest' rather than 'point'
                    // because intersect would make the thin slices unhittable —
                    // GRP_DATA is under a pixel tall on most days, so requiring
                    // the cursor to land inside it hides the very rows a reader
                    // most needs the tooltip for.  This way the column picks the
                    // segment whose band the cursor is closest to.
                    interaction: { mode: 'nearest', intersect: false, axis: 'xy' },
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: {
                                color: colors.muted,
                                boxWidth: 10,
                                boxHeight: 10,
                                padding: 8,
                                font: { size: 10 },
                                usePointStyle: true,
                                pointStyle: 'rect',
                            },
                        },
                        tooltip: {
                            filter: (ctx) => ctx.parsed.y !== null && ctx.parsed.y > 0,
                            callbacks: {
                                // Room to spell both readings out now that only
                                // the hovered type is described.
                                label: (ctx) => {
                                    const entry = (ctx.dataset.rawCounts || [])[ctx.dataIndex];
                                    const name = ctx.dataset.label;
                                    const slice = ctx.parsed.y.toFixed(1) + '% of the day\'s packets';
                                    if (!entry) return [' ' + name, ' ' + slice];
                                    const adoption = entry.total
                                        ? (entry.mb / entry.total * 100).toFixed(1) +
                                          '% of all ' + name
                                        : 'no ' + name + ' traffic';
                                    return [
                                        ' ' + name + ' — ' + slice,
                                        ' ' + formatNumber(entry.mb) + ' of ' +
                                            formatNumber(entry.total) + ' packets · ' + adoption,
                                    ];
                                },
                                // The bar's own height, so the segment reads
                                // against the whole it is part of.
                                footer: (items) => {
                                    if (!items.length) return '';
                                    const i = items[0].dataIndex;
                                    return 'Whole bar: ' + barHeights[i].toFixed(1) +
                                        '% multibyte (' + formatNumber(dayMultibyte[i]) +
                                        ' of ' + formatNumber(dayPackets[i]) + ')';
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            stacked: true,
                            ticks: { color: colors.muted, font: { size: 10 }, maxTicksLimit: 8 },
                            grid: { display: false },
                        },
                        y: {
                            stacked: true,
                            min: 0,
                            max: axisMax,
                            ticks: {
                                // Gridlines on the same step the ceiling is
                                // rounded to, so every label is a round number
                                // and the top one is the ceiling itself.
                                stepSize: axisMax <= 40 ? AXIS_STEP_PCT : undefined,
                                maxTicksLimit: 8,
                                color: colors.muted,
                                font: { size: 10 },
                                callback: (v) => v + '%',
                            },
                            grid: { color: colors.grid },
                        },
                    },
                }),
            });
        }

        renderMix(containerId, entries) {
            const container = el(containerId);
            if (!container) return;
            const list = Array.isArray(entries) ? entries : [];
            if (list.length === 0) {
                container.replaceChildren(makeTextElement('div', 'No data.', 'dashboard-empty'));
                return;
            }
            // No slicing here: the server already rolls the tail into "Other",
            // so dropping rows now would leave bars that no longer sum to the
            // total shown beside them.
            const max = Math.max(...list.map((entry) => Number(entry[1]) || 0), 1);
            container.replaceChildren();
            list.forEach((entry, index) => {
                const [name, count] = entry;
                const row = document.createElement('div');
                row.className = 'mix-row';
                const track = document.createElement('div');
                track.className = 'mix-row__track';
                const fill = document.createElement('div');
                fill.className = 'mix-row__fill';
                fill.style.width = ((Number(count) || 0) / max * 100).toFixed(1) + '%';
                fill.style.backgroundColor = MIX_COLORS[index % MIX_COLORS.length];
                track.appendChild(fill);
                row.append(
                    makeTextElement('div', name, 'mix-row__name'),
                    track,
                    makeTextElement('div', formatNumber(count), 'mix-row__count')
                );
                container.appendChild(row);
            });
        }

        /** Say plainly which sources are missing rather than showing zeros. */
        renderSourceNotes(coverage) {
            const present = new Set(coverage.sources_present || []);
            const note = el('bot-source-note');
            if (!note) return;
            const missing = ['message_stats', 'command_stats', 'path_stats']
                .filter((name) => !present.has(name));
            if (missing.length === 0) {
                note.textContent = '';
                note.style.display = 'none';
                return;
            }
            note.style.display = '';
            note.textContent =
                'Bot statistics are unavailable: ' + missing.join(', ') +
                ' not present (set collect_stats = true in [Stats_Command] to record them).';
        }

        updateSnapshotAge() {
            if (this.lastGeneratedAt === null) return;
            const age = Date.now() / 1000 - this.lastGeneratedAt;
            setText('health-snapshot', 'snapshot ' + formatAge(age));
        }

        tickUptime() {
            if (this.botUptime === null) return;
            this.botUptime += 1;
            setText('health-uptime', formatUptime(this.botUptime));
        }

        setStatusDot(id, state) {
            const node = el(id);
            if (node) node.className = 'status-indicator status-' + state;
        }

        setDatabaseStatus(ok) {
            const node = el('health-db');
            if (!node) return;
            node.textContent = ok
                ? 'DB ok' + (BOOT.database_size ? ' · ' + formatBytes(BOOT.database_size) : '')
                : 'DB error';
            node.className = 'health-strip__item' + (ok ? '' : ' text-danger');
        }

        // -- interaction --------------------------------------------------

        wireSelectors() {
            Object.entries(TOP_LISTS).forEach(([kind, config]) => {
                const select = el(config.select);
                if (select) select.addEventListener('change', () => this.loadTop(kind));
            });
        }

        wireRefreshButton() {
            const button = el('snapshot-refresh');
            if (!button) return;
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    await fetch('/api/dashboard/refresh', {
                        method: 'POST',
                        headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    });
                    this.summaryEtag = null;
                    await this.loadSummary();
                } catch (err) {
                    console.error('Manual refresh failed', err);
                } finally {
                    button.disabled = false;
                }
            });
        }
    }

    // ── leaderboard configuration ────────────────────────────────────────

    /**
     * Link-quality colour for an SNR value. The boundaries are LoRa-practical
     * rather than arbitrary: below 0 dB the signal is under the noise floor and
     * only spreading recovers it, and below about -7 dB even that starts to
     * fail at the higher spreading factors.
     */
    function snrColor(snr) {
        if (snr === null || snr === undefined) return '#adb5bd';
        if (snr < -7) return '#dc3545';
        if (snr < 0) return '#fd7e14';
        if (snr < 6) return '#ffc107';
        return '#198754';
    }

    /**
     * One direct neighbour: name, SNR bar on a fixed scale, values.
     *
     * Signal is shown when a zero-hop advert or neighbor-discover measurement
     * is stored for this node. Otherwise the row says "no signal reading".
     */
    function neighborRow(item) {
        const row = document.createElement('div');
        row.className = 'neighbor-row';

        const name = makeTextElement('div', item.name, 'neighbor-row__name');
        name.title = String(item.name ?? '') + ' (' + String(item.role ?? '') + ')';

        const track = document.createElement('div');
        track.className = 'neighbor-row__track';
        const values = document.createElement('div');
        values.className = 'neighbor-row__values';

        if (item.signal_corroborated) {
            // Fixed -12..+14 dB scale so bars are comparable between refreshes
            // rather than rescaling to whatever the current worst link happens to be.
            const fill = document.createElement('div');
            fill.className = 'neighbor-row__fill';
            fill.style.width = (Math.max(0, Math.min(1, (Number(item.snr) + 12) / 26)) * 100)
                .toFixed(1) + '%';
            fill.style.backgroundColor = snrColor(item.snr);
            track.appendChild(fill);
            values.append(
                makeTextElement('span', formatSigned(item.snr) + ' dB', 'neighbor-row__snr'),
                makeTextElement('span',
                    item.rssi === null || item.rssi === undefined
                        ? ''
                        : ' / ' + Math.round(item.rssi) + ' dBm',
                    'neighbor-row__rssi')
            );
        } else {
            track.classList.add('neighbor-row__track--unknown');
            const unknown = makeTextElement('span', 'no signal reading', 'neighbor-row__rssi');
            unknown.title = 'Heard directly, but no SNR/RSSI measurement is stored for this node.';
            values.appendChild(unknown);
        }

        row.append(name, track, values);
        return row;
    }

    function rankedRow(rank, name, badgeText, badgeClass) {
        const row = document.createElement('div');
        row.className = 'top-list-row';
        const identity = document.createElement('div');
        identity.className = 'd-flex align-items-center gap-2 top-list-row__name';
        const label = makeTextElement('span', name, 'fw-semibold top-list-row__name');
        label.title = String(name ?? '');
        identity.append(makeBadge(rank, 'bg-secondary'), label);
        row.append(identity, makeBadge(badgeText, badgeClass));
        return row;
    }

    const TOP_LISTS = {
        commands: {
            select: 'top-commands-window',
            container: 'top-commands-list',
            limit: 15,
            empty: 'No commands recorded.',
            render: (item, i) => rankedRow(i + 1, item.name, formatNumber(item.count) + ' uses', 'bg-primary'),
        },
        users: {
            select: 'top-users-window',
            container: 'top-users-list',
            limit: 15,
            empty: 'No users recorded.',
            render: (item, i) => rankedRow(i + 1, item.name, formatNumber(item.count) + ' msgs', 'bg-info'),
        },
        channels: {
            select: 'top-channels-window',
            container: 'top-channels-list',
            limit: 10,
            empty: 'No channels recorded.',
            render: (item, i) => {
                const row = rankedRow(i + 1, item.name, formatNumber(item.count) + ' msgs', 'bg-secondary');
                row.appendChild(makeBadge(formatNumber(item.users) + ' users', 'bg-info'));
                return row;
            },
        },
        paths: {
            select: 'top-paths-window',
            container: 'top-paths-list',
            limit: 5,
            empty: 'No paths recorded.',
            render: (item, i) => {
                const card = document.createElement('div');
                card.className = 'mb-2 pb-2 border-bottom';
                const heading = document.createElement('div');
                heading.className = 'd-flex align-items-center gap-2 mb-1';
                const name = makeTextElement('span', item.name, 'fw-semibold top-list-row__name');
                name.title = String(item.name ?? '');
                heading.append(makeBadge(i + 1, 'bg-warning text-dark'), name,
                    makeBadge(item.path_length + ' hops', 'bg-danger'));
                const path = makeTextElement('code', item.path_string, 'small text-break');
                path.style.wordBreak = 'break-all';
                const when = makeTextElement(
                    'div',
                    item.timestamp ? new Date(Number(item.timestamp) * 1000).toLocaleString() : '',
                    'dashboard-note'
                );
                card.append(heading, path, when);
                return card;
            },
        },
        repeaters: {
            select: 'top-repeaters-window',
            container: 'top-repeaters-list',
            limit: 10,
            empty: 'No repeater adverts recorded.',
            render: (item, i) => rankedRow(i + 1, item.name, formatNumber(item.count) + ' adverts', 'bg-success'),
        },
        neighbors: {
            select: 'neighbors-window',
            container: 'neighbors-list',
            limit: 10,
            empty: 'No direct neighbours heard in this window.',
            render: (item) => neighborRow(item),
            onLoad: (data) => {
                setText('neighbors-count', formatNumber(data.total));
                const shown = Math.min(data.total || 0, (data.items || []).length);
                setText('neighbors-shown',
                    data.total > shown ? 'showing ' + shown + ' of ' + formatNumber(data.total) : '');
            },
        },
    };

    // ── connected clients modal ──────────────────────────────────────────

    async function loadConnectedClients() {
        const loading = el('connected-clients-loading');
        const empty = el('connected-clients-empty');
        const table = el('connected-clients-table');
        const tbody = el('connected-clients-tbody');
        if (!loading) return;
        loading.style.display = '';
        empty.style.display = 'none';
        table.style.display = 'none';
        tbody.replaceChildren();
        try {
            const response = await fetch('/api/connected_clients');
            const clients = await response.json();
            loading.style.display = 'none';
            if (!Array.isArray(clients) || clients.length === 0) {
                empty.style.display = '';
                return;
            }
            table.style.display = '';
            clients.forEach((client) => {
                const row = document.createElement('tr');
                const idCell = document.createElement('td');
                idCell.appendChild(makeTextElement('code', client.client_id));
                row.append(
                    idCell,
                    makeTextElement('td', client.connected_at
                        ? new Date(client.connected_at * 1000).toLocaleTimeString() : '—'),
                    makeTextElement('td', client.last_activity
                        ? new Date(client.last_activity * 1000).toLocaleTimeString() : '—')
                );
                tbody.appendChild(row);
            });
        } catch (err) {
            loading.style.display = 'none';
            empty.style.display = '';
            empty.textContent = 'Failed to load client list.';
        }
    }

    // ── tooltips ─────────────────────────────────────────────────────────

    const TOOLTIPS = {
        'contacts-encoding-info':
            'Contacts heard in the last 7 days count as multibyte capable if any apply: ' +
            'their stored path encoding (out bytes per hop) is 2 or 3; we saw an advert from ' +
            'them in that window on a path with multibyte hops; or, for repeaters and room ' +
            'servers, their public key prefix matches a hop prefix from a multibyte advert path.',
        'multibyte-trend-info':
            'Each bar\'s height is the share of that day\'s packets that arrived on a path ' +
            'with 2- or 3-byte hop hashes, divided by the payload type that carried them. A ' +
            'segment is that type\'s share of the whole day\'s traffic, not of its own ' +
            'packets — hover for both figures. The axis tops out at the tallest bar rounded ' +
            'up to the next 5%, so it is not comparable between screenshots taken on ' +
            'different days. Counted from the packet stream as each day is rolled up: the ' +
            'stream itself is pruned within days, so the chart accumulates forward and days ' +
            'before it existed are blank rather than zero. Packets whose encoding has not ' +
            'been classified yet are left out rather than counted as single-byte.',
        'delta-info':
            'Change compares the last complete calendar day against the day before it — ' +
            'not a rolling 24 hours. The headline number above it is a rolling 24-hour count.',
        'neighbors-info':
            'Radios this node heard directly (MeshCore hop count 0: an empty RF path). ' +
            'A path that already contains one hop hash is a relayed packet, not a neighbour. ' +
            'Membership comes from those empty-path adverts plus confirmed zero-hop ' +
            'neighbor-discover responses, not from the stored hop count — that field claims ' +
            'far more direct neighbours than the path evidence supports. SNR/RSSI are the ' +
            'measurement from that direct reception (or from discover, which reports SNR ' +
            'only). A relayed packet\'s SNR measures the last hop into this radio rather ' +
            'than the link to whoever sent it.',
        'hops-info':
            'Two distributions on one axis. Nodes: the fewest hops any of a node\'s adverts ' +
            'took to reach this radio, over 7 days. Flood packets: how far each flood packet ' +
            'had already travelled when it arrived, over whatever the packet stream retains ' +
            '(typically 3 days). Because one counts nodes and the other counts packets, both ' +
            'are drawn as a share of their own total; the tooltip gives the raw counts. ' +
            'Hop buckets holding under 0.1% of the flood series are omitted — that tail ' +
            'decays for around twenty hops in bars under a pixel tall — and the amount left ' +
            'out is stated beneath the chart. Percentages stay shares of the full series, ' +
            'not of the drawn subset. Flood packets carry no sender identity, so they cannot ' +
            'be reduced to a shortest path per node the way adverts can.',
    };

    function initTooltips() {
        if (typeof bootstrap === 'undefined' || !bootstrap.Tooltip) return;
        Object.entries(TOOLTIPS).forEach(([id, title]) => {
            const node = el(id);
            if (!node) return;
            const existing = bootstrap.Tooltip.getInstance(node);
            if (existing) existing.dispose();
            new bootstrap.Tooltip(node, { title, html: false, customClass: 'dashboard-hint' });
        });
    }

    // ── bootstrap ────────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', () => {
        window.dashboard = new ModernDashboard();
        el('health-clients-link')?.addEventListener('click', (event) => {
            event.preventDefault();
            loadConnectedClients();
        });
        initTooltips();
    });

    window.addEventListener('beforeunload', () => {
        if (window.dashboard) window.dashboard.destroy();
    });
})();
