/**
 * AI Tools Usage & Cost Visualizer - Thin Orchestrator
 * Owns state, odometer-backed metric cards, event wiring, polling timer,
 * and UI dispatch. Data fetching lives in api.js, rendering in
 * charts.js / tables.js / analytics.js, shared helpers in utils.js.
 *
 * Load order (see index.html): odometer.js, utils.js, api.js, charts.js,
 * tables.js, analytics.js, then this file.
 */
(function () {
  'use strict';

  // Global Dashboard State
  const state = {
    currentTool: 'all',
    currentTimeRange: 'all',
    customStart: '',
    customEnd: '',
    autoRefreshInterval: 30000,
    refreshTimer: null,
    pricingData: null,
    currentUsageData: null,
    allSessions: [],
    searchQuery: '',
  };

  // Odometer Instances
  const odometers = {
    totalTokens: null,
    totalCost: null,
    cachedTokens: null,
    cacheRate: null,
    outputTokens: null,
    sessions: null,
  };

  // DOM Elements cache
  const elements = {};

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
   * Fetch reference pricing table and update the status badge.
   */
  async function fetchPricing() {
    try {
      state.pricingData = await window.DashboardApi.fetchPricing();
      window.DashboardApi.updatePricingStatus(elements.pricingStatus, state.pricingData.__meta__ || {});
    } catch (e) {
      console.warn('Failed to load pricing table:', e);
      window.DashboardApi.updatePricingStatus(elements.pricingStatus, { source: 'unavailable', stale: true, error: e.message });
    }
  }

  /**
   * Fetch usage data from /api/usage?tool=...&time_range=...[&start=...&end=...]
   * @param {boolean} [isUserInitiated=false]
   */
  async function fetchUsageData(isUserInitiated = false) {
    const refreshIcon = elements.refreshBtn ? elements.refreshBtn.querySelector('.refresh-icon') : null;
    if (refreshIcon) refreshIcon.classList.add('spin');

    try {
      const result = await window.DashboardApi.requestUsage(
        state.currentTool,
        state.currentTimeRange,
        isUserInitiated,
        { start: state.customStart, end: state.customEnd },
      );
      if (result.status !== 'ok') {
        // 'skipped' (background poll while fetching) or 'aborted'
        // (superseded by a newer user-initiated fetch): silently exit.
        return;
      }

      const data = result.data;
      state.currentUsageData = data;
      state.allSessions = Array.isArray(data.sessions) ? data.sessions : [];

      // Update UI components
      updateMetricCards(data.summary || {});
      const isCustom = state.currentTimeRange === 'custom'
        && state.customStart && state.customEnd;
      window.DashboardAnalytics.updateAnalytics(data.analytics || {}, data.summary || {}, {
        analyticsWindowBadge: elements.analyticsWindowBadge,
        analyticsCallCount: elements.analyticsCallCount,
        analyticsAvgCost: elements.analyticsAvgCost,
        analyticsAvgTokens: elements.analyticsAvgTokens,
        analyticsMonthlyProjection: elements.analyticsMonthlyProjection,
        analyticsProjectionBasis: elements.analyticsProjectionBasis,
        analyticsPeakDayCost: elements.analyticsPeakDayCost,
        analyticsPeakDayDetail: elements.analyticsPeakDayDetail,
        topSessionsTableBody: elements.topSessionsTableBody,
        comparisonSubtitle: elements.comparisonSubtitle,
        comparisonContent: elements.comparisonContent,
        selectedRangeLabel: isCustom
          ? `${state.customStart} → ${state.customEnd}`
          : elements.timeRangeSelect?.selectedOptions?.[0]?.textContent,
      });
      window.DashboardTables.renderModelTable(data.models || [], {
        tbody: elements.modelsTableBody,
        countBadge: elements.modelsCountBadge,
        pricingData: state.pricingData,
        unpricedBanner: elements.unpricedBanner,
      });
      window.DashboardTables.renderSessionsTable(state.allSessions, state.searchQuery, {
        tbody: elements.sessionsTableBody,
        countBadge: elements.sessionsCountBadge,
      });
      window.DashboardCharts.updateCharts(data, {
        tokensCanvas: elements.chartTokensCanvas,
        costCanvas: elements.chartCostCanvas,
        costByToolCanvas: elements.chartCostByToolCanvas,
        cacheTrendCanvas: elements.chartCacheTrendCanvas,
        costPer1kCanvas: elements.chartCostPer1kCanvas,
        hourlyActivityCanvas: elements.chartHourlyActivityCanvas,
      });

      // Update last synced badge
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const timeStr = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
      if (elements.lastSyncedBadge) {
        elements.lastSyncedBadge.textContent = `Synced ${timeStr}`;
      }
    } catch (err) {
      console.error('Failed to fetch usage metrics:', err);
      window.DashboardUtils.showToast(`Sync failed: ${err.message}`, 'error');
      if (elements.lastSyncedBadge) {
        elements.lastSyncedBadge.textContent = 'Sync error';
      }
    } finally {
      if (refreshIcon) {
        setTimeout(() => refreshIcon.classList.remove('spin'), 400);
      }
    }
  }

  /**
   * Update the 6 metric cards and their subtexts
   */
  function updateMetricCards(summary) {
    const { updateCompactMetric } = window.DashboardUtils;
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
      if (state.currentTimeRange === 'all') {
        elements.cardTokensSubtext.textContent = 'Cumulative audit';
      } else if (state.currentTimeRange === 'custom' && state.customStart && state.customEnd) {
        elements.cardTokensSubtext.textContent = `${state.customStart} → ${state.customEnd} usage`;
      } else {
        const selectedOption = elements.timeRangeSelect?.selectedOptions?.[0];
        elements.cardTokensSubtext.textContent = `${selectedOption?.textContent || 'Selected range'} usage`;
      }
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
   * Show or hide the custom date-range inputs next to the time-range select.
   */
  function setCustomControlsVisible(visible) {
    const flag = !visible;
    if (elements.customStartDate) elements.customStartDate.hidden = flag;
    if (elements.customEndDate) elements.customEndDate.hidden = flag;
    if (elements.customRangeApply) elements.customRangeApply.hidden = flag;
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
        const isCustom = state.currentTimeRange === 'custom';
        setCustomControlsVisible(isCustom);
        if (isCustom) {
          // Wait for explicit Apply; keep last custom dates in the inputs.
          return;
        }
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on time range select:', err));
      });
    }

    // Custom date-range apply
    if (elements.customRangeApply) {
      elements.customRangeApply.addEventListener('click', () => {
        const start = elements.customStartDate ? elements.customStartDate.value : '';
        const end = elements.customEndDate ? elements.customEndDate.value : '';
        if (!start || !end) {
          window.DashboardUtils.showToast('Select both a start and an end date.', 'error');
          return;
        }
        if (start > end) {
          window.DashboardUtils.showToast('Start date must be on or before the end date.', 'error');
          return;
        }
        state.customStart = start;
        state.customEnd = end;
        state.currentTimeRange = 'custom';
        if (elements.timeRangeSelect) elements.timeRangeSelect.value = 'custom';
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on custom range apply:', err));
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
        window.DashboardTables.renderSessionsTable(state.allSessions, state.searchQuery, {
          tbody: elements.sessionsTableBody,
          countBadge: elements.sessionsCountBadge,
        });
      });
    }
  }

  /**
   * Cache DOM elements for fast access.
   * IDs preserved from the original monolith; renderers receive
   * the elements they need as explicit arguments.
   */
  function cacheElements() {
    elements.toolSelect = document.getElementById('tool-select');
    elements.timeRangeSelect = document.getElementById('time-range-select');
    elements.customStartDate = document.getElementById('custom-start-date');
    elements.customEndDate = document.getElementById('custom-end-date');
    elements.customRangeApply = document.getElementById('custom-range-apply');
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
    elements.chartCostByToolCanvas = document.getElementById('chart-cost-by-tool');
    elements.chartCacheTrendCanvas = document.getElementById('chart-cache-trend');
    elements.chartCostPer1kCanvas = document.getElementById('chart-cost-per-1k');
    elements.chartHourlyActivityCanvas = document.getElementById('chart-hourly-activity');
    elements.unpricedBanner = document.getElementById('unpriced-banner');

    elements.modelsCountBadge = document.getElementById('models-count-badge');
    elements.pricingStatus = document.getElementById('pricing-status');
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
