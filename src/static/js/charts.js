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

  function formatDollarPer1k(val) {
    const num = Number(val || 0);
    if (num === 0) return '$0.0000/1k';
    return num < 0.0001 ? `$${num.toFixed(6)}/1k` : `$${num.toFixed(4)}/1k`;
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
    updateHourlyActivityChart((data && data.sessions) || [], el.hourlyActivityCanvas);
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
    const output = list.map((m) => m.output || 0);
    const reasoning = list.map((m) => m.reasoning_output || 0);

    if (prepareCanvas(chartTokens, canvas)) {
      chartTokens.data.labels = labels;
      chartTokens.data.datasets[0].data = uncached;
      chartTokens.data.datasets[1].data = cached;
      chartTokens.data.datasets[2].data = output;
      chartTokens.data.datasets[3].data = reasoning;
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
          { label: 'Output', data: output, backgroundColor: '#8b5cf6', borderRadius: 4, stack },
          { label: 'Reasoning', data: reasoning, backgroundColor: '#ec4899', borderRadius: 4, stack },
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
      const total = Number(t.total_input || 0);
      return total > 0 ? Math.round((Number(t.cached_input || 0) / total) * 10000) / 100 : 0;
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
   * Chart 5: Cost per 1K Tokens by Model (Horizontal Bar)
   * Displays $/1K tokens efficiency for top models.
   */
  function updateCostPer1kChart(models, canvas) {
    if (!canvas) return;

    const list = (Array.isArray(models) ? models : [])
      .filter((m) => m && typeof m === 'object' && Number(m.total_tokens || 0) > 0)
      .sort((a, b) => Number(b.total_tokens || 0) - Number(a.total_tokens || 0))
      .slice(0, 8);

    const labels = list.map((m) => m.model || 'Unknown');
    const costPer1kData = list.map((m) => {
      const tokens = Number(m.total_tokens || 0);
      const cost = Number(m.est_cost_cached_usd ?? m.cost_cached_usd ?? 0);
      return tokens > 0 ? (cost / tokens) * 1000 : 0;
    });

    if (prepareCanvas(chartCostPer1k, canvas)) {
      chartCostPer1k.data.labels = labels;
      chartCostPer1k.data.datasets[0].data = costPer1kData;
      chartCostPer1k.update();
      return;
    }

    chartCostPer1k = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Cost / 1K Tokens ($)',
          data: costPer1kData,
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
          tooltip: defaultTooltip((ctx) => ` ${formatDollarPer1k(ctx.raw)}`),
        },
        scales: {
          x: {
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: { color: COLOR_MUTED, font: { size: 11 }, callback: (val) => formatDollarPer1k(val) },
            title: { display: true, text: 'Cost per 1K Tokens ($)', color: '#8b5cf6', font: { size: 11 } },
          },
          y: {
            grid: { color: GRID_COLOR },
            ticks: { color: COLOR_MUTED, font: { size: 11 } },
          },
        },
      },
    });
  }

  /**
   * Chart 6: Hourly Activity (Dual-Axis Bar & Line)
   * API call distribution and token volume by hour of day (UTC).
   */
  function updateHourlyActivityChart(sessions, canvas) {
    if (!canvas) return;
    const formatCompactNumber = utils().formatCompactNumber;

    const hourlyLabels = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);
    const hourlyTokens = new Array(24).fill(0);
    const hourlyCalls = new Array(24).fill(0);

    const list = Array.isArray(sessions) ? sessions : [];
    for (const s of list) {
      if (!s || typeof s !== 'object') continue;
      const rawDate = s.activity_at || s.created_at || s.start_time || s.end_time;
      if (!rawDate) continue;
      const d = new Date(rawDate);
      if (isNaN(d.getTime())) continue;
      const hour = d.getUTCHours();
      if (hour >= 0 && hour < 24) {
        hourlyTokens[hour] += Number(s.total_tokens || 0);
        hourlyCalls[hour] += Number(s.call_count || 1);
      }
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

  /**
   * Destroy all active chart instances.
   */
  function destroyCharts() {
    [chartTokens, chartCost, chartCostByTool, chartCacheTrend, chartCostPer1k, chartHourlyActivity].forEach((c) => c?.destroy());
    chartTokens = chartCost = chartCostByTool = chartCacheTrend = chartCostPer1k = chartHourlyActivity = null;
  }

  window.DashboardCharts = {
    updateCharts,
    updateTokensChart,
    updateCostChart,
    updateCostByToolChart,
    updateCacheTrendChart,
    updateCostPer1kChart,
    updateHourlyActivityChart,
    destroyCharts,
  };
})();
