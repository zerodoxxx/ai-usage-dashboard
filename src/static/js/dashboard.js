/**
 * AI Tools Usage & Cost Visualizer - Thin Orchestrator
 * Owns state, the drum register and readout, event wiring, polling timer,
 * theme toggle and UI dispatch. Data fetching lives in api.js, rendering in
 * charts.js / tables.js / analytics.js, shared helpers in utils.js.
 *
 * Load order (see index.html): odometer.js, utils.js, api.js, charts.js,
 * tables.js, analytics.js, then this file.
 */
(function () {
  'use strict';

  const THEME_KEY = 'aiud.theme';

  const RANGE_PHRASES = {
    all: 'all time',
    month: 'this month',
    '30d': 'past 30 days',
    '7d': 'past 7 days',
    '24h': 'past 24 hours',
  };

  const PROJECTION_PHRASES = {
    all_run_rate: "Projected from the all-time daily average",
    last_30_days: "Projected from the all-time daily average",
    current_month_run_rate: "Projected from this month's daily average",
    custom_run_rate: "Projected from the selected range's daily average",
    '30d_run_rate': "Projected from the past 30 days' daily average",
    '7d_run_rate': "Projected from the past 7 days' daily average",
    '24h_run_rate': "Projected from the past 24 hours' daily average",
  };
  const PROJECTION_DEFAULT = "Projected from this period's daily average";

  const EMPTY_VALUE = '–';
  const NO_USAGE = 'No usage in this period';
  let hasRenderedOnce = false;

  // Global Dashboard State
  const state = {
    currentTool: 'all',
    currentTimeRange: 'all',
    // The range the on-screen data reflects; a dismissed custom draft reverts to it.
    appliedTimeRange: 'all',
    customStart: '',
    customEnd: '',
    customPending: false,
    customDraftTool: null,
    autoRefreshInterval: 30000,
    refreshTimer: null,
    pricingData: null,
    currentUsageData: null,
    allSessions: [],
    searchQuery: '',
    usageRequestGeneration: 0,
    activeUsageRequest: null,
  };

  // The drum register is the only odometer left.
  const odometers = {
    totalCost: null,
  };

  // DOM Elements cache
  const elements = {};

  function prefersReducedMotion() {
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /**
   * Initialize the drum register defensively
   */
  function initOdometers() {
    try {
      const el = document.getElementById('odo-total-cost');
      if (!el || typeof RollingOdometer === 'undefined') return;
      odometers.totalCost = new RollingOdometer({
        element: el,
        prefix: '$',
        decimals: 2,
        register: true,
        minIntegerDigits: 4,
        duration: 850,
      });
    } catch (err) {
      console.warn('Failed to initialize the register:', err);
    }
  }

  /* ------------------------------------------------------------- theme */

  function effectiveTheme() {
    const forced = document.documentElement.getAttribute('data-theme');
    if (forced === 'light' || forced === 'dark') return forced;
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }

  function syncThemeButton() {
    const btn = elements.themeToggle;
    if (!btn) return;
    const label = `Switch to ${effectiveTheme() === 'dark' ? 'light' : 'dark'} theme`;
    btn.setAttribute('aria-label', label);
    btn.title = label;
  }

  function toggleTheme() {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (err) {
      // Storage can be blocked; the choice still applies for this page view.
    }
    syncThemeButton();
    document.dispatchEvent(new CustomEvent('themechange'));
  }

  function setupTheme() {
    syncThemeButton();
    if (elements.themeToggle) elements.themeToggle.addEventListener('click', toggleTheme);
    if (typeof window.matchMedia === 'function') {
      const query = window.matchMedia('(prefers-color-scheme: dark)');
      const onChange = () => syncThemeButton();
      if (query.addEventListener) query.addEventListener('change', onChange);
      else if (query.addListener) query.addListener(onChange);
    }
  }

  /* -------------------------------------------------------- range text */

  function formatRangeDay(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!match) return String(iso || '');
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    const sameYear = date.getFullYear() === new Date().getFullYear();
    return date.toLocaleDateString('en-US', sameYear
      ? { month: 'short', day: 'numeric' }
      : { month: 'short', day: 'numeric', year: 'numeric' });
  }

  /** Lower-case phrase for the register label ("past 30 days", "Sep 1 – Sep 12"). */
  function describeRange(range, start, end) {
    if (range === 'custom' && start) {
      return `${formatRangeDay(start)} – ${end ? formatRangeDay(end) : 'now'}`;
    }
    return RANGE_PHRASES[range] || 'selected range';
  }

  /** Title-case label for the insights window ("Past 30 days", "Sep 1 – Sep 12"). */
  function describeRangeLabel(range, start, end) {
    if (range === 'custom' && start) return describeRange(range, start, end);
    const option = elements.timeRangeSelect && Array.from(elements.timeRangeSelect.options)
      .find((item) => item.value === range);
    return option ? option.textContent : 'Selected range';
  }

  /* -------------------------------------------------------- sync badge */

  function setSyncState(mode, timeText) {
    const badge = elements.lastSyncedBadge;
    if (!badge) return;
    badge.dataset.state = mode;
    const text = badge.querySelector('.sync__text') || badge;
    const label = mode === 'loading' ? 'Updating…'
      : mode === 'error' ? 'Update failed' : `Updated ${timeText}`;
    if (text.textContent !== label) text.textContent = label;
  }

  function pulseLiveDot() {
    const dot = elements.lastSyncedBadge && elements.lastSyncedBadge.querySelector('.live-dot');
    if (!dot || prefersReducedMotion()) return;
    dot.classList.remove('is-pulsing');
    void dot.offsetWidth; // restart the one-shot animation
    dot.classList.add('is-pulsing');
  }

  /** Only the latest issued usage request owns data and status updates. */
  function isCurrentUsageRequest(generation) {
    return generation === state.usageRequestGeneration;
  }

  /** Fetch reference pricing without letting a superseded request change its badge. */
  async function fetchPricing(generation = state.usageRequestGeneration) {
    try {
      const pricingData = await window.DashboardApi.fetchPricing();
      if (!isCurrentUsageRequest(generation)) return;
      state.pricingData = pricingData;
      window.DashboardApi.updatePricingStatus(elements.pricingStatus, state.pricingData.__meta__ || {});
    } catch (e) {
      if (!isCurrentUsageRequest(generation)) return;
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

    // A skipped poll must not supersede the request whose result is still pending.
    // Keep the guard through pricing and rendering, while user requests may abort it.
    if (!isUserInitiated && state.activeUsageRequest !== null) return;

    const generation = ++state.usageRequestGeneration;
    state.activeUsageRequest = generation;
    const request = {
      tool: state.currentTool,
      timeRange: state.appliedTimeRange,
      start: state.customStart,
      end: state.customEnd,
    };

    const refreshIcon = elements.refreshBtn ? elements.refreshBtn.querySelector('.refresh-icon') : null;
    if (refreshIcon) {
      refreshIcon.classList.remove('spin');
      void refreshIcon.getBoundingClientRect(); // restart the rotation
      refreshIcon.classList.add('spin');
    }
    setSyncState('loading');

    try {
      const result = await window.DashboardApi.requestUsage(
        request.tool,
        request.timeRange,
        isUserInitiated,
        { start: request.start, end: request.end },
      );
      if (!isCurrentUsageRequest(generation)) return;
      if (result.status !== 'ok') {
        // 'skipped' (background poll while fetching) or 'aborted'
        // (superseded by a newer user-initiated fetch): silently exit.
        return;
      }

      await fetchPricing(generation);

      if (!isCurrentUsageRequest(generation)) return;

      const data = result.data;
      const ctx = {
        boot: !hasRenderedOnce,
        queryKey: JSON.stringify([request.tool, request.timeRange, request.start, request.end]),
        tool: request.tool,
        timeRange: request.timeRange,
      };
      state.currentUsageData = data;
      state.allSessions = Array.isArray(data.sessions) ? data.sessions : [];

      // Update UI components
      updateMeter(data.summary || {}, data.analytics || {}, request, ctx);
      const rangeLabel = describeRangeLabel(request.timeRange, request.start, request.end);
      const timezoneSuffix = data.timezone ? ` (${data.timezone})` : '';
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
        selectedRangeLabel: `${rangeLabel}${timezoneSuffix}`,
        timezone: data.timezone || '',
      });
      window.DashboardTables.renderModelLedger(data.models || [], elements.modelLedger);
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
      try {
        window.DashboardCharts.updateCharts(data, ctx);
      } catch (chartErr) {
        // A chart failure must not take down the meter, ledger and tables.
        console.error('Failed to render charts:', chartErr);
      }

      if (window.DashboardMilestones && typeof window.DashboardMilestones.update === 'function') {
        try {
          window.DashboardMilestones.update(data, ctx);
        } catch (featureErr) {
          console.error('Failed to update milestones:', featureErr);
        }
      }
      if (window.DashboardReceipt && typeof window.DashboardReceipt.update === 'function') {
        try {
          window.DashboardReceipt.update(data, ctx);
        } catch (featureErr) {
          console.error('Failed to update receipt:', featureErr);
        }
      }
      if (window.DashboardSkyline && typeof window.DashboardSkyline.update === 'function') {
        try {
          window.DashboardSkyline.update(data, ctx);
        } catch (featureErr) {
          console.error('Failed to update skyline:', featureErr);
        }
      }
      if (window.DashboardLinked && typeof window.DashboardLinked.update === 'function') {
        try {
          window.DashboardLinked.update(data, ctx);
        } catch (featureErr) {
          console.error('Failed to update linked views:', featureErr);
        }
      }

      // Update last synced badge in the same timezone used by the API.
      const synced = window.DashboardUtils.formatDateTime(
        new Date().toISOString(),
        data.timezone || '',
      );
      const timeStr = synced.includes(' ') ? synced.slice(11, 16) : synced;
      setSyncState('ok', timeStr);
      pulseLiveDot();
      hasRenderedOnce = true;
    } catch (err) {
      if (!isCurrentUsageRequest(generation)) return;
      console.warn('Failed to fetch usage metrics:', err);
      window.DashboardUtils.showToast("Couldn't load usage data. Check that the server is running, then refresh.", 'error');
      setSyncState('error');
    } finally {
      if (state.activeUsageRequest === generation) state.activeUsageRequest = null;
    }
  }

  /* -------------------------------------------------- meter and readout */

  function projectionSubtext(basis) {
    const text = typeof basis === 'string' ? basis.trim() : '';
    if (text && /\s/.test(text)) return text;
    return PROJECTION_PHRASES[text] || PROJECTION_DEFAULT;
  }

  function setReadoutRow(valueEl, subEl, value, sub, isEmpty) {
    if (valueEl) valueEl.textContent = value;
    if (subEl) subEl.textContent = sub;
    const row = valueEl && valueEl.closest('.readout__row');
    if (row) row.classList.toggle('is-empty', Boolean(isEmpty));
  }

  /**
   * Update the drum register and the four readout rows. Only the register rolls;
   * the readout values are plain text.
   */
  function updateMeter(summary, analytics, request, ctx) {
    const totals = summary && typeof summary === 'object' ? summary : {};
    const details = analytics && typeof analytics === 'object' ? analytics : {};
    const { formatUsd, formatRate, formatInt, formatCompactSig } = window.DashboardUtils;
    const num = (value) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };

    const totalTokens = num(totals.total_tokens);
    const totalCost = num(totals.cost_cached_usd);
    const uncachedCost = num(totals.cost_uncached_usd);
    const savings = num(totals.savings_usd);
    const cacheHitRate = num(totals.cache_hit_rate);
    const sessionCount = num(totals.session_count);
    const callCount = num(totals.call_count);
    const unpricedCount = num(totals.unpriced_model_count);
    const burn = num(details.projected_30d_usd ?? details.monthly_projection_usd);
    const hasUsage = totalTokens > 0 || callCount > 0;

    if (elements.unpricedSummaryBanner) {
      const hasUnpriced = unpricedCount > 0;
      const banner = elements.unpricedSummaryBanner;
      banner.hidden = !hasUnpriced;
      banner.textContent = '';
      if (hasUnpriced) {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.textContent = 'Unpriced';
        const text = document.createElement('span');
        text.textContent = `${formatInt(unpricedCount)} model${unpricedCount === 1 ? '' : 's'} lack a usable catalog rate. Spend totals are partial; see the Models tab for details.`;
        banner.append(chip, text);
      }
    }

    if (elements.meterLabel) {
      elements.meterLabel.textContent = `API value, ${describeRange(request.timeRange, request.start, request.end)}`;
    }
    if (odometers.totalCost) {
      if (ctx && ctx.boot) {
        if (typeof odometers.totalCost.boot === 'function') odometers.totalCost.boot(totalCost);
        else odometers.totalCost.update(totalCost);
      } else {
        odometers.totalCost.update(totalCost);
      }
    }

    if (elements.cardSpendSubtext) {
      const lines = ['What this usage would cost at pay-as-you-go API rates.'];
      if (!hasUsage) {
        lines.push(`${NO_USAGE}.`);
      } else if (savings > 0.005 && uncachedCost > 0) {
        lines.push(`Without caching ${formatUsd(uncachedCost)}. Caching saved ${formatUsd(savings)}.`);
      } else if (uncachedCost > 0) {
        lines.push(`Without caching ${formatUsd(uncachedCost)}.`);
      }
      if (unpricedCount > 0) lines.push('Some models have no catalog rate, so this total is partial.');
      elements.cardSpendSubtext.textContent = '';
      lines.forEach((line) => {
        const p = document.createElement('p');
        p.textContent = line;
        elements.cardSpendSubtext.appendChild(p);
      });
    }

    const burnSub = projectionSubtext(details.projection_basis);
    setReadoutRow(
      elements.readoutBurn,
      elements.cardBurnSubtext,
      formatUsd(burn, 2),
      unpricedCount > 0 ? `${burnSub}, partial cost` : burnSub,
      false,
    );

    setReadoutRow(
      elements.readoutTokens,
      elements.cardTokensSubtext,
      formatCompactSig(totalTokens),
      totalTokens > 0 ? `${cacheHitRate.toFixed(1)}% read from cache` : NO_USAGE,
      false,
    );

    if (totalTokens > 0) {
      setReadoutRow(
        elements.readoutRate,
        elements.cardRateSubtext,
        formatRate((totalCost / totalTokens) * 1e6),
        `${formatRate((uncachedCost / totalTokens) * 1e6)} per 1M without caching`,
        false,
      );
    } else {
      setReadoutRow(elements.readoutRate, elements.cardRateSubtext, EMPTY_VALUE, NO_USAGE, true);
    }

    setReadoutRow(
      elements.readoutCalls,
      elements.cardCallsSubtext,
      formatInt(callCount),
      `${formatInt(sessionCount)} session${sessionCount === 1 ? '' : 's'}`,
      false,
    );
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

  /* ------------------------------------------------ custom range popover */

  function isRangePopoverOpen() {
    return Boolean(elements.rangePopover && !elements.rangePopover.hidden);
  }

  function openRangePopover() {
    if (!elements.rangePopover) return;
    if (!isRangePopoverOpen()) state.customDraftTool = state.currentTool;
    elements.rangePopover.hidden = false;
    updateCustomEndHint();
    if (elements.customStartDate) elements.customStartDate.focus();
  }

  function closeRangePopover(restoreFocus) {
    if (!isRangePopoverOpen()) return;
    elements.rangePopover.hidden = true;
    state.customDraftTool = null;
    if (restoreFocus && elements.timeRangeSelect) elements.timeRangeSelect.focus();
  }

  /**
   * Close an unapplied custom draft and put the selector back on the range the
   * dashboard is actually showing.
   */
  function dismissRangePopover(restoreFocus) {
    if (!isRangePopoverOpen()) return;
    const toolChanged = state.customPending && state.currentTool !== state.customDraftTool;
    if (state.customPending) {
      setCustomPending(false);
      state.currentTimeRange = state.appliedTimeRange;
      if (elements.timeRangeSelect) elements.timeRangeSelect.value = state.appliedTimeRange;
    }
    closeRangePopover(restoreFocus);
    if (toolChanged) {
      fetchUsageData(true).catch((err) => console.error('Error fetching usage data after dismissing a custom draft:', err));
    }
  }

  function syncCustomRangeEdit() {
    if (elements.customRangeEdit) elements.customRangeEdit.hidden = state.appliedTimeRange !== 'custom';
  }

  /** Mark the visible custom range as a draft, or clear the draft marker. */
  function setCustomPending(pending) {
    state.customPending = Boolean(pending);
  }

  /** Show that a blank custom end date means the range ends at the current time. */
  function updateCustomEndHint() {
    if (elements.customEndNowLabel && elements.customEndDate) {
      elements.customEndNowLabel.hidden = Boolean(elements.customEndDate.value);
    }
  }

  /* ---------------------------------------------------------- tool filter */

  /**
   * Tool radiogroup: roving tabindex, arrow keys move and select.
   */
  function setupToolFilter() {
    const group = elements.toolFilter;
    if (!group) return;
    const radios = Array.from(group.querySelectorAll('[role="radio"]'));
    if (radios.length === 0) return;

    const select = (radio, focus) => {
      radios.forEach((item) => {
        const on = item === radio;
        item.setAttribute('aria-checked', on ? 'true' : 'false');
        item.tabIndex = on ? 0 : -1;
      });
      if (focus) radio.focus();
      const tool = radio.getAttribute('data-tool') || 'all';
      if (tool === state.currentTool) return;
      state.currentTool = tool;
      fetchUsageData(true).catch((err) => console.error('Error fetching usage data on tool change:', err));
    };

    radios.forEach((radio, index) => {
      radio.addEventListener('click', () => select(radio, false));
      radio.addEventListener('keydown', (event) => {
        let nextIndex = index;
        switch (event.key) {
          case 'ArrowRight':
          case 'ArrowDown':
            nextIndex = (index + 1) % radios.length;
            break;
          case 'ArrowLeft':
          case 'ArrowUp':
            nextIndex = (index - 1 + radios.length) % radios.length;
            break;
          case 'Home':
            nextIndex = 0;
            break;
          case 'End':
            nextIndex = radios.length - 1;
            break;
          default:
            return;
        }
        event.preventDefault();
        select(radios[nextIndex], true);
      });
    });
  }

  /**
   * Attach all DOM event listeners
   */
  function setupEventListeners() {
    setupToolFilter();

    // Time range dropdown switch
    if (elements.timeRangeSelect) {
      elements.timeRangeSelect.addEventListener('change', (e) => {
        state.currentTimeRange = e.target.value;
        const isCustom = state.currentTimeRange === 'custom';
        setCustomPending(isCustom);
        if (isCustom) {
          // Wait for explicit Apply; keep last custom dates in the inputs.
          openRangePopover();
          return;
        }
        state.appliedTimeRange = state.currentTimeRange;
        syncCustomRangeEdit();
        closeRangePopover(false);
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on time range select:', err));
      });
    }

    if (elements.customRangeEdit) {
      elements.customRangeEdit.addEventListener('click', () => {
        if (state.appliedTimeRange !== 'custom') return;
        state.currentTimeRange = 'custom';
        if (elements.timeRangeSelect) elements.timeRangeSelect.value = 'custom';
        if (elements.customStartDate) elements.customStartDate.value = state.customStart;
        if (elements.customEndDate) elements.customEndDate.value = state.customEnd;
        setCustomPending(true);
        openRangePopover();
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
        state.appliedTimeRange = 'custom';
        syncCustomRangeEdit();
        if (elements.timeRangeSelect) elements.timeRangeSelect.value = 'custom';
        closeRangePopover(true);
        fetchUsageData(true).catch((err) => console.error('Error fetching usage data on custom range apply:', err));
      });
    }

    // Dismiss the popover with Escape or a press outside of the range control.
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isRangePopoverOpen()) {
        event.preventDefault();
        dismissRangePopover(true);
      }
    });
    document.addEventListener('pointerdown', (event) => {
      if (!isRangePopoverOpen()) return;
      if (elements.rangeControl && elements.rangeControl.contains(event.target)) return;
      dismissRangePopover(false);
    });

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
          timezone: state.currentUsageData?.timezone || '',
        });
      });
    }

    setupDetailsTabs();
    setupTopbarScroll();
  }

  /**
   * Hairline under the sticky topbar once the page has scrolled.
   */
  function setupTopbarScroll() {
    const topbar = elements.topbar;
    if (!topbar) return;
    const update = () => topbar.classList.toggle('is-scrolled', window.scrollY > 4);
    window.addEventListener('scroll', update, { passive: true });
    update();
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
   * Renderers receive the elements they need as explicit arguments; charts
   * look up their own mount points.
   */
  function cacheElements() {
    elements.topbar = document.getElementById('topbar');
    elements.toolFilter = document.getElementById('tool-filter');
    elements.rangeControl = document.getElementById('range-control');
    elements.rangePopover = document.getElementById('range-popover');
    elements.timeRangeSelect = document.getElementById('time-range-select');
    elements.customRangeEdit = document.getElementById('custom-range-edit');
    elements.customStartDate = document.getElementById('custom-start-date');
    elements.customEndDate = document.getElementById('custom-end-date');
    elements.customEndControl = document.getElementById('custom-end-control');
    elements.customEndNowLabel = document.getElementById('custom-end-now-label');
    elements.customRangeApply = document.getElementById('custom-range-apply');
    elements.autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    elements.refreshInterval = document.getElementById('refresh-interval');
    elements.refreshBtn = document.getElementById('refresh-btn');
    elements.themeToggle = document.getElementById('theme-toggle');
    elements.lastSyncedBadge = document.getElementById('last-synced-badge');

    elements.meterLabel = document.getElementById('meter-label');
    elements.cardSpendSubtext = document.getElementById('card-spend-subtext');
    elements.readoutBurn = document.getElementById('readout-burn');
    elements.readoutTokens = document.getElementById('readout-tokens');
    elements.readoutRate = document.getElementById('readout-rate');
    elements.readoutCalls = document.getElementById('readout-calls');
    elements.cardBurnSubtext = document.getElementById('card-burn-subtext');
    elements.cardTokensSubtext = document.getElementById('card-tokens-subtext');
    elements.cardRateSubtext = document.getElementById('card-rate-subtext');
    elements.cardCallsSubtext = document.getElementById('card-calls-subtext');

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

    elements.unpricedBanner = document.getElementById('unpriced-banner');
    elements.unpricedSummaryBanner = document.getElementById('unpriced-summary-banner');

    elements.modelLedger = document.getElementById('model-ledger');
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
    setupTheme();
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
