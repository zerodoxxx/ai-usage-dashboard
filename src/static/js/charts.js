/**
 * DashboardCharts - All Chart.js helpers for the AI Usage dashboard.
 * Manages the 6 dashboard chart instances. Depends on window.DashboardUtils
 * for compact number formatting.
 */
(function () {
  'use strict';

  let chartTokens = null;
  let chartCost = null;
  let chartCostByTool = null;
  let chartCacheTrend = null;
  let chartCostPer1k = null;
  let chartHourlyActivity = null;

  const FONT_FAMILY = '-apple-system, BlinkMacSystemFont, sans-serif';
  const COLOR_MUTED = '#94a3b8';
  const GRID_COLOR = 'rgba(255, 255, 255, 0.04)';
  const BASE_OPTIONS = { responsive: true, maintainAspectRatio: false };

  function utils() {
    return window.DashboardUtils || { formatCompactNumber: (num) => String(num) };
  }

  function formatDollarPerMillion(val) {
    const num = Number(val || 0);
    if (!Number.isFinite(num) || num === 0) return '$0.00/1M';
    if (Math.abs(num) < 0.01) return `$${num.toFixed(4)}/1M`;
    if (Math.abs(num) < 1) return `$${num.toFixed(3)}/1M`;
    return `$${num.toFixed(2)}/1M`;
  }

  function defaultLegend() {
    return {
      position: 'top',
      labels: { color: COLOR_MUTED, font: { size: 11, family: FONT_FAMILY }, boxWidth: 12, padding: 12 },
    };
  }

  function defaultTooltip(labelCallback) {
    return {
      backgroundColor: '#1f2937',
      titleColor: '#f8fafc',
      bodyColor: '#e2e8f0',
      borderColor: 'rgba(255, 255, 255, 0.1)',
      borderWidth: 1,
      padding: 10,
      callbacks: labelCallback ? { label: labelCallback } : {},
    };
  }

  function defaultXAxis(opts = {}) {
    return {
      grid: { color: GRID_COLOR },
      ticks: { color: COLOR_MUTED, font: { size: 11 }, maxRotation: opts.maxRotation ?? 25, minRotation: 0 },
      ...opts,
    };
  }

  function defaultYAxis(titleText, color, callback, position = 'left', extra = {}) {
    return {
      type: 'linear',
      position,
      grid: { color: position === 'right' ? undefined : 'rgba(255, 255, 255, 0.05)', drawOnChartArea: position !== 'right' },
      ticks: { color, font: { size: 11 }, callback },
      title: { display: true, text: titleText, color, font: { size: 11 } },
      ...extra,
    };
  }

  function timeSeriesOptions(plugins, scales) {
    return {
      ...BASE_OPTIONS,
      interaction: { mode: 'index', intersect: false },
      plugins,
      scales,
    };
  }

  function prepareCanvas(currentChart, canvas) {
    if (currentChart) {
      if (currentChart.canvas === canvas) return true;
      currentChart.destroy();
    }
    Chart.getChart(canvas)?.destroy();
    return false;
  }

  /**
   * Build or update Chart.js visualizations.
   * Accepts canvas map or individual params for backward compatibility.
   */
  function updateCharts(data, elementsOrTokensCanvas, legacyCostCanvas) {
    if (typeof Chart === 'undefined') return;

    const isMap = elementsOrTokensCanvas && typeof elementsOrTokensCanvas === 'object' && !elementsOrTokensCanvas.tagName;
    const el = isMap ? elementsOrTokensCanvas : { tokensCanvas: elementsOrTokensCanvas, costCanvas: legacyCostCanvas };

    updateTokensChart((data && data.models) || [], el.tokensCanvas);
    updateCostChart((data && data.timeline) || [], el.costCanvas);
    updateCostByToolChart(data || {}, el.costByToolCanvas);
    updateCacheTrendChart((data && data.timeline) || [], el.cacheTrendCanvas);
    updateCostPer1kChart((data && data.models) || [], el.costPer1kCanvas);
    updateHourlyActivityChart(
      (data && data.hourly_timeline) || [],
      el.hourlyActivityCanvas,
      data && data.timezone
    );
    updateWeekdayHeatmap((data && data.weekday_hour) || [], el.weekdayHeatmap);
    updateSparklines((data && data.timeline) || [], el);
  }

  /**
   * Chart 1: Token Breakdown per Model (Stacked Bar)
   */
  function updateTokensChart(models, canvas) {
    if (!canvas) return;
    const formatCompactNumber = utils().formatCompactNumber;
    const list = (Array.isArray(models) ? models : []).filter((m) => m && typeof m === 'object');
    const labels = list.map((m) => m.model);
    const uncached = list.map((m) => m.uncached_input || 0);
    const cached = list.map((m) => m.cached_input || 0);
    const cacheWrite = list.map((m) => m.cache_write || 0);
    const output = list.map((m) => m.output || 0);

    if (prepareCanvas(chartTokens, canvas)) {
      chartTokens.data.labels = labels;
      chartTokens.data.datasets[0].data = uncached;
      chartTokens.data.datasets[1].data = cached;
      chartTokens.data.datasets[2].data = cacheWrite;
      chartTokens.data.datasets[3].data = output;
      chartTokens.update();
      return;
    }

    const stack = 'tokens';
    chartTokens = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'Uncached Input', data: uncached, backgroundColor: '#3b82f6', borderRadius: 4, stack },
          { label: 'Cached Input', data: cached, backgroundColor: '#06b6d4', borderRadius: 4, stack },
          { label: 'Cache Writes', data: cacheWrite, backgroundColor: '#f59e0b', borderRadius: 4, stack },
          { label: 'Output (incl. reasoning)', data: output, backgroundColor: '#8b5cf6', borderRadius: 4, stack },
        ],
      },
      options: timeSeriesOptions(
        {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => ` ${ctx.dataset.label || ''}: ${(ctx.raw || 0).toLocaleString()} tokens`),
        },
        {
          x: defaultXAxis({ stacked: true }),
          y: {
            stacked: true,
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: { color: COLOR_MUTED, font: { size: 11 }, callback: (v) => formatCompactNumber(v) },
          },
        }
      ),
    });
  }

  /**
   * Chart 2: Cost & Daily Trend (Dual-Axis Bar & Line)
   */
  function updateCostChart(timeline, canvas) {
    if (!canvas) return;
    const formatCompactNumber = utils().formatCompactNumber;
    const list = (Array.isArray(timeline) ? timeline : [])
      .filter((t) => t && typeof t === 'object')
      .sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const labels = list.map((t) => t.date);
    const costData = list.map((t) => t.cost_cached_usd || 0);
    const tokenData = list.map((t) => t.total_tokens || 0);
    const callData = list.map((t) => t.call_count || 0);

    if (prepareCanvas(chartCost, canvas)) {
      chartCost.data.labels = labels;
      chartCost.data.datasets[0].data = costData;
      chartCost.data.datasets[1].data = tokenData;
      if (chartCost.data.datasets[2]) chartCost.data.datasets[2].data = callData;
      chartCost.update();
      return;
    }

    chartCost = new Chart(canvas.getContext('2d'), {
      data: {
        labels,
        datasets: [
          {
            type: 'line', label: 'Cost ($)', data: costData, yAxisID: 'yCost',
            borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.15)',
            borderWidth: 2.5, fill: true, tension: 0.35, pointRadius: 3, pointHoverRadius: 6,
          },
          {
            type: 'bar', label: 'Tokens Processed', data: tokenData, yAxisID: 'yTokens',
            backgroundColor: 'rgba(99, 102, 241, 0.45)', borderColor: 'rgba(99, 102, 241, 0.8)',
            borderWidth: 1, borderRadius: 4,
          },
          {
            type: 'line', label: 'API Calls', data: callData, yAxisID: 'yCalls',
            borderColor: '#f59e0b', backgroundColor: 'rgba(245, 158, 11, 0.15)',
            borderWidth: 2, fill: false, tension: 0.35, pointRadius: 3, pointHoverRadius: 6,
          },
        ],
      },
      options: timeSeriesOptions(
        {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => {
            const l = ctx.dataset.label || '';
            const v = ctx.raw || 0;
            return l.includes('Cost') ? ` ${l}: $${Number(v).toFixed(4)}` : ` ${l}: ${Number(v).toLocaleString()}`;
          }),
        },
        {
          x: defaultXAxis({ maxRotation: 30 }),
          yCost: defaultYAxis('Cost ($)', '#34d399', (v) => `$${Number(v).toFixed(2)}`, 'left'),
          yTokens: defaultYAxis('Tokens', '#818cf8', (v) => formatCompactNumber(v), 'right'),
          yCalls: defaultYAxis('Calls', '#fbbf24', (v) => Number(v).toLocaleString(), 'right', { offset: true }),
        }
      ),
    });
  }

  /**
   * Chart 3: Cost by Tool (Doughnut)
   * Splits spend across Codex, Claude Code, and Antigravity.
   */
  function updateCostByToolChart(dataOrSessions, canvas) {
    if (!canvas) return;

    const sessions = Array.isArray(dataOrSessions)
      ? dataOrSessions
      : Array.isArray(dataOrSessions?.sessions)
        ? dataOrSessions.sessions
        : [];
    const models = !sessions.length && Array.isArray(dataOrSessions?.models) ? dataOrSessions.models : [];

    let codexCost = 0;
    let claudeCost = 0;
    let agyCost = 0;

    const items = sessions.length ? sessions : models;
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      const tool = String(item.tool || item.provider || '').toLowerCase();
      const cost = Number(item.cost_cached_usd ?? item.est_cost_cached_usd ?? 0) || 0;
      if (tool.includes('codex')) codexCost += cost;
      else if (tool.includes('claude')) claudeCost += cost;
      else if (tool.includes('antigravity') || tool.includes('agy') || tool.includes('google')) agyCost += cost;
    }

    const chartData = [
      Math.round(codexCost * 10000) / 10000,
      Math.round(claudeCost * 10000) / 10000,
      Math.round(agyCost * 10000) / 10000,
    ];

    if (prepareCanvas(chartCostByTool, canvas)) {
      chartCostByTool.data.datasets[0].data = chartData;
      chartCostByTool.update();
      return;
    }

    chartCostByTool = new Chart(canvas.getContext('2d'), {
      type: 'doughnut',
      data: {
        labels: ['Codex', 'Claude Code', 'Antigravity'],
        datasets: [{
          data: chartData,
          backgroundColor: ['#10a37f', '#d97706', '#4285f4'],
          borderColor: '#0f172a',
          borderWidth: 2,
          hoverOffset: 4,
        }],
      },
      options: {
        ...BASE_OPTIONS,
        cutout: '62%',
        plugins: {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => {
            const val = Number(ctx.raw || 0);
            const total = (ctx.dataset.data || []).reduce((acc, curr) => acc + Number(curr || 0), 0);
            const pct = total > 0 ? ((val / total) * 100).toFixed(1) : '0.0';
            return ` ${ctx.label || ''}: $${val.toFixed(4)} (${pct}%)`;
          }),
        },
      },
    });
  }

  /**
   * Chart 4: Cache-Efficiency Trend (Line)
   * Daily prompt cache hit rate percentage over time from timeline.
   */
  function updateCacheTrendChart(timeline, canvas) {
    if (!canvas) return;

    const list = (Array.isArray(timeline) ? timeline : [])
      .filter((t) => t && typeof t === 'object')
      .sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const labels = list.map((t) => t.date);
    const rateData = list.map((t) => {
      if (t.cache_hit_rate !== undefined && t.cache_hit_rate !== null) return Number(t.cache_hit_rate) || 0;
      const cacheableInput = Number(t.uncached_input || 0) + Number(t.cached_input || 0);
      return cacheableInput > 0 ? Math.round((Number(t.cached_input || 0) / cacheableInput) * 10000) / 100 : 0;
    });

    if (prepareCanvas(chartCacheTrend, canvas)) {
      chartCacheTrend.data.labels = labels;
      chartCacheTrend.data.datasets[0].data = rateData;
      chartCacheTrend.update();
      return;
    }

    chartCacheTrend = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cache Hit Rate (%)',
          data: rateData,
          borderColor: '#06b6d4',
          backgroundColor: 'rgba(6, 182, 212, 0.15)',
          borderWidth: 2.5,
          fill: true,
          tension: 0.35,
          pointRadius: 3,
          pointHoverRadius: 6,
        }],
      },
      options: timeSeriesOptions(
        {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => ` Cache Hit Rate: ${Number(ctx.raw || 0).toFixed(2)}%`),
        },
        {
          x: defaultXAxis({ maxRotation: 30 }),
          y: {
            min: 0,
            max: 100,
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: { color: '#06b6d4', font: { size: 11 }, callback: (val) => `${val}%` },
            title: { display: true, text: 'Hit Rate (%)', color: '#06b6d4', font: { size: 11 } },
          },
        }
      ),
    });
  }

  /**
   * Chart 5: Cost per 1M Tokens by Model (Horizontal Bar)
   * Actual blended $/1M from each model's token mix, for the 8 models
   * with the most tokens in the current filter.
   */
  function updateCostPer1kChart(models, canvas) {
    if (!canvas) return;

    const list = (Array.isArray(models) ? models : [])
      .filter((m) => m && typeof m === 'object' && Number(m.total_tokens || 0) > 0)
      .sort((a, b) => Number(b.total_tokens || 0) - Number(a.total_tokens || 0))
      .slice(0, 8);

    const labels = list.map((m) => m.model || 'Unknown');
    const costPerMillionData = list.map((m) => {
      const tokens = Number(m.total_tokens || 0);
      const cost = Number(m.est_cost_cached_usd ?? m.cost_cached_usd ?? 0);
      return tokens > 0 ? (cost / tokens) * 1_000_000 : 0;
    });

    if (prepareCanvas(chartCostPer1k, canvas)) {
      chartCostPer1k.data.labels = labels;
      chartCostPer1k.data.datasets[0].data = costPerMillionData;
      chartCostPer1k.data.datasets[0].label = 'Blended cost / 1M tokens ($)';
      if (chartCostPer1k.options.scales && chartCostPer1k.options.scales.x && chartCostPer1k.options.scales.x.title) {
        chartCostPer1k.options.scales.x.title.text = 'Blended cost per 1M tokens ($)';
      }
      if (chartCostPer1k.options.scales && chartCostPer1k.options.scales.x && chartCostPer1k.options.scales.x.ticks) {
        chartCostPer1k.options.scales.x.ticks.callback = (val) => formatDollarPerMillion(val);
      }
      chartCostPer1k.update();
      return;
    }

    chartCostPer1k = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Blended cost / 1M tokens ($)',
          data: costPerMillionData,
          backgroundColor: 'rgba(139, 92, 246, 0.7)',
          borderColor: '#8b5cf6',
          borderWidth: 1.5,
          borderRadius: 4,
        }],
      },
      options: {
        ...BASE_OPTIONS,
        indexAxis: 'y',
        plugins: {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => ` ${formatDollarPerMillion(ctx.raw)} actual blended`),
        },
        scales: {
          x: {
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: { color: COLOR_MUTED, font: { size: 11 }, callback: (val) => formatDollarPerMillion(val) },
            title: { display: true, text: 'Blended cost per 1M tokens ($)', color: '#8b5cf6', font: { size: 11 } },
          },
          y: {
            grid: { color: GRID_COLOR },
            ticks: { color: COLOR_MUTED, font: { size: 11 } },
          },
        },
      },
    });
  }

  function localTimeZoneLabel(zone) {
    try {
      const options = { timeZoneName: 'short' };
      if (zone) options.timeZone = String(zone);
      const parts = new Intl.DateTimeFormat(undefined, options).formatToParts(new Date());
      const tz = parts.find((part) => part.type === 'timeZoneName');
      if (tz && tz.value) return tz.value;
    } catch {
      /* keep the fallback label */
    }
    return 'local time';
  }

  /**
   * Chart 6: Hourly Activity (Dual-Axis Bar & Line)
   * Uses backend `hourly_timeline` (24 local-hour buckets). Missing data
   * falls back to 24 zeros — never re-buckets sessions on the client.
   */
  function updateHourlyActivityChart(hourlyTimeline, canvas, timezone) {
    if (!canvas) return;
    const formatCompactNumber = utils().formatCompactNumber;
    const titleEl = document.getElementById('hourly-activity-title');
    if (titleEl) {
      titleEl.textContent = `Hourly Activity (${localTimeZoneLabel(timezone)})`;
    }

    const hourlyLabels = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);
    const hourlyTokens = new Array(24).fill(0);
    const hourlyCalls = new Array(24).fill(0);

    const list = Array.isArray(hourlyTimeline) ? hourlyTimeline : [];
    for (const row of list) {
      if (!row || typeof row !== 'object') continue;
      const hour = Math.trunc(Number(row.hour));
      if (!Number.isFinite(hour) || hour < 0 || hour > 23) continue;
      hourlyTokens[hour] = Number(row.total_tokens || 0) || 0;
      hourlyCalls[hour] = Number(row.call_count || 0) || 0;
      if (row.label) hourlyLabels[hour] = String(row.label);
    }

    if (prepareCanvas(chartHourlyActivity, canvas)) {
      chartHourlyActivity.data.labels = hourlyLabels;
      chartHourlyActivity.data.datasets[0].data = hourlyTokens;
      chartHourlyActivity.data.datasets[1].data = hourlyCalls;
      chartHourlyActivity.update();
      return;
    }

    chartHourlyActivity = new Chart(canvas.getContext('2d'), {
      data: {
        labels: hourlyLabels,
        datasets: [
          {
            type: 'bar', label: 'Tokens Processed', data: hourlyTokens, yAxisID: 'yTokens',
            backgroundColor: 'rgba(99, 102, 241, 0.5)', borderColor: 'rgba(99, 102, 241, 0.85)',
            borderWidth: 1, borderRadius: 3,
          },
          {
            type: 'line', label: 'API Calls', data: hourlyCalls, yAxisID: 'yCalls',
            borderColor: '#f59e0b', backgroundColor: 'rgba(245, 158, 11, 0.15)',
            borderWidth: 2, fill: false, tension: 0.35, pointRadius: 3, pointHoverRadius: 6,
          },
        ],
      },
      options: timeSeriesOptions(
        {
          legend: defaultLegend(),
          tooltip: defaultTooltip((ctx) => ` ${ctx.dataset.label || ''}: ${Number(ctx.raw || 0).toLocaleString()}`),
        },
        {
          x: defaultXAxis({ maxRotation: 45 }),
          yTokens: defaultYAxis('Tokens', '#818cf8', (v) => formatCompactNumber(v), 'left'),
          yCalls: defaultYAxis('Calls', '#fbbf24', (v) => Number(v).toLocaleString(), 'right'),
        }
      ),
    });
  }

  const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function heatmapCellTitle(weekdayLabel, hour, cell) {
    const tokens = Number(cell.total_tokens || 0).toLocaleString();
    const calls = Number(cell.call_count || 0).toLocaleString();
    const cost = Number(cell.cost_cached_usd || 0).toFixed(4);
    const hh = String(hour).padStart(2, '0');
    return `${weekdayLabel} ${hh}:00 — ${tokens} tokens, ${calls} calls, $${cost}`;
  }

  /**
   * 7×24 weekday × hour heatmap (HTML/CSS grid). Intensity uses total_tokens,
   * or call_count if tokens are all zero. Empty input still renders a dim grid.
   */
  function updateWeekdayHeatmap(cells, container) {
    if (!container) return;
    const escapeHtml = (utils().escapeHtml) || ((s) => String(s));
    const grid = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => ({
      total_tokens: 0,
      call_count: 0,
      cost_cached_usd: 0,
      session_count: 0,
    })));

    const list = Array.isArray(cells) ? cells : [];
    for (const cell of list) {
      if (!cell || typeof cell !== 'object') continue;
      const day = Math.trunc(Number(cell.weekday));
      const hour = Math.trunc(Number(cell.hour));
      if (!Number.isFinite(day) || day < 0 || day > 6) continue;
      if (!Number.isFinite(hour) || hour < 0 || hour > 23) continue;
      grid[day][hour] = {
        total_tokens: Number(cell.total_tokens || 0) || 0,
        call_count: Number(cell.call_count || 0) || 0,
        cost_cached_usd: Number(cell.cost_cached_usd || 0) || 0,
        session_count: Number(cell.session_count || 0) || 0,
        weekday_label: cell.weekday_label,
      };
    }

    let maxTokens = 0;
    let maxCalls = 0;
    for (const row of grid) {
      for (const cell of row) {
        maxTokens = Math.max(maxTokens, cell.total_tokens);
        maxCalls = Math.max(maxCalls, cell.call_count);
      }
    }
    const useTokens = maxTokens > 0;
    const maxVal = useTokens ? maxTokens : maxCalls;

    const hourHeaders = Array.from({ length: 24 }, (_, hour) => (
      `<div class="heatmap-hour">${String(hour).padStart(2, '0')}</div>`
    )).join('');

    let body = `<div class="heatmap-corner"></div>${hourHeaders}`;
    WEEKDAY_LABELS.forEach((label, day) => {
      body += `<div class="heatmap-weekday">${label}</div>`;
      for (let hour = 0; hour < 24; hour += 1) {
        const cell = grid[day][hour];
        const value = useTokens ? cell.total_tokens : cell.call_count;
        const t = maxVal > 0 ? value / maxVal : 0;
        const alpha = (0.08 + t * 0.87).toFixed(3);
        const weekdayLabel = cell.weekday_label || label;
        const title = heatmapCellTitle(weekdayLabel, hour, cell);
        body += `<div class="heatmap-cell" style="background:rgba(99,102,241,${alpha})" title="${escapeHtml(title)}"></div>`;
      }
    });

    container.innerHTML = `
      <div class="heatmap-scroll">
        <div class="heatmap-grid" role="img" aria-label="Weekday by hour activity heatmap">${body}</div>
      </div>
      <div class="heatmap-legend">
        <span>Low</span>
        <div class="heatmap-legend-bar" aria-hidden="true"></div>
        <span>High</span>
      </div>
    `;
  }

  function sparklinePoints(values, width, height, pad) {
    const nums = (Array.isArray(values) ? values : []).map((value) => {
      const n = Number(value);
      return Number.isFinite(n) ? n : 0;
    });
    if (nums.length === 0) nums.push(0, 0);
    if (nums.length === 1) nums.push(nums[0]);
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const span = max - min;
    return nums.map((value, index) => {
      const x = pad + (index / (nums.length - 1)) * (width - pad * 2);
      const y = span === 0
        ? height / 2
        : height - pad - ((value - min) / span) * (height - pad * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(' ');
  }

  function renderSparkline(container, values, color) {
    if (!container) return;
    const width = 100;
    const height = 28;
    const pad = 2;
    const safeColor = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(String(color || ''))
      ? color
      : '#94a3b8';
    const points = sparklinePoints(values, width, height, pad);
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" class="sparkline" preserveAspectRatio="none" aria-hidden="true"><polyline fill="none" stroke="${safeColor}" stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round" points="${points}"></polyline></svg>`;
  }

  function timelineFieldSeries(timeline, key) {
    const list = Array.isArray(timeline) ? timeline : [];
    return list.map((row) => {
      if (!row || typeof row !== 'object') return 0;
      const n = Number(row[key] || 0);
      return Number.isFinite(n) ? n : 0;
    });
  }

  function updateSparklines(timeline, el) {
    if (!el) return;
    renderSparkline(el.sparklineSpend, timelineFieldSeries(timeline, 'cost_cached_usd'), '#10b981');
    renderSparkline(el.sparklineBurn, timelineFieldSeries(timeline, 'cost_cached_usd'), '#f59e0b');
    renderSparkline(el.sparklineSavings, timelineFieldSeries(timeline, 'savings_usd'), '#06b6d4');
    renderSparkline(el.sparklineSessions, timelineFieldSeries(timeline, 'session_count'), '#ec4899');
  }

  /**
   * Destroy all active chart instances.
   */
  function destroyCharts() {
    [chartTokens, chartCost, chartCostByTool, chartCacheTrend, chartCostPer1k, chartHourlyActivity].forEach((c) => c?.destroy());
    chartTokens = chartCost = chartCostByTool = chartCacheTrend = chartCostPer1k = chartHourlyActivity = null;
  }

  function resizeCharts() {
    [chartTokens, chartCost, chartCostByTool, chartCacheTrend, chartCostPer1k, chartHourlyActivity].forEach((chart) => {
      try { chart?.resize(); } catch { /* canvas may be hidden */ }
    });
  }

  window.DashboardCharts = {
    updateCharts,
    updateTokensChart,
    updateCostChart,
    updateCostByToolChart,
    updateCacheTrendChart,
    updateCostPer1kChart,
    updateHourlyActivityChart,
    updateWeekdayHeatmap,
    updateSparklines,
    resizeCharts,
    destroyCharts,
  };
})();
