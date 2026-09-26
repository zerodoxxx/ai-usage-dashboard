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
    customPending: false,
    autoRefreshInterval: 30000,
    refreshTimer: null,
    pricingData: null,
    currentUsageData: null,
    allSessions: [],
    searchQuery: '',
  };

  // Odometer Instances
  const odometers = {
    totalCost: null,
    burnRate: null,
    savings: null,
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

    odometers.totalCost = createOdometer('#odo-total-cost', {
      prefix: '$',
      suffix: '',
      decimals: 4,
      formatCommas: true,
      duration: 850,
    });

    odometers.burnRate = createOdometer('#odo-burn-rate', {
      prefix: '$',
      suffix: '',
      decimals: 4,
      formatCommas: true,
      duration: 850,
    });

    odometers.savings = createOdometer('#odo-savings', {
      prefix: '$',
      suffix: '',
      decimals: 4,
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
    if (state.customPending) {
      if (isUserInitiated) {
        window.DashboardUtils.showToast('Apply a custom date range before refreshing.', 'info');
      }
      return;
    }

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
      if (elements.exportSessionsBtn) {
        elements.exportSessionsBtn.disabled = state.customPending || state.allSessions.length === 0;
      }

      // Update UI components
      updateMetricCards(data.summary || {}, data.analytics || {});
      const isCustom = state.currentTimeRange === 'custom' && state.customStart;
      const rangeLabel = isCustom
        ? `${state.customStart} → ${state.customEnd || 'Up to now'}`
        : elements.timeRangeSelect?.selectedOptions?.[0]?.textContent;
      const timezoneSuffix = data.timezone ? ` · ${data.timezone}` : '';
      window.DashboardAnalytics.updateAnalytics(data.analytics || {}, data.summary || {}, {
        analyticsWindowBadge: elements.analyticsWindowBadge,
        analyticsCallCount: elements.analyticsCallCount,
        analyticsAvgCost: elements.analyticsAvgCost,
        analyticsAvgTokens: elements.analyticsAvgTokens,
        analyticsMonthlyProjection: elements.analyticsMonthlyProjection,
        analyticsProjectionBasis: elements.analyticsProjectionBasis,
        analyticsPeakDayCost: elements.analyticsPeakDayCost,
        analyticsPeakDayDetail: elements.analyticsPeakDayDetail,
        analyticsActiveDays: elements.analyticsActiveDays,
        topSessionsTableBody: elements.topSessionsTableBody,
        comparisonSubtitle: elements.comparisonSubtitle,
        comparisonContent: elements.comparisonContent,
        selectedRangeLabel: `${rangeLabel || 'Selected range'}${timezoneSuffix}`,
        timezone: data.timezone || '',
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
        timezone: data.timezone || '',
      });
      window.DashboardCharts.updateCharts(data, {
        tokensCanvas: elements.chartTokensCanvas,
        costCanvas: elements.chartCostCanvas,
        costByToolCanvas: elements.chartCostByToolCanvas,
        cacheTrendCanvas: elements.chartCacheTrendCanvas,
        costPer1kCanvas: elements.chartCostPer1kCanvas,
        hourlyActivityCanvas: elements.chartHourlyActivityCanvas,
        weekdayHeatmap: elements.weekdayHeatmap,
        sparklineSpend: elements.sparklineSpend,
        sparklineBurn: elements.sparklineBurn,
        sparklineSavings: elements.sparklineSavings,
        sparklineSessions: elements.sparklineSessions,
      });

      // Update last synced badge in the same timezone used by the API.
      const synced = window.DashboardUtils.formatDateTime(
        new Date().toISOString(),
        data.timezone || '',
      );
      const timeStr = synced.includes(' ') ? synced.slice(11) : synced;
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
   * Update the 4 executive overview cards and their subtexts
   */
  function updateMetricCards(summary, analytics) {
    const totals = summary && typeof summary === 'object' ? summary : {};
    const details = analytics && typeof analytics === 'object' ? analytics : {};
    const { formatCompactNumber } = window.DashboardUtils;
    const totalTokens = Number(totals.total_tokens || 0);
    const cacheWriteTokens = Number(totals.cache_write || 0);
    const totalCost = Number(totals.cost_cached_usd || 0);
    const uncachedCost = Number(totals.cost_uncached_usd || 0);
    const cacheHitRate = Number(totals.cache_hit_rate || 0);
    const sessionCount = Number(totals.session_count || 0);
    const savings = Number(totals.savings_usd || 0);
    const callCount = Number(totals.call_count || 0);
    const unpricedCount = Number(totals.unpriced_model_count || 0);
    const burn = Number(details.projected_30d_usd ?? details.monthly_projection_usd ?? 0);

    if (elements.unpricedSummaryBanner) {
      const hasUnpriced = Number.isFinite(unpricedCount) && unpricedCount > 0;
      elements.unpricedSummaryBanner.hidden = !hasUnpriced;
      elements.unpricedSummaryBanner.textContent = hasUnpriced
        ? `${unpricedCount.toLocaleString()} model${unpricedCount === 1 ? '' : 's'} lack a usable catalog rate. Spend totals are partial; see the Models tab for details.`
        : '';
    }

    if (odometers.totalCost) odometers.totalCost.update(Number.isFinite(totalCost) ? totalCost : 0);
    if (odometers.burnRate) odometers.burnRate.update(Number.isFinite(burn) ? burn : 0);
    if (odometers.savings) odometers.savings.update(Number.isFinite(savings) ? savings : 0);
    if (odometers.sessions) odometers.sessions.update(Number.isFinite(sessionCount) ? sessionCount : 0);

    if (elements.cardSpendSubtext) {
      let text = `${formatCompactNumber(totalTokens)} tokens in range`;
      if (Number.isFinite(uncachedCost) && uncachedCost > 0 && Math.abs(uncachedCost - totalCost) > 0.00005) {
        text += ` · $${totalCost.toFixed(2)} cached vs $${uncachedCost.toFixed(2)} uncached`;
      }
      if (Number.isFinite(cacheWriteTokens) && cacheWriteTokens > 0) {
        text += ` · ${formatCompactNumber(cacheWriteTokens)} cache-write`;
      }
      if (unpricedCount > 0) text += ' · partial cost';
      elements.cardSpendSubtext.textContent = text;
    }
    if (elements.cardBurnSubtext) {
      const labelFn = window.DashboardAnalytics && window.DashboardAnalytics.projectionBasisLabel;
      const projectionLabel = labelFn
        ? labelFn(details.projection_basis)
        : 'Filter daily average × 30';
      elements.cardBurnSubtext.textContent = unpricedCount > 0
        ? `${projectionLabel} · partial cost`
        : projectionLabel;
    }
    if (elements.cardSavingsSubtext) {
      const rate = Number.isFinite(cacheHitRate) ? cacheHitRate : 0;
      elements.cardSavingsSubtext.textContent = `${rate.toFixed(1)}% cache hit rate`;
    }
    if (elements.cardCallsSubtext) {
      elements.cardCallsSubtext.textContent = `${Number(callCount || 0).toLocaleString()} API calls recorded`;
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
    if (elements.customEndControl) elements.customEndControl.hidden = flag;
    if (elements.customRangeApply) elements.customRangeApply.hidden = flag;
  }

  /** Mark the visible custom range as a draft, or clear the draft marker. */
  function setCustomPending(pending) {
    state.customPending = Boolean(pending);
    if (elements.exportSessionsBtn) {
      elements.exportSessionsBtn.disabled = state.customPending || state.allSessions.length === 0;
    }
  }

  /** Show that a blank custom end date means the range ends at the current time. */
  function updateCustomEndHint() {
    if (elements.customEndNowLabel && elements.customEndDate) {
      elements.customEndNowLabel.hidden = Boolean(elements.customEndDate.value);
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
        const isCustom = state.currentTimeRange === 'custom';
        setCustomPending(isCustom);
        setCustomControlsVisible(isCustom);
        if (isCustom) {
          // Wait for explicit Apply; keep last custom dates in the inputs.
          updateCustomEndHint();
          return;
        }
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on time range select:', err));
      });
    }

    // Custom date-range apply
    if (elements.customStartDate) {
      elements.customStartDate.addEventListener('change', () => {
        // A new start date begins a fresh draft range ending at now.
        setCustomPending(true);
        if (elements.customEndDate) elements.customEndDate.value = '';
        updateCustomEndHint();
      });
    }

    if (elements.customEndDate) {
      elements.customEndDate.addEventListener('change', () => {
        setCustomPending(true);
        updateCustomEndHint();
      });
    }

    if (elements.customRangeApply) {
      elements.customRangeApply.addEventListener('click', () => {
        const start = elements.customStartDate ? elements.customStartDate.value : '';
        const end = elements.customEndDate ? elements.customEndDate.value : '';
        if (!start) {
          window.DashboardUtils.showToast('Select a start date.', 'error');
          return;
        }
        if (end && start > end) {
          window.DashboardUtils.showToast('Start date must be on or before the end date.', 'error');
          return;
        }
        state.customStart = start;
        state.customEnd = end;
        setCustomPending(false);
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

    // Export the active session set (including the active search filter)
    if (elements.exportSessionsBtn) {
      elements.exportSessionsBtn.addEventListener('click', () => {
        const rangePart = state.currentTimeRange === 'custom'
          ? `${state.customStart || 'start'}_to_${state.customEnd || 'now'}`
          : state.currentTimeRange;
        const filename = `ai-usage-${state.currentTool}-${rangePart}.csv`
          .replace(/[^a-z0-9._-]+/gi, '-');
        const exported = window.DashboardTables.exportSessionsCsv(
          state.allSessions,
          state.searchQuery,
          filename,
          state.currentUsageData?.timezone || '',
        );
        window.DashboardUtils.showToast(
          exported > 0 ? `Exported ${exported.toLocaleString()} session${exported === 1 ? '' : 's'}.` : 'No sessions to export.',
          'info',
        );
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

    setupDetailsTabs();
    document.querySelectorAll('.chart-group').forEach((group) => {
      group.addEventListener('toggle', () => {
        if (group.open && window.DashboardCharts && window.DashboardCharts.resizeCharts) {
          window.DashboardCharts.resizeCharts();
        }
      });
    });
  }

  /**
   * Models / Sessions / Insights tablist with roving tabindex.
   */
  function setupDetailsTabs() {
    const tabs = Array.from(document.querySelectorAll('.details-tab'));
    if (tabs.length === 0) return;

    const selectTab = (tab) => {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute('aria-selected', selected ? 'true' : 'false');
        item.tabIndex = selected ? 0 : -1;
        const panel = document.getElementById(item.getAttribute('aria-controls'));
        if (panel) panel.hidden = !selected;
      });
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => selectTab(tab));
      tab.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft' && event.key !== 'Home' && event.key !== 'End') {
          return;
        }
        event.preventDefault();
        let nextIndex = index;
        if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
        if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'Home') nextIndex = 0;
        if (event.key === 'End') nextIndex = tabs.length - 1;
        const next = tabs[nextIndex];
        selectTab(next);
        next.focus();
      });
    });
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
    elements.customEndControl = document.getElementById('custom-end-control');
    elements.customEndNowLabel = document.getElementById('custom-end-now-label');
    elements.customRangeApply = document.getElementById('custom-range-apply');
    elements.autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    elements.refreshInterval = document.getElementById('refresh-interval');
    elements.refreshBtn = document.getElementById('refresh-btn');
    elements.exportSessionsBtn = document.getElementById('export-sessions-btn');
    elements.lastSyncedBadge = document.getElementById('last-synced-badge');

    elements.cardSpendSubtext = document.getElementById('card-spend-subtext');
    elements.cardBurnSubtext = document.getElementById('card-burn-subtext');
    elements.cardSavingsSubtext = document.getElementById('card-savings-subtext');
    elements.cardCallsSubtext = document.getElementById('card-calls-subtext');
    elements.sparklineSpend = document.getElementById('sparkline-spend');
    elements.sparklineBurn = document.getElementById('sparkline-burn');
    elements.sparklineSavings = document.getElementById('sparkline-savings');
    elements.sparklineSessions = document.getElementById('sparkline-sessions');

    elements.analyticsWindowBadge = document.getElementById('analytics-window-badge');
    elements.analyticsCallCount = document.getElementById('analytics-call-count');
    elements.analyticsAvgCost = document.getElementById('analytics-avg-cost');
    elements.analyticsAvgTokens = document.getElementById('analytics-avg-tokens');
    elements.analyticsMonthlyProjection = document.getElementById('analytics-monthly-projection');
    elements.analyticsProjectionBasis = document.getElementById('analytics-projection-basis');
    elements.analyticsPeakDayCost = document.getElementById('analytics-peak-day-cost');
    elements.analyticsPeakDayDetail = document.getElementById('analytics-peak-day-detail');
    elements.analyticsActiveDays = document.getElementById('analytics-active-days');
    elements.topSessionsTableBody = document.getElementById('top-sessions-table-body');
    elements.comparisonSubtitle = document.getElementById('comparison-subtitle');
    elements.comparisonContent = document.getElementById('comparison-content');

    elements.chartTokensCanvas = document.getElementById('chart-tokens');
    elements.chartCostCanvas = document.getElementById('chart-cost');
    elements.chartCostByToolCanvas = document.getElementById('chart-cost-by-tool');
    elements.chartCacheTrendCanvas = document.getElementById('chart-cache-trend');
    elements.chartCostPer1kCanvas = document.getElementById('chart-cost-per-1k');
    elements.chartHourlyActivityCanvas = document.getElementById('chart-hourly-activity');
    elements.weekdayHeatmap = document.getElementById('weekday-hour-heatmap');
    elements.unpricedBanner = document.getElementById('unpriced-banner');
    elements.unpricedSummaryBanner = document.getElementById('unpriced-summary-banner');

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
