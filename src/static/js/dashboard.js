/**
 * AI Tools Usage & Cost Visualizer - Client-Side Controller
 * Manages state, odometers, Chart.js graphs, live polling, and table filtering.
 */

(function () {
  'use strict';

  // Global Dashboard State
  const state = {
    currentTool: 'all',
    currentTimeRange: 'all',
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
   * Pick a short, single-line representation for the summary token cards.
   * The exact value remains available through the card's accessible label/title.
   */
  function getCompactMetricParts(num) {
    const numericValue = Number(num);
    const value = Number.isFinite(numericValue) ? numericValue : 0;
    const absoluteValue = Math.abs(value);
    const units = [
      { threshold: 1e3, suffix: 'K' },
      { threshold: 1e6, suffix: 'M' },
      { threshold: 1e9, suffix: 'B' },
      { threshold: 1e12, suffix: 'T' },
    ];

    let unitIndex = -1;
    units.forEach((unit, index) => {
      if (absoluteValue >= unit.threshold) unitIndex = index;
    });

    if (unitIndex >= 0) {
      let unit = units[unitIndex];
      let scaledValue = value / unit.threshold;
      let roundedValue = Number(scaledValue.toFixed(1));

      // Avoid awkward values such as 1,000.0K at a unit boundary.
      if (Math.abs(roundedValue) >= 1000 && unitIndex < units.length - 1) {
        unit = units[unitIndex + 1];
        scaledValue = value / unit.threshold;
        roundedValue = Number(scaledValue.toFixed(1));
      }

      return {
        value: roundedValue,
        decimals: 1,
        formatCommas: false,
        suffix: unit.suffix,
      };
    }

    return {
      value,
      decimals: 0,
      formatCommas: true,
      suffix: '',
    };
  }

  /**
   * Update a token metric with compact display text while preserving the full value.
   */
  function updateCompactMetric(odometer, unitElement, valueContainer, rawValue, label) {
    if (!odometer) return;

    const numericValue = Number(rawValue);
    const value = Number.isFinite(numericValue) ? numericValue : 0;
    const parts = getCompactMetricParts(value);
    const fullValue = Math.round(value).toLocaleString();

    odometer.decimals = parts.decimals;
    odometer.formatCommas = parts.formatCommas;
    odometer.update(parts.value);

    if (unitElement) unitElement.textContent = parts.suffix;
    if (valueContainer) {
      const accessibleValue = `${fullValue} ${label}`;
      valueContainer.title = accessibleValue;
      valueContainer.setAttribute('aria-label', accessibleValue);
    }
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
      <span>${escapeHtml(String(message))}</span>
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
      if (!res.ok) {
        throw new Error(`Failed to fetch pricing: ${res.status} ${res.statusText}`);
      }
      state.pricingData = await res.json();
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
    const sortedPricing = Object.entries(state.pricingData).sort((a, b) => b[0].length - a[0].length);
    for (const [k, v] of sortedPricing) {
      if (k.toLowerCase() === norm) return v;
    }
    for (const [k, v] of sortedPricing) {
      if (norm.includes(k.toLowerCase())) return v;
    }
    if (norm.includes('gemini')) {
      return state.pricingData['Gemini 3.8 Flash (High)'] || { uncached_input: 0.10, cached_input: 0.025, output: 0.40 };
    }
    return state.pricingData['gpt-5.6-luna'] || { uncached_input: 0.20, cached_input: 0.02, output: 1.20 };
  }

  /**
   * Fetch usage data from /api/usage?tool=...&time_range=...
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
      const query = new URLSearchParams({
        tool: state.currentTool,
        time_range: state.currentTimeRange,
      });
      const res = await fetch(`/api/usage?${query.toString()}`, {
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
      updateAnalytics(data.analytics || {}, data.summary || {});
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

    // Roll Odometers. Token totals use compact units so large values remain readable
    // within the fixed-width summary cards; tables retain the exact values.
    updateCompactMetric(
      odometers.totalTokens,
      elements.metricTotalTokensUnit,
      elements.metricTotalTokensValue,
      totalTokens,
      'total tokens',
    );
    if (odometers.totalCost) odometers.totalCost.update(totalCost);
    updateCompactMetric(
      odometers.cachedTokens,
      elements.metricCachedTokensUnit,
      elements.metricCachedTokensValue,
      cachedTokens,
      'cached input tokens',
    );
    if (odometers.cacheRate) odometers.cacheRate.update(cacheHitRate);
    updateCompactMetric(
      odometers.outputTokens,
      elements.metricOutputTokensUnit,
      elements.metricOutputTokensValue,
      outputTokens,
      'output tokens',
    );
    if (odometers.sessions) odometers.sessions.update(sessionCount);

    // Update Subtexts
    if (elements.cardSavingsText) {
      elements.cardSavingsText.textContent = `Saved $${savings.toFixed(4)} cached`;
    }
    if (elements.cardTokensSubtext) {
      const selectedOption = elements.timeRangeSelect?.selectedOptions?.[0];
      elements.cardTokensSubtext.textContent = state.currentTimeRange === 'all'
        ? 'Cumulative audit'
        : `${selectedOption?.textContent || 'Selected range'} usage`;
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
   * Format the secondary analytics snapshot.
   */
  function updateAnalytics(analytics, summary) {
    const details = analytics && typeof analytics === 'object' ? analytics : {};
    const totals = summary && typeof summary === 'object' ? summary : {};
    const formatCurrency = (value) => `$${Number(value || 0).toFixed(4)}`;
    const formatChange = (value) => {
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'n/a';
      const numericValue = Number(value);
      return `${numericValue > 0 ? '+' : ''}${numericValue.toFixed(1)}%`;
    };
    const changeClass = (value, lowerIsBetter = false) => {
      if (value === null || value === undefined || !Number.isFinite(Number(value)) || Number(value) === 0) {
        return 'comparison-neutral';
      }
      const improved = lowerIsBetter ? Number(value) < 0 : Number(value) > 0;
      return improved ? 'comparison-positive' : 'comparison-negative';
    };

    const selectedOption = elements.timeRangeSelect?.selectedOptions?.[0];
    if (elements.analyticsWindowBadge) {
      elements.analyticsWindowBadge.textContent = selectedOption?.textContent || 'Selected range';
    }
    if (elements.analyticsCallCount) {
      elements.analyticsCallCount.textContent = Number(totals.call_count || 0).toLocaleString();
    }
    if (elements.analyticsAvgCost) {
      elements.analyticsAvgCost.textContent = formatCurrency(details.avg_cost_per_session_usd);
    }
    if (elements.analyticsAvgTokens) {
      elements.analyticsAvgTokens.textContent = formatCompactNumber(Math.round(Number(details.avg_tokens_per_session || 0)));
    }
    if (elements.analyticsMonthlyProjection) {
      elements.analyticsMonthlyProjection.textContent = formatCurrency(details.monthly_projection_usd);
    }
    if (elements.analyticsProjectionBasis) {
      const projectionLabels = {
        last_30_days: 'Based on last 30 days',
        current_month_run_rate: 'Current-month run rate',
        '30d_run_rate': '30-day run rate',
        '7d_run_rate': '7-day run rate',
        '24h_run_rate': '24-hour run rate',
      };
      elements.analyticsProjectionBasis.textContent = projectionLabels[details.projection_basis] || 'Based on current usage';
    }

    const peakDay = details.peak_day;
    if (elements.analyticsPeakDayCost) {
      elements.analyticsPeakDayCost.textContent = peakDay ? formatCurrency(peakDay.cost_cached_usd) : '$0.0000';
    }
    if (elements.analyticsPeakDayDetail) {
      elements.analyticsPeakDayDetail.textContent = peakDay
        ? `${peakDay.date} · ${Number(peakDay.call_count || 0).toLocaleString()} calls`
        : 'No daily usage yet';
    }

    renderTopSessions(details.top_sessions || [], formatCurrency);

    const comparison = details.comparison;
    if (elements.comparisonSubtitle) {
      elements.comparisonSubtitle.textContent = comparison
        ? `Compared with the ${comparison.label}`
        : 'Select a preset range to compare usage';
    }
    if (!elements.comparisonContent) return;
    if (!comparison) {
      elements.comparisonContent.innerHTML = '<div class="comparison-empty">No comparison available for All time.</div>';
      return;
    }

    const comparisonRows = [
      {
        label: 'Est. cost',
        current: formatCurrency(comparison.current?.cost_cached_usd),
        previous: formatCurrency(comparison.previous?.cost_cached_usd),
        change: comparison.change_pct?.cost_cached_usd,
        lowerIsBetter: true,
      },
      {
        label: 'Tokens',
        current: formatCompactNumber(comparison.current?.total_tokens || 0),
        previous: formatCompactNumber(comparison.previous?.total_tokens || 0),
        change: comparison.change_pct?.total_tokens,
      },
      {
        label: 'API calls',
        current: Number(comparison.current?.call_count || 0).toLocaleString(),
        previous: Number(comparison.previous?.call_count || 0).toLocaleString(),
        change: comparison.change_pct?.call_count,
      },
    ];
    elements.comparisonContent.innerHTML = `
      <div class="comparison-heading">
        <span>Metric</span><span>Current</span><span>Previous</span><span>Change</span>
      </div>
      ${comparisonRows.map((row) => `
        <div class="comparison-row">
          <span class="comparison-label">${row.label}</span>
          <span class="comparison-value">${row.current}</span>
          <span class="comparison-value">${row.previous}</span>
          <span class="comparison-change ${changeClass(row.change, row.lowerIsBetter)}">${formatChange(row.change)}</span>
        </div>
      `).join('')}
    `;
  }

  /**
   * Render the five highest-cost sessions in the current view.
   */
  function renderTopSessions(sessions, formatCurrency) {
    const tbody = elements.topSessionsTableBody;
    if (!tbody) return;

    const sessionList = (Array.isArray(sessions) ? sessions : []).filter((session) => session && typeof session === 'object');
    if (sessionList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No session cost data available.</td></tr>';
      return;
    }

    tbody.innerHTML = sessionList.map((session) => {
      const tool = String(session.tool || '');
      const isCodex = tool === 'codex';
      const badgeClass = isCodex ? 'badge-codex' : 'badge-agy';
      const badgeText = isCodex ? 'Codex' : 'Antigravity';
      const activity = formatDateTime(session.activity_at || session.created_at || session.start_time);
      return `
        <tr>
          <td>
            <strong class="insight-session-title" title="${escapeHtml(String(session.title || 'Untitled Session'))}">${escapeHtml(String(session.title || 'Untitled Session'))}</strong>
            <div class="text-muted" style="font-size: 10px; font-family: var(--font-mono);">${escapeHtml(activity)}</div>
          </td>
          <td><span class="provider-badge ${badgeClass}">${badgeText}</span></td>
          <td class="cell-mono">${escapeHtml(String(session.model || 'unknown'))}</td>
          <td class="cell-mono cell-right">${Number(session.total_tokens || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right text-success">${formatCurrency(session.cost_cached_usd)}</td>
        </tr>
      `;
    }).join('');
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
      const ratesStr = `$${(rates?.uncached_input ?? 0).toFixed(2)} / $${(rates?.cached_input ?? 0).toFixed(3)} / $${(rates?.output ?? 0).toFixed(2)}`;

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
    const sessionList = (Array.isArray(state.allSessions) ? state.allSessions : []).filter(
      (s) => s && typeof s === 'object'
    );
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
      if (!s || typeof s !== 'object') return;
      const toolStr = String(s.tool || '');
      const isCodex = toolStr === 'codex';
      const badgeClass = isCodex ? 'badge-codex' : 'badge-agy';
      const badgeText = isCodex ? 'Codex' : 'Antigravity';

      const hitRate = Number(s.cache_hit_rate) || 0;
      let hitRateClass = 'hit-rate-low';
      if (hitRate >= 75) hitRateClass = 'hit-rate-high';
      else if (hitRate >= 35) hitRateClass = 'hit-rate-mid';

      const dateStr = formatDateTime(s.activity_at || s.created_at || s.start_time);
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

    const modelList = (Array.isArray(models) ? models : []).filter((m) => m && typeof m === 'object');
    const labels = modelList.map((m) => m.model);
    const uncachedData = modelList.map((m) => m.uncached_input || 0);
    const cachedData = modelList.map((m) => m.cached_input || 0);
    const outputData = modelList.map((m) => m.output || 0);
    const reasoningData = modelList.map((m) => m.reasoning_output || 0);

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

    const timelineList = (Array.isArray(timeline) ? timeline : []).filter((t) => t && typeof t === 'object');
    const sortedTimeline = [...timelineList].sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const labels = sortedTimeline.map((t) => t.date);
    const costData = sortedTimeline.map((t) => t.cost_cached_usd || 0);
    const tokenData = sortedTimeline.map((t) => t.total_tokens || 0);
    const callData = sortedTimeline.map((t) => t.call_count || 0);

    if (chartCost) {
      chartCost.data.labels = labels;
      chartCost.data.datasets[0].data = costData;
      chartCost.data.datasets[1].data = tokenData;
      if (chartCost.data.datasets[2]) {
        chartCost.data.datasets[2].data = callData;
      }
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
          {
            type: 'line',
            label: 'API Calls',
            data: callData,
            borderColor: '#f59e0b',
            backgroundColor: 'rgba(245, 158, 11, 0.15)',
            borderWidth: 2,
            fill: false,
            tension: 0.35,
            yAxisID: 'yCalls',
            pointRadius: 3,
            pointHoverRadius: 6,
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
          yCalls: {
            type: 'linear',
            position: 'right',
            offset: true,
            grid: { drawOnChartArea: false },
            ticks: {
              color: '#fbbf24',
              font: { size: 11 },
              callback: (val) => Number(val).toLocaleString(),
            },
            title: {
              display: true,
              text: 'Calls',
              color: '#fbbf24',
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
        fetchUsageData().catch((err) => console.error('Error in auto-refresh fetchUsageData:', err));
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
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on tool select:', err));
      });
    }

    // Time range dropdown switch
    if (elements.timeRangeSelect) {
      elements.timeRangeSelect.addEventListener('change', (e) => {
        state.currentTimeRange = e.target.value;
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on time range select:', err));
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
        fetchUsageData(true).catch((err) => console.error('Error on manual refresh:', err));
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
    elements.timeRangeSelect = document.getElementById('time-range-select');
    elements.autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    elements.refreshInterval = document.getElementById('refresh-interval');
    elements.refreshBtn = document.getElementById('refresh-btn');
    elements.lastSyncedBadge = document.getElementById('last-synced-badge');

    elements.metricTotalTokensValue = document.getElementById('metric-total-tokens-value');
    elements.metricTotalTokensUnit = document.getElementById('metric-total-tokens-unit');
    elements.metricCachedTokensValue = document.getElementById('metric-cached-tokens-value');
    elements.metricCachedTokensUnit = document.getElementById('metric-cached-tokens-unit');
    elements.metricOutputTokensValue = document.getElementById('metric-output-tokens-value');
    elements.metricOutputTokensUnit = document.getElementById('metric-output-tokens-unit');

    elements.cardSavingsText = document.getElementById('card-savings-text');
    elements.cardTokensSubtext = document.getElementById('card-tokens-subtext');
    elements.cardCachedShare = document.getElementById('card-cached-share');
    elements.cardReasoningSubtext = document.getElementById('card-reasoning-subtext');
    elements.cardCallsSubtext = document.getElementById('card-calls-subtext');

    elements.analyticsWindowBadge = document.getElementById('analytics-window-badge');
    elements.analyticsCallCount = document.getElementById('analytics-call-count');
    elements.analyticsAvgCost = document.getElementById('analytics-avg-cost');
    elements.analyticsAvgTokens = document.getElementById('analytics-avg-tokens');
    elements.analyticsMonthlyProjection = document.getElementById('analytics-monthly-projection');
    elements.analyticsProjectionBasis = document.getElementById('analytics-projection-basis');
    elements.analyticsPeakDayCost = document.getElementById('analytics-peak-day-cost');
    elements.analyticsPeakDayDetail = document.getElementById('analytics-peak-day-detail');
    elements.topSessionsTableBody = document.getElementById('top-sessions-table-body');
    elements.comparisonSubtitle = document.getElementById('comparison-subtitle');
    elements.comparisonContent = document.getElementById('comparison-content');

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
    try {
      await fetchPricing();
    } catch (err) {
      console.warn('Initial fetchPricing error:', err);
    }
    try {
      await fetchUsageData();
    } catch (err) {
      console.error('Initial fetchUsageData error:', err);
    }

    // Start auto-refresh timer
    setupAutoRefresh();
  }

  // Execute on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      init().catch((err) => console.error('Initialization error:', err));
    });
  } else {
    init().catch((err) => console.error('Initialization error:', err));
  }
})();
