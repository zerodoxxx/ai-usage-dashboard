/**
 * AI Tools Usage & Cost Visualizer - Client-Side Controller
 * Manages state, odometers, Chart.js graphs, live polling, and table filtering.
 */

(function () {
  'use strict';

  // Global Dashboard State
  const state = {
    currentTool: 'all',
    autoRefreshInterval: 30000,
    refreshTimer: null,
    isFetching: false,
    pricingData: null,
    currentUsageData: null,
    allSessions: [],
    searchQuery: '',
  };

  // Active in-flight abort controller for user-initiated fetches
  let currentAbortController = null;

  // Odometer Instances
  let odometers = {
    totalTokens: null,
    totalCost: null,
    cachedTokens: null,
    cacheRate: null,
    outputTokens: null,
    sessions: null,
  };

  // Chart Instances
  let chartTokens = null;
  let chartCost = null;

  // DOM Elements cache
  const elements = {};

  /**
   * Format numbers compactly (e.g. 1.5M, 20.4K)
   */
  function formatCompactNumber(num) {
    const n = Math.abs(Number(num));
    if (n >= 1e9) return (num / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (num / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (num / 1e3).toFixed(1) + 'K';
    return String(num);
  }

  /**
   * Format date strings cleanly
   */
  function formatDateTime(isoString) {
    if (!isoString) return '—';
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString).slice(0, 19);
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
      return String(isoString).slice(0, 19);
    }
  }

  /**
   * Toast notification display
   */
  function showToast(message, type = 'info') {
    const container = elements.toastContainer;
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type === 'error' ? 'toast-error' : ''}`;
    toast.innerHTML = `
      <span>${type === 'error' ? '⚠️' : 'ℹ️'}</span>
      <span>${message}</span>
    `;

    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  /**
   * Initialize rolling odometers defensively
   */
  function initOdometers() {
    const createOdometer = (selector, opts) => {
      try {
        const el = document.querySelector(selector);
        if (!el || typeof RollingOdometer === 'undefined') return null;
        return new RollingOdometer({ element: selector, ...opts });
      } catch (err) {
        console.warn(`Failed to initialize odometer for ${selector}:`, err);
        return null;
      }
    };

    odometers.totalTokens = createOdometer('#odo-total-tokens', {
      prefix: '',
      suffix: '',
      decimals: 0,
      formatCommas: true,
      duration: 850,
    });

    odometers.totalCost = createOdometer('#odo-total-cost', {
      prefix: '$',
      suffix: '',
      decimals: 4,
      formatCommas: true,
      duration: 850,
    });

    odometers.cachedTokens = createOdometer('#odo-cached-tokens', {
      prefix: '',
      suffix: '',
      decimals: 0,
      formatCommas: true,
      duration: 850,
    });

    odometers.cacheRate = createOdometer('#odo-cache-rate', {
      prefix: '',
      suffix: '%',
      decimals: 2,
      formatCommas: true,
      duration: 850,
    });

    odometers.outputTokens = createOdometer('#odo-output-tokens', {
      prefix: '',
      suffix: '',
      decimals: 0,
      formatCommas: true,
      duration: 850,
    });

    odometers.sessions = createOdometer('#odo-sessions', {
      prefix: '',
      suffix: '',
      decimals: 0,
      formatCommas: true,
      duration: 850,
    });
  }

  /**
   * Fetch reference pricing table from /api/pricing
   */
  async function fetchPricing() {
    try {
      const res = await fetch('/api/pricing');
      if (res.ok) {
        state.pricingData = await res.json();
      }
    } catch (e) {
      console.warn('Failed to load pricing table:', e);
    }
  }

  /**
   * Resolve pricing rates for a given model
   */
  function getModelRates(modelName) {
    if (!state.pricingData) {
      return { uncached_input: 0.20, cached_input: 0.02, output: 1.20 };
    }
    if (state.pricingData[modelName]) {
      return state.pricingData[modelName];
    }
    const norm = (modelName || '').toLowerCase().trim();
    for (const [k, v] of Object.entries(state.pricingData)) {
      if (k.toLowerCase() === norm) return v;
    }
    for (const [k, v] of Object.entries(state.pricingData)) {
      if (norm.includes(k.toLowerCase()) || k.toLowerCase().includes(norm)) return v;
    }
    if (norm.includes('gemini')) {
      return state.pricingData['Gemini 3.8 Flash (High)'] || { uncached_input: 0.10, cached_input: 0.025, output: 0.40 };
    }
    return state.pricingData['gpt-5.6-luna'] || { uncached_input: 0.20, cached_input: 0.02, output: 1.20 };
  }

  /**
   * Fetch usage data from /api/usage?tool=...
   * @param {boolean} [isUserInitiated=false]
   */
  async function fetchUsageData(isUserInitiated = false) {
    if (isUserInitiated) {
      if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
      }
      state.isFetching = false;
    } else if (state.isFetching) {
      return;
    }

    state.isFetching = true;
    const controller = new AbortController();
    currentAbortController = controller;

    const refreshIcon = elements.refreshBtn ? elements.refreshBtn.querySelector('.refresh-icon') : null;
    if (refreshIcon) refreshIcon.classList.add('spin');

    try {
      const res = await fetch(`/api/usage?tool=${encodeURIComponent(state.currentTool)}`, {
        signal: controller.signal,
      });
      if (!res.ok) {
        const errorText = await res.text();
        throw new Error(`API error ${res.status}: ${errorText}`);
      }

      const data = await res.json();
      state.currentUsageData = data;
      state.allSessions = Array.isArray(data.sessions) ? data.sessions : [];

      // Update UI components
      updateMetricCards(data.summary || {});
      renderModelTable(data.models || []);
      renderSessionsTable();
      updateCharts(data);

      // Update last synced badge
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const timeStr = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
      if (elements.lastSyncedBadge) {
        elements.lastSyncedBadge.textContent = `Synced ${timeStr}`;
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        // Request was aborted by a new user-initiated fetch; silently exit
        return;
      }
      console.error('Failed to fetch usage metrics:', err);
      showToast(`Sync failed: ${err.message}`, 'error');
      if (elements.lastSyncedBadge) {
        elements.lastSyncedBadge.textContent = 'Sync error';
      }
    } finally {
      if (currentAbortController === controller) {
        currentAbortController = null;
        state.isFetching = false;
      }
      if (refreshIcon) {
        setTimeout(() => refreshIcon.classList.remove('spin'), 400);
      }
    }
  }

  /**
   * Update the 6 metric cards and their subtexts
   */
  function updateMetricCards(summary) {
    const totalTokens = summary.total_tokens || 0;
    const totalCost = summary.cost_cached_usd || 0;
    const cachedTokens = summary.cached_input || 0;
    const cacheHitRate = summary.cache_hit_rate || 0;
    const outputTokens = summary.output || 0;
    const sessionCount = summary.session_count || 0;
    const savings = summary.savings_usd || 0;
    const reasoning = summary.reasoning_output || 0;
    const callCount = summary.call_count || 0;

    // Roll Odometers
    if (odometers.totalTokens) odometers.totalTokens.update(totalTokens);
    if (odometers.totalCost) odometers.totalCost.update(totalCost);
    if (odometers.cachedTokens) odometers.cachedTokens.update(cachedTokens);
    if (odometers.cacheRate) odometers.cacheRate.update(cacheHitRate);
    if (odometers.outputTokens) odometers.outputTokens.update(outputTokens);
    if (odometers.sessions) odometers.sessions.update(sessionCount);

    // Update Subtexts
    if (elements.cardSavingsText) {
      elements.cardSavingsText.textContent = `Saved $${savings.toFixed(4)} cached`;
    }
    if (elements.cardCachedShare) {
      elements.cardCachedShare.textContent = `${cacheHitRate.toFixed(1)}% of input tokens`;
    }
    if (elements.cardReasoningSubtext) {
      elements.cardReasoningSubtext.textContent = `Incl. ${reasoning.toLocaleString()} reasoning`;
    }
    if (elements.cardCallsSubtext) {
      elements.cardCallsSubtext.textContent = `${callCount.toLocaleString()} API calls recorded`;
    }
  }

  /**
   * Render Per-Model Granularity Table with all 12 columns
   */
  function renderModelTable(models) {
    const tbody = elements.modelsTableBody;
    if (!tbody) return;

    const modelList = Array.isArray(models) ? models : [];

    if (elements.modelsCountBadge) {
      elements.modelsCountBadge.textContent = `${modelList.length} Model${modelList.length === 1 ? '' : 's'}`;
    }

    if (modelList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No model usage data recorded.</td></tr>';
      return;
    }

    let rowsHtml = '';
    modelList.forEach((m) => {
      const modelName = String(m.model || 'unknown');
      const isCodex = m.tool === 'codex' || /gpt|o1|o3/i.test(modelName);
      const badgeClass = isCodex ? 'badge-openai' : 'badge-agy';
      const badgeText = isCodex ? 'OpenAI' : 'Google AGY';

      const rates = getModelRates(modelName);
      const ratesStr = `$${rates.uncached_input.toFixed(2)} / $${rates.cached_input.toFixed(3)} / $${rates.output.toFixed(2)}`;

      const hitRate = Number(m.cache_hit_rate) || 0;
      let hitRateClass = 'hit-rate-low';
      if (hitRate >= 75) hitRateClass = 'hit-rate-high';
      else if (hitRate >= 35) hitRateClass = 'hit-rate-mid';

      rowsHtml += `
        <tr>
          <td>
            <strong>${escapeHtml(modelName)}</strong>
            <span class="provider-badge ${badgeClass}" style="margin-left: 8px;">${badgeText}</span>
          </td>
          <td class="cell-mono cell-right">${(m.uncached_input || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right">${(m.cached_input || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right">${(m.total_input || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right">${(m.output || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right">${(m.reasoning_output || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right"><strong>${(m.total_tokens || 0).toLocaleString()}</strong></td>
          <td class="cell-right">
            <span class="hit-rate-pill ${hitRateClass}">${hitRate.toFixed(2)}%</span>
          </td>
          <td class="cell-mono cell-right text-muted">${ratesStr}</td>
          <td class="cell-mono cell-right"><strong>$${(m.est_cost_cached_usd || 0).toFixed(4)}</strong></td>
          <td class="cell-mono cell-right text-muted">$${(m.est_cost_uncached_usd || 0).toFixed(4)}</td>
          <td class="cell-mono cell-right text-success">+$${(m.est_savings_usd || 0).toFixed(4)}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
  }

  /**
   * Render Recent Sessions Explorer with real-time text search filtering
   */
  function renderSessionsTable() {
    const tbody = elements.sessionsTableBody;
    if (!tbody) return;

    const query = String(state.searchQuery || '').trim().toLowerCase();
    const sessionList = Array.isArray(state.allSessions) ? state.allSessions : [];
    let filtered = sessionList;

    if (query) {
      filtered = sessionList.filter((s) => {
        if (!s || typeof s !== 'object') return false;
        const title = String(s.title || '').toLowerCase();
        const model = String(s.model || '').toLowerCase();
        const tool = String(s.tool || '').toLowerCase();
        const id = String(s.id || '').toLowerCase();
        return title.includes(query) || model.includes(query) || tool.includes(query) || id.includes(query);
      });
    }

    if (elements.sessionsCountBadge) {
      elements.sessionsCountBadge.textContent = query
        ? `${filtered.length} of ${sessionList.length} Sessions`
        : `${sessionList.length} Sessions`;
    }

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No matching sessions found.</td></tr>';
      return;
    }

    let rowsHtml = '';
    // Show top 100 most recent filtered sessions for high responsiveness
    const displayList = filtered.slice(0, 100);

    displayList.forEach((s) => {
      const toolStr = String(s.tool || '');
      const isCodex = toolStr === 'codex';
      const badgeClass = isCodex ? 'badge-codex' : 'badge-agy';
      const badgeText = isCodex ? 'Codex' : 'Antigravity';

      const hitRate = Number(s.cache_hit_rate) || 0;
      let hitRateClass = 'hit-rate-low';
      if (hitRate >= 75) hitRateClass = 'hit-rate-high';
      else if (hitRate >= 35) hitRateClass = 'hit-rate-mid';

      const dateStr = formatDateTime(s.created_at || s.start_time);
      const titleStr = String(s.title || 'Untitled Session');
      const idStr = String(s.id || '');
      const modelStr = String(s.model || 'unknown');

      rowsHtml += `
        <tr>
          <td>
            <strong>${escapeHtml(titleStr)}</strong>
            <div class="text-muted" style="font-size: 11px; font-family: var(--font-mono);">${escapeHtml(idStr)}</div>
          </td>
          <td>
            <span class="provider-badge ${badgeClass}">${badgeText}</span>
          </td>
          <td class="cell-mono">${escapeHtml(modelStr)}</td>
          <td class="cell-mono cell-right"><strong>${(s.total_tokens || 0).toLocaleString()}</strong></td>
          <td class="cell-right">
            <span class="hit-rate-pill ${hitRateClass}">${hitRate.toFixed(1)}%</span>
          </td>
          <td class="cell-mono cell-right text-success">$${(s.cost_cached_usd || 0).toFixed(4)}</td>
          <td class="cell-mono cell-right text-muted">${dateStr}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
  }

  /**
   * Build or update Chart.js visualizations
   */
  function updateCharts(data) {
    if (typeof Chart === 'undefined') return;

    updateTokensChart(data.models || []);
    updateCostChart(data.timeline || []);
  }

  /**
   * Chart 1: Token Breakdown per Model (Stacked Bar)
   */
  function updateTokensChart(models) {
    const canvas = elements.chartTokensCanvas;
    if (!canvas) return;

    const labels = models.map((m) => m.model);
    const uncachedData = models.map((m) => m.uncached_input || 0);
    const cachedData = models.map((m) => m.cached_input || 0);
    const outputData = models.map((m) => m.output || 0);
    const reasoningData = models.map((m) => m.reasoning_output || 0);

    if (chartTokens) {
      chartTokens.data.labels = labels;
      chartTokens.data.datasets[0].data = uncachedData;
      chartTokens.data.datasets[1].data = cachedData;
      chartTokens.data.datasets[2].data = outputData;
      chartTokens.data.datasets[3].data = reasoningData;
      chartTokens.update();
      return;
    }

    Chart.getChart(canvas)?.destroy();

    const ctx = canvas.getContext('2d');
    chartTokens = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Uncached Input',
            data: uncachedData,
            backgroundColor: '#3b82f6',
            borderRadius: 4,
            stack: 'tokens',
          },
          {
            label: 'Cached Input',
            data: cachedData,
            backgroundColor: '#06b6d4',
            borderRadius: 4,
            stack: 'tokens',
          },
          {
            label: 'Output',
            data: outputData,
            backgroundColor: '#8b5cf6',
            borderRadius: 4,
            stack: 'tokens',
          },
          {
            label: 'Reasoning',
            data: reasoningData,
            backgroundColor: '#ec4899',
            borderRadius: 4,
            stack: 'tokens',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'index',
          intersect: false,
        },
        plugins: {
          legend: {
            position: 'top',
            labels: {
              color: '#94a3b8',
              font: { size: 11, family: '-apple-system, BlinkMacSystemFont, sans-serif' },
              boxWidth: 12,
              padding: 12,
            },
          },
          tooltip: {
            backgroundColor: '#1f2937',
            titleColor: '#f8fafc',
            bodyColor: '#e2e8f0',
            borderColor: 'rgba(255, 255, 255, 0.1)',
            borderWidth: 1,
            padding: 10,
            callbacks: {
              label: function (ctx) {
                const label = ctx.dataset.label || '';
                const val = ctx.raw || 0;
                return ` ${label}: ${val.toLocaleString()} tokens`;
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { color: 'rgba(255, 255, 255, 0.04)' },
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              maxRotation: 25,
              minRotation: 0,
            },
          },
          y: {
            stacked: true,
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              callback: (val) => formatCompactNumber(val),
            },
          },
        },
      },
    });
  }

  /**
   * Chart 2: Cost & Daily Trend (Dual-Axis Bar & Line)
   */
  function updateCostChart(timeline) {
    const canvas = elements.chartCostCanvas;
    if (!canvas) return;

    const sortedTimeline = [...timeline].sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const labels = sortedTimeline.map((t) => t.date);
    const costData = sortedTimeline.map((t) => t.cost_cached_usd || 0);
    const tokenData = sortedTimeline.map((t) => t.total_tokens || 0);

    if (chartCost) {
      chartCost.data.labels = labels;
      chartCost.data.datasets[0].data = costData;
      chartCost.data.datasets[1].data = tokenData;
      chartCost.update();
      return;
    }

    Chart.getChart(canvas)?.destroy();

    const ctx = canvas.getContext('2d');
    chartCost = new Chart(ctx, {
      data: {
        labels: labels,
        datasets: [
          {
            type: 'line',
            label: 'Cost ($)',
            data: costData,
            borderColor: '#10b981',
            backgroundColor: 'rgba(16, 185, 129, 0.15)',
            borderWidth: 2.5,
            fill: true,
            tension: 0.35,
            yAxisID: 'yCost',
            pointRadius: 3,
            pointHoverRadius: 6,
          },
          {
            type: 'bar',
            label: 'Tokens Processed',
            data: tokenData,
            backgroundColor: 'rgba(99, 102, 241, 0.45)',
            borderColor: 'rgba(99, 102, 241, 0.8)',
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: 'yTokens',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'index',
          intersect: false,
        },
        plugins: {
          legend: {
            position: 'top',
            labels: {
              color: '#94a3b8',
              font: { size: 11, family: '-apple-system, BlinkMacSystemFont, sans-serif' },
              boxWidth: 12,
              padding: 12,
            },
          },
          tooltip: {
            backgroundColor: '#1f2937',
            titleColor: '#f8fafc',
            bodyColor: '#e2e8f0',
            borderColor: 'rgba(255, 255, 255, 0.1)',
            borderWidth: 1,
            padding: 10,
            callbacks: {
              label: function (ctx) {
                const label = ctx.dataset.label || '';
                const val = ctx.raw || 0;
                if (label.includes('Cost')) {
                  return ` ${label}: $${Number(val).toFixed(4)}`;
                }
                return ` ${label}: ${Number(val).toLocaleString()}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: 'rgba(255, 255, 255, 0.04)' },
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              maxRotation: 30,
              minRotation: 0,
            },
          },
          yCost: {
            type: 'linear',
            position: 'left',
            grid: { color: 'rgba(255, 255, 255, 0.05)' },
            ticks: {
              color: '#34d399',
              font: { size: 11 },
              callback: (val) => `$${Number(val).toFixed(2)}`,
            },
            title: {
              display: true,
              text: 'Cost ($)',
              color: '#34d399',
              font: { size: 11 },
            },
          },
          yTokens: {
            type: 'linear',
            position: 'right',
            grid: { drawOnChartArea: false },
            ticks: {
              color: '#818cf8',
              font: { size: 11 },
              callback: (val) => formatCompactNumber(val),
            },
            title: {
              display: true,
              text: 'Tokens',
              color: '#818cf8',
              font: { size: 11 },
            },
          },
        },
      },
    });
  }

  /**
   * Helper to escape HTML and prevent XSS
   */
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  /**
   * Setup recurring auto-refresh timer
   */
  function setupAutoRefresh() {
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer);
      state.refreshTimer = null;
    }

    if (elements.autoRefreshToggle && elements.autoRefreshToggle.checked) {
      state.refreshTimer = setInterval(() => {
        fetchUsageData();
      }, state.autoRefreshInterval);
    }
  }

  /**
   * Attach all DOM event listeners
   */
  function setupEventListeners() {
    // Tool dropdown switch
    if (elements.toolSelect) {
      elements.toolSelect.addEventListener('change', (e) => {
        state.currentTool = e.target.value;
        fetchUsageData(true);
      });
    }

    // Auto-refresh toggle
    if (elements.autoRefreshToggle) {
      elements.autoRefreshToggle.addEventListener('change', () => {
        setupAutoRefresh();
      });
    }

    // Refresh interval dropdown
    if (elements.refreshInterval) {
      elements.refreshInterval.addEventListener('change', (e) => {
        state.autoRefreshInterval = parseInt(e.target.value, 10) || 30000;
        setupAutoRefresh();
      });
    }

    // Refresh now button
    if (elements.refreshBtn) {
      elements.refreshBtn.addEventListener('click', () => {
        fetchUsageData(true);
      });
    }

    // Sessions search input
    if (elements.sessionsSearch) {
      elements.sessionsSearch.addEventListener('input', (e) => {
        state.searchQuery = e.target.value;
        renderSessionsTable();
      });
    }
  }

  /**
   * Cache DOM elements for fast access
   */
  function cacheElements() {
    elements.toolSelect = document.getElementById('tool-select');
    elements.autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    elements.refreshInterval = document.getElementById('refresh-interval');
    elements.refreshBtn = document.getElementById('refresh-btn');
    elements.lastSyncedBadge = document.getElementById('last-synced-badge');

    elements.cardSavingsText = document.getElementById('card-savings-text');
    elements.cardCachedShare = document.getElementById('card-cached-share');
    elements.cardReasoningSubtext = document.getElementById('card-reasoning-subtext');
    elements.cardCallsSubtext = document.getElementById('card-calls-subtext');

    elements.chartTokensCanvas = document.getElementById('chart-tokens');
    elements.chartCostCanvas = document.getElementById('chart-cost');

    elements.modelsCountBadge = document.getElementById('models-count-badge');
    elements.modelsTableBody = document.getElementById('models-table-body');

    elements.sessionsCountBadge = document.getElementById('sessions-count-badge');
    elements.sessionsSearch = document.getElementById('sessions-search');
    elements.sessionsTableBody = document.getElementById('sessions-table-body');

    elements.toastContainer = document.getElementById('toast-container');
  }

  /**
   * Main Initialization Routine
   */
  async function init() {
    cacheElements();
    initOdometers();
    setupEventListeners();

    // Fetch initial pricing metadata and first usage batch
    await fetchPricing();
    await fetchUsageData();

    // Start auto-refresh timer
    setupAutoRefresh();
  }

  // Execute on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
