/* Backtest Dashboard — MarketLens */

const RUN_COLORS = ['#2A7A7A', '#C44B28', '#5B6ABF', '#B8860B', '#6B4C8A'];
const TEAL = '#2A7A7A';
const TEAL_SOFT = 'rgba(42,122,122,0.12)';
const VERMILLION = '#C44B28';
const VERMILLION_SOFT = 'rgba(196,75,40,0.12)';
const GRID_COLOR = '#EDE9E0';
const AXIS_COLOR = '#DBD5C9';
const BG_TRANSPARENT = 'rgba(0,0,0,0)';

const PLOTLY_LAYOUT = {
    paper_bgcolor: BG_TRANSPARENT,
    plot_bgcolor: BG_TRANSPARENT,
    font: { family: 'Inter, sans-serif', size: 12, color: '#1B1915' },
    margin: { l: 60, r: 16, t: 8, b: 40 },
    xaxis: { gridcolor: GRID_COLOR, linecolor: AXIS_COLOR, zeroline: false },
    yaxis: { gridcolor: GRID_COLOR, linecolor: AXIS_COLOR, zeroline: false },
    hovermode: 'x unified',
    showlegend: false,
    dragmode: 'zoom',
};

const PLOTLY_CFG = { displayModeBar: false, responsive: true };

let DATA = null;
let visibleRuns = [];

const SETTLEMENTS_PAGE_SIZE = 15;
let settlementsExpanded = false;

// ── Formatting helpers ────────────────────────────────────

function fmtMoney(v) {
    if (v == null) return '---';
    const sign = v >= 0 ? '' : '-';
    return sign + '$' + Math.abs(v).toFixed(2);
}

function fmtPct(v) {
    if (v == null) return '---';
    return (v * 100).toFixed(2) + '%';
}

function fmtRatio(v) {
    if (v == null) return '---';
    return v.toFixed(2);
}

function fmtInt(v) {
    if (v == null) return '---';
    return v.toLocaleString();
}

function msToDate(ms) { return new Date(ms); }

function signClass(v) {
    if (v == null) return '';
    return v > 0 ? 'positive' : v < 0 ? 'negative' : '';
}

function truncId(id) {
    if (!id) return '---';
    if (id.length <= 16) return id;
    return id.slice(0, 8) + '…' + id.slice(-6);
}

function marketLabel(id, run) {
    const names = run && run.market_names || {};
    const name = names[id];
    if (name) {
        return name.length > 24 ? name.slice(0, 22) + '…' : name;
    }
    return truncId(id);
}

// ── Metric groups ─────────────────────────────────────────

const METRIC_GROUPS = [
    {
        title: 'Returns',
        metrics: [
            { key: 'total_pnl', label: 'PnL', fmt: fmtMoney, signed: true },
            { key: 'total_return', label: 'Return', fmt: fmtPct, signed: true },
            { key: 'expectancy', label: 'Expectancy', fmt: fmtMoney, signed: true },
        ],
    },
    {
        title: 'Risk',
        metrics: [
            { key: 'sharpe_ratio', label: 'Sharpe', fmt: fmtRatio },
            { key: 'sortino_ratio', label: 'Sortino', fmt: fmtRatio },
            { key: 'max_drawdown', label: 'Max DD', fmt: v => fmtPct(v != null ? -Math.abs(v) : null), signed: true, invertSign: true },
        ],
    },
    {
        title: 'Trading',
        metrics: [
            { key: 'win_rate', label: 'Win Rate', fmt: fmtPct },
            { key: 'profit_factor', label: 'Profit Factor', fmt: fmtRatio },
            { key: 'total_trades', label: 'Trades', fmt: fmtInt },
        ],
    },
];

// ── Init ──────────────────────────────────────────────────

async function init() {
    try {
        const resp = await fetch('/api/data');
        DATA = await resp.json();
    } catch (e) {
        document.getElementById('app').innerHTML =
            '<div id="loading">Failed to load data</div>';
        return;
    }

    visibleRuns = DATA.runs.map((_, i) => i);

    const app = document.getElementById('app');
    const tpl = document.getElementById('tpl-main');
    app.innerHTML = '';
    app.appendChild(tpl.content.cloneNode(true));

    renderHeader();
    renderAll();
}

function renderAll() {
    const runs = visibleRuns.map(i => DATA.runs[i]);
    renderKPIs(runs);
    renderEquityCurve(runs);
    renderDrawdown(runs);
    renderPnLByMarket(runs);
    renderPnLDist(runs);
    renderTradeTimeline(runs);
    renderOrderAnalysis(runs);
    settlementsExpanded = false;
    renderSettlements(runs);
    renderConfig(runs);
}

// ── Header ────────────────────────────────────────────────

function renderHeader() {
    const isCompare = DATA.runs.length > 1;

    if (DATA.title) {
        document.querySelector('.header-title').textContent = DATA.title;
        document.title = DATA.title + ' — MarketLens';
    }

    const info = document.getElementById('run-info');
    if (!isCompare) {
        info.textContent = DATA.runs[0].label;
    } else {
        info.textContent = DATA.runs.length + ' runs';
    }

    const sel = document.getElementById('run-selector');
    if (!isCompare) { sel.innerHTML = ''; return; }

    sel.innerHTML = DATA.runs.map((run, i) => {
        const checked = visibleRuns.includes(i) ? 'checked' : '';
        const muted = visibleRuns.includes(i) ? '' : ' muted';
        return `<label class="run-toggle${muted}" data-idx="${i}">
            <input type="checkbox" ${checked}>
            <span class="run-dot" style="background:${RUN_COLORS[i % RUN_COLORS.length]}"></span>
            ${run.label}
        </label>`;
    }).join('');

    sel.querySelectorAll('input[type=checkbox]').forEach(cb => {
        cb.addEventListener('change', () => {
            const idx = parseInt(cb.closest('.run-toggle').dataset.idx);
            if (cb.checked) {
                if (!visibleRuns.includes(idx)) visibleRuns.push(idx);
            } else {
                visibleRuns = visibleRuns.filter(i => i !== idx);
            }
            visibleRuns.sort((a, b) => a - b);
            sel.querySelectorAll('.run-toggle').forEach(t => {
                const ti = parseInt(t.dataset.idx);
                t.classList.toggle('muted', !visibleRuns.includes(ti));
            });
            renderAll();
        });
    });
}

// ── KPI Groups (Returns / Risk / Trading) ─────────────────

const LOWER_IS_BETTER = new Set(['max_drawdown']);

function bestRunIndex(key, runs) {
    let bestIdx = 0;
    let bestVal = runs[0].metrics[key];
    const lowerBetter = LOWER_IS_BETTER.has(key);
    let tied = true;
    for (let i = 1; i < runs.length; i++) {
        const v = runs[i].metrics[key];
        if (v == null) continue;
        if (bestVal == null || (lowerBetter ? Math.abs(v) < Math.abs(bestVal) : v > bestVal)) {
            bestIdx = i;
            bestVal = v;
            tied = false;
        } else if (lowerBetter ? Math.abs(v) !== Math.abs(bestVal) : v !== bestVal) {
            tied = false;
        }
    }
    if (tied || bestVal == null) return -1;
    return bestIdx;
}

function renderKPIs(runs) {
    const el = document.getElementById('kpi-groups');
    const isCompare = runs.length > 1;

    el.innerHTML = METRIC_GROUPS.map(group => {
        const rows = group.metrics.map(d => {
            if (!isCompare) {
                const v = runs[0].metrics[d.key];
                const formatted = d.fmt(v);
                let sc = '';
                if (d.signed) {
                    const testVal = d.invertSign ? -(v ?? 0) : (v ?? 0);
                    sc = signClass(testVal);
                }
                return `<div class="kpi-row">
                    <span class="kpi-row-label">${d.label}</span>
                    <span class="kpi-row-value ${sc}">${formatted}</span>
                </div>`;
            }
            const best = bestRunIndex(d.key, runs);
            const values = runs.map((run, ri) => {
                const v = run.metrics[d.key];
                const formatted = d.fmt(v);
                const color = RUN_COLORS[DATA.runs.indexOf(run) % RUN_COLORS.length];
                const isBest = ri === best;
                const cls = isBest ? 'value best' : 'value';
                return `<span class="kpi-run-line">
                    <span class="run-dot" style="background:${color}"></span>
                    <span class="${cls}">${formatted}</span>
                </span>`;
            }).join('');
            return `<div class="kpi-row">
                <span class="kpi-row-label">${d.label}</span>
                <span class="kpi-row-values">${values}</span>
            </div>`;
        }).join('');

        return `<div class="kpi-group">
            <div class="kpi-group-title">${group.title}</div>
            ${rows}
        </div>`;
    }).join('');
}

// ── Equity Curve ──────────────────────────────────────────

function renderEquityCurve(runs) {
    const el = document.getElementById('chart-equity');
    if (!runs.length || runs.every(r => !r.equity_curve.length)) {
        el.innerHTML = '<div class="no-data">No equity data</div>';
        return;
    }

    const traces = runs.map(run => {
        const x = run.equity_curve.map(p => msToDate(p.t));
        const y = run.equity_curve.map(p => p.equity);
        const idx = DATA.runs.indexOf(run);
        const color = RUN_COLORS[idx % RUN_COLORS.length];
        const trace = {
            x, y,
            type: 'scatter', mode: 'lines',
            name: run.label,
            line: { color, width: 2 },
        };
        if (runs.length === 1) {
            trace.fill = 'tozeroy';
            trace.fillcolor = TEAL_SOFT;
        }
        return trace;
    });

    // Scale y-axis to data range + 5% padding
    const allEquity = runs.flatMap(r => r.equity_curve.map(p => p.equity));
    const yMin = Math.min(...allEquity);
    const yMax = Math.max(...allEquity);
    const yPad = (yMax - yMin) * 0.05 || 1;

    const layout = {
        ...PLOTLY_LAYOUT,
        showlegend: false,
        yaxis: { ...PLOTLY_LAYOUT.yaxis, tickprefix: '$', range: [yMin - yPad, yMax + yPad] },
    };

    Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
}

// ── Drawdown ──────────────────────────────────────────────

function renderDrawdown(runs) {
    const el = document.getElementById('chart-drawdown');
    if (!runs.length || runs.every(r => !r.drawdown_curve.length)) {
        el.innerHTML = '<div class="no-data">No drawdown data</div>';
        return;
    }

    const traces = runs.map(run => {
        const x = run.drawdown_curve.map(p => msToDate(p.t));
        const y = run.drawdown_curve.map(p => p.drawdown * 100);
        const idx = DATA.runs.indexOf(run);
        const color = RUN_COLORS[idx % RUN_COLORS.length];
        return {
            x, y,
            type: 'scatter', mode: 'lines',
            name: run.label,
            line: { color, width: 1.5 },
            fill: 'tozeroy',
            fillcolor: runs.length === 1 ? VERMILLION_SOFT : hexToRgba(color, 0.08),
        };
    });

    const layout = {
        ...PLOTLY_LAYOUT,
        showlegend: false,
        yaxis: { ...PLOTLY_LAYOUT.yaxis, ticksuffix: '%' },
    };

    const allDD = runs.flatMap(r => r.drawdown_curve.map(p => p.drawdown * 100));
    const ddMin = Math.min(...allDD);
    const pad = Math.abs(ddMin) * 0.05 || 0.5;
    layout.yaxis.range = [ddMin - pad, pad];

    Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
}

// ── PnL by Market ─────────────────────────────────────────
// Top 3 by |net_pnl|, gains on top

function renderPnLByMarket(runs) {
    const el = document.getElementById('chart-pnl-market');
    if (!runs.length || runs.every(r => !r.pnl_by_market.length)) {
        el.innerHTML = '<div class="no-data">No market data</div>';
        return;
    }

    // Pick which markets to show: top 3 highest + top 3 lowest across all runs
    const byMarket = new Map();
    for (const run of runs) {
        for (const m of run.pnl_by_market) {
            const prev = byMarket.get(m.market_id);
            if (!prev || Math.abs(m.net_pnl) > Math.abs(prev.net_pnl)) {
                byMarket.set(m.market_id, m);
            }
        }
    }
    const all = [...byMarket.values()].sort((a, b) => b.net_pnl - a.net_pnl);
    const top3 = all.filter(m => m.net_pnl > 0).slice(0, 3);
    const bot3 = all.filter(m => m.net_pnl < 0).slice(-3);
    let selected = [...top3, ...bot3];
    if (!selected.length) selected = all.slice(0, 6);
    // Lowest first in array → highest renders at top of horizontal bar
    selected.sort((a, b) => a.net_pnl - b.net_pnl);
    const selectedIds = new Set(selected.map(m => m.market_id));

    // Build tick mapping
    const tickMap = new Map();
    for (const m of selected) {
        const full = m.name || truncId(m.market_id);
        tickMap.set(m.market_id, full.length > 24 ? full.slice(0, 22) + '…' : full);
    }

    const orderedIds = selected.map(m => m.market_id);

    const traces = runs.map(run => {
        const lookup = new Map(run.pnl_by_market.map(m => [m.market_id, m]));
        const markets = orderedIds.map(id => lookup.get(id)).filter(Boolean);
        if (!markets.length) return null;

        const y = markets.map(m => m.market_id);
        const fullNames = markets.map(m => m.name || m.market_id);
        const x = markets.map(m => m.net_pnl);
        const idx = DATA.runs.indexOf(run);

        if (runs.length === 1) {
            return {
                y, x,
                text: fullNames, textposition: 'none',
                type: 'bar', orientation: 'h',
                name: run.label,
                marker: { color: markets.map(m => m.net_pnl >= 0 ? TEAL : VERMILLION) },
                hovertemplate: '%{text}<br>Net PnL: $%{x:.2f}<extra></extra>',
            };
        }
        return {
            y, x,
            text: fullNames, textposition: 'none',
            type: 'bar', orientation: 'h',
            name: run.label,
            marker: { color: RUN_COLORS[idx % RUN_COLORS.length] },
            hovertemplate: '%{text}<br>Net PnL: $%{x:.2f}<extra>' + run.label + '</extra>',
        };
    }).filter(Boolean);

    if (!traces.length) {
        el.innerHTML = '<div class="no-data">No market data</div>';
        return;
    }

    const layout = {
        ...PLOTLY_LAYOUT,
        barmode: runs.length > 1 ? 'group' : undefined,
        showlegend: false,
        xaxis: {
            ...PLOTLY_LAYOUT.xaxis,
            type: 'linear',
            tickformat: '$,.2f',
            zeroline: true, zerolinecolor: AXIS_COLOR,
        },
        yaxis: {
            ...PLOTLY_LAYOUT.yaxis,
            type: 'category',
            automargin: true,
            tickvals: [...tickMap.keys()],
            ticktext: [...tickMap.values()],
            tickfont: { family: 'JetBrains Mono, monospace', size: 10 },
        },
        margin: { l: 10, r: 16, t: 8, b: 40 },
    };

    Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
}

// ── PnL Distribution ──────────────────────────────────────

function renderPnLDist(runs) {
    const el = document.getElementById('chart-pnl-dist');
    if (!runs.length || runs.every(r => !r.settlements.length)) {
        el.innerHTML = '<div class="no-data">No settlement data</div>';
        return;
    }

    let traces;
    if (runs.length === 1) {
        const pnls = runs[0].settlements.map(s => s.net_pnl);
        const pos = pnls.filter(p => p >= 0);
        const neg = pnls.filter(p => p < 0);
        traces = [];
        if (neg.length) traces.push({
            x: neg, type: 'histogram', name: 'Loss',
            marker: { color: VERMILLION_SOFT, line: { color: VERMILLION, width: 1 } },
            hovertemplate: 'PnL: $%{x:.2f}<br>Count: %{y}<extra></extra>',
        });
        if (pos.length) traces.push({
            x: pos, type: 'histogram', name: 'Win',
            marker: { color: TEAL_SOFT, line: { color: TEAL, width: 1 } },
            hovertemplate: 'PnL: $%{x:.2f}<br>Count: %{y}<extra></extra>',
        });
    } else {
        traces = runs.map(run => {
            const pnls = run.settlements.map(s => s.net_pnl);
            const idx = DATA.runs.indexOf(run);
            return {
                x: pnls, type: 'histogram', name: run.label, opacity: 0.65,
                marker: { color: RUN_COLORS[idx % RUN_COLORS.length] },
                hovertemplate: 'PnL: $%{x:.2f}<br>Count: %{y}<extra>' + run.label + '</extra>',
            };
        });
    }

    const layout = {
        ...PLOTLY_LAYOUT,
        barmode: 'overlay',
        showlegend: runs.length === 1,
        legend: { x: 1, y: 1, xanchor: 'right', bgcolor: BG_TRANSPARENT, font: { size: 11 } },
        xaxis: { ...PLOTLY_LAYOUT.xaxis, type: 'linear', tickformat: '$,.2f', autorange: true,
            rangemode: 'tozero', range: undefined },
        yaxis: { ...PLOTLY_LAYOUT.yaxis, rangemode: 'tozero' },
    };

    // Add ~10% padding to x-axis so bars don't touch edges
    const allPnls = runs.flatMap(r => r.settlements.map(s => s.net_pnl));
    if (allPnls.length) {
        const lo = Math.min(...allPnls);
        const hi = Math.max(...allPnls);
        const pad = (hi - lo) * 0.1 || 1;
        layout.xaxis.range = [lo - pad, hi + pad];
        layout.xaxis.autorange = false;
    }

    Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
}

// ── Trade Timeline ────────────────────────────────────────

function renderTradeTimeline(runs) {
    const el = document.getElementById('chart-trades');
    if (!runs.length || runs.every(r => !r.trades.length)) {
        el.innerHTML = '<div class="no-data">No trade data</div>';
        return;
    }

    const sideStyle = {
        'BUY_YES':  { color: TEAL,       symbol: 'circle',        name: 'Buy Yes' },
        'BUY_NO':   { color: '#5B6ABF',  symbol: 'square',        name: 'Buy No' },
        'SELL_YES': { color: VERMILLION,  symbol: 'diamond',       name: 'Sell Yes' },
        'SELL_NO':  { color: '#B8860B',   symbol: 'triangle-down', name: 'Sell No' },
    };

    const traces = [];

    if (runs.length === 1) {
        const bySide = {};
        for (const t of runs[0].trades) {
            if (!bySide[t.side]) bySide[t.side] = [];
            bySide[t.side].push(t);
        }
        for (const [side, trades] of Object.entries(bySide)) {
            const s = sideStyle[side] || { color: TEAL, symbol: 'circle', name: side };
            traces.push({
                x: trades.map(t => msToDate(t.t)),
                y: trades.map(t => t.price),
                text: trades.map(t => `${s.name}<br>Size: ${t.size.toFixed(2)}<br>Price: $${t.price.toFixed(4)}`),
                type: 'scatter', mode: 'markers',
                name: s.name,
                marker: {
                    color: s.color, symbol: s.symbol,
                    size: trades.map(t => Math.min(Math.max(Math.sqrt(t.size) * 5, 5), 18)),
                    opacity: 0.8,
                    line: { width: 1, color: 'rgba(255,255,255,0.6)' },
                },
                hovertemplate: '%{text}<extra></extra>',
            });
        }
    } else {
        const buySymbol = 'circle';
        const sellSymbol = 'diamond';
        for (const run of runs) {
            const idx = DATA.runs.indexOf(run);
            const color = RUN_COLORS[idx % RUN_COLORS.length];
            const buys = run.trades.filter(t => t.side.startsWith('BUY'));
            const sells = run.trades.filter(t => t.side.startsWith('SELL'));
            if (buys.length) {
                traces.push({
                    x: buys.map(t => msToDate(t.t)),
                    y: buys.map(t => t.price),
                    text: buys.map(t => `${run.label}<br>${t.side}<br>Size: ${t.size.toFixed(2)}<br>Price: $${t.price.toFixed(4)}`),
                    type: 'scatter', mode: 'markers',
                    name: run.label + ' Buy',
                    legendgroup: run.label,
                    marker: {
                        color, symbol: buySymbol,
                        size: buys.map(t => Math.min(Math.max(Math.sqrt(t.size) * 5, 5), 14)),
                        opacity: 0.8,
                        line: { width: 1, color: 'rgba(255,255,255,0.6)' },
                    },
                    hovertemplate: '%{text}<extra></extra>',
                });
            }
            if (sells.length) {
                traces.push({
                    x: sells.map(t => msToDate(t.t)),
                    y: sells.map(t => t.price),
                    text: sells.map(t => `${run.label}<br>${t.side}<br>Size: ${t.size.toFixed(2)}<br>Price: $${t.price.toFixed(4)}`),
                    type: 'scatter', mode: 'markers',
                    name: run.label + ' Sell',
                    legendgroup: run.label,
                    marker: {
                        color, symbol: sellSymbol,
                        size: sells.map(t => Math.min(Math.max(Math.sqrt(t.size) * 5, 5), 14)),
                        opacity: 0.8,
                        line: { width: 1, color: 'rgba(255,255,255,0.6)' },
                    },
                    hovertemplate: '%{text}<extra></extra>',
                });
            }
        }
    }

    const layout = {
        ...PLOTLY_LAYOUT,
        showlegend: runs.length === 1,
        hovermode: 'closest',
        legend: { x: 0, y: 1, bgcolor: BG_TRANSPARENT, font: { size: 11 } },
        yaxis: { ...PLOTLY_LAYOUT.yaxis, tickprefix: '$' },
    };

    Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
}

// ── Order Analysis ────────────────────────────────────────
// Pie chart (donut) with fill rate in center, works for both modes

function renderOrderAnalysis(runs) {
    const el = document.getElementById('chart-orders');
    if (!runs.length || runs.every(r => r.order_stats.total === 0)) {
        el.innerHTML = '<div class="no-data">No order data</div>';
        return;
    }

    const statusColors = {
        'FILLED': TEAL,
        'PARTIALLY_FILLED': '#5B6ABF',
        'CANCELLED': VERMILLION,
        'EXPIRED': '#9A9288',
        'OPEN': '#B8860B',
        'PENDING': '#DBD5C9',
    };
    // Short display names so labels fit inside the donut ring without clipping.
    const statusLabels = {
        'FILLED': 'Filled',
        'PARTIALLY_FILLED': 'Partial',
        'CANCELLED': 'Cancelled',
        'EXPIRED': 'Expired',
        'OPEN': 'Open',
        'PENDING': 'Pending',
    };
    const prettyStatus = s => statusLabels[s] || s;

    if (runs.length === 1) {
        const stats = runs[0].order_stats;
        const statuses = Object.entries(stats.by_status).sort((a, b) => b[1] - a[1]);

        const traces = [{
            labels: statuses.map(([s]) => prettyStatus(s)),
            values: statuses.map(([, v]) => v),
            type: 'pie',
            hole: 0.5,
            marker: { colors: statuses.map(([s]) => statusColors[s] || '#9A9288') },
            textinfo: statuses.length > 1 ? 'label+percent' : 'none',
            textposition: 'inside',
            insidetextorientation: 'horizontal',
            textfont: { family: 'JetBrains Mono, monospace', size: 10 },
            hovertemplate: '%{label}: %{value} (%{percent})<extra></extra>',
        }];

        const layout = {
            ...PLOTLY_LAYOUT,
            showlegend: false,
            annotations: [{
                x: 0.5, y: 0.5, xref: 'paper', yref: 'paper',
                text: `<b>${fmtPct(stats.fill_rate)}</b><br><span style="font-size:10px;color:#9A9288">Fill Rate</span>`,
                showarrow: false,
                font: { family: 'Inter', size: 18, color: '#1B1915' },
            }],
        };

        Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
    } else {
        // Comparison: one donut per run, side by side
        const n = runs.length;
        const traces = [];
        const annotations = [];

        runs.forEach((run, ri) => {
            const stats = run.order_stats;
            const statuses = Object.entries(stats.by_status).sort((a, b) => b[1] - a[1]);
            const xStart = ri / n;
            const xEnd = (ri + 1) / n;
            const xCenter = (xStart + xEnd) / 2;

            traces.push({
                labels: statuses.map(([s]) => prettyStatus(s)),
                values: statuses.map(([, v]) => v),
                type: 'pie',
                hole: 0.5,
                marker: { colors: statuses.map(([s]) => statusColors[s] || '#9A9288') },
                textinfo: statuses.length > 1 ? 'label+percent' : 'none',
                textposition: 'inside',
                insidetextorientation: 'horizontal',
                textfont: { family: 'JetBrains Mono, monospace', size: 9 },
                hovertemplate: '%{label}: %{value} (%{percent})<extra></extra>',
                domain: { x: [xStart + 0.02, xEnd - 0.02], y: [0, 1] },
                showlegend: ri === 0,
            });

            const idx = DATA.runs.indexOf(run);
            const domainCenter = (xStart + 0.02 + xEnd - 0.02) / 2;
            annotations.push({
                x: domainCenter, y: 0.5, xref: 'paper', yref: 'paper',
                xanchor: 'center',
                text: `<b>${fmtPct(stats.fill_rate)}</b>`,
                showarrow: false,
                font: { family: 'Inter', size: 14, color: '#1B1915' },
            });
            annotations.push({
                x: domainCenter, y: -0.08, xref: 'paper', yref: 'paper',
                xanchor: 'center',
                text: `<span style="color:${RUN_COLORS[idx % RUN_COLORS.length]}">${run.label}</span>`,
                showarrow: false,
                font: { family: 'JetBrains Mono', size: 10 },
            });
        });

        const layout = {
            ...PLOTLY_LAYOUT,
            showlegend: false,
            annotations,
            margin: { ...PLOTLY_LAYOUT.margin, b: 50 },
        };

        Plotly.newPlot(el, traces, layout, PLOTLY_CFG);
    }
}

// ── Settlements Table ─────────────────────────────────────
// Fewer columns, sortable, paged

let sortCol = 'net_pnl';
let sortAsc = false;

function renderSettlements(runs) {
    const el = document.getElementById('settlements-table');
    if (!runs.length || runs.every(r => !r.settlements.length)) {
        el.innerHTML = '<div class="no-data">No settlement data</div>';
        return;
    }

    const isCompare = runs.length > 1;
    const allRows = [];
    for (const run of runs) {
        for (const s of run.settlements) {
            allRows.push({ ...s, _label: run.label, _runIdx: DATA.runs.indexOf(run) });
        }
    }

    if (sortCol !== null) {
        allRows.sort((a, b) => {
            let va = a[sortCol], vb = b[sortCol];
            if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
            va = va ?? -Infinity; vb = vb ?? -Infinity;
            return sortAsc ? va - vb : vb - va;
        });
    }

    const cols = [
        ...(isCompare ? [{ key: '_label', label: 'Run' }] : []),
        { key: 'market_id', label: 'Market' },
        { key: 'side', label: 'Side' },
        { key: 'avg_entry_price', label: 'Entry', fmt: v => '$' + v.toFixed(4) },
        { key: 'settlement_price', label: 'Settlement', fmt: v => '$' + v.toFixed(2) },
        { key: 'net_pnl', label: 'Net PnL', fmt: fmtMoney, signed: true },
        { key: 'resolved_at', label: 'Resolved', fmt: v => v ? new Date(v).toLocaleDateString() : '---' },
    ];

    const showCount = settlementsExpanded ? allRows.length : Math.min(allRows.length, SETTLEMENTS_PAGE_SIZE);
    const hasMore = allRows.length > SETTLEMENTS_PAGE_SIZE && !settlementsExpanded;
    const visibleRows = allRows.slice(0, showCount);

    let html = '<table><thead><tr>';
    for (const col of cols) {
        const isSorted = sortCol === col.key;
        const arrow = isSorted ? (sortAsc ? '↑' : '↓') : '↕';
        const cls = isSorted ? ' class="sorted"' : '';
        html += `<th${cls} data-col="${col.key}">${col.label} <span class="sort-arrow">${arrow}</span></th>`;
    }
    html += '</tr></thead><tbody>';

    for (const row of visibleRows) {
        html += '<tr>';
        for (const col of cols) {
            let val = row[col.key];
            let display = col.fmt ? col.fmt(val) : (col.key === 'market_id' ? marketLabel(val, DATA.runs[row._runIdx]) : String(val ?? '---'));
            let titleAttr = '';
            if (col.key === 'market_id') {
                const names = DATA.runs[row._runIdx].market_names || {};
                const full = names[val] || val;
                titleAttr = ` title="${full.replace(/"/g, '&quot;')}"`;
            }
            let cls = '';
            if (col.signed && val != null) cls = val > 0 ? ' class="cell-positive"' : val < 0 ? ' class="cell-negative"' : '';
            if (col.key === '_label') {
                const dotColor = RUN_COLORS[row._runIdx % RUN_COLORS.length];
                display = `<span class="run-dot" style="background:${dotColor};display:inline-block;margin-right:4px"></span>${display}`;
            }
            html += `<td${cls}${titleAttr}>${display}</td>`;
        }
        html += '</tr>';
    }

    if (hasMore) {
        const remaining = allRows.length - SETTLEMENTS_PAGE_SIZE;
        html += `<tr class="show-more-row"><td colspan="${cols.length}">
            <button class="show-more-btn" id="show-more-settlements">
                Show ${remaining} more
            </button>
        </td></tr>`;
    }

    html += '</tbody></table>';
    el.innerHTML = html;

    el.querySelectorAll('th').forEach(th => {
        th.addEventListener('click', () => {
            const col = th.dataset.col;
            if (sortCol === col) { sortAsc = !sortAsc; }
            else { sortCol = col; sortAsc = true; }
            renderSettlements(visibleRuns.map(i => DATA.runs[i]));
        });
    });

    const btn = document.getElementById('show-more-settlements');
    if (btn) {
        btn.addEventListener('click', () => {
            settlementsExpanded = true;
            renderSettlements(visibleRuns.map(i => DATA.runs[i]));
        });
    }
}

// ── Config ────────────────────────────────────────────────
// Single line, key: value pairs

function renderConfig(runs) {
    const el = document.getElementById('config-inline');
    const run = runs[0];
    if (!run || !run.config) {
        el.innerHTML = '<span class="cfg-pair" style="color:var(--text-tertiary)">No configuration data</span>';
        return;
    }

    const cfg = run.config;
    el.innerHTML = Object.entries(cfg).map(([key, val]) => {
        const label = key.replace(/_/g, ' ');
        let display = String(val ?? '---');
        if (typeof val === 'boolean') display = val ? 'yes' : 'no';
        if (key === 'initial_cash') display = '$' + parseFloat(val).toFixed(2);
        return `<span class="cfg-pair"><span class="cfg-key">${label}:</span> <span class="cfg-val">${display}</span></span>`;
    }).join('');
}

// ── Utilities ─────────────────────────────────────────────

function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

// ── Boot ──────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);
