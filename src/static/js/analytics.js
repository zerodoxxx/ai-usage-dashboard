/**
 * DashboardAnalytics - Analytics snapshot renderer (stats, top sessions,
 * period comparison). Depends on window.DashboardUtils.
 */
(function () {
  'use strict';

  const PROJECTION_LABELS = {
    all_run_rate: 'All-time daily average × 30',
    last_30_days: 'All-time daily average × 30',
    current_month_run_rate: 'Current-month daily average × 30',
    custom_run_rate: 'Selected-range daily average × 30',
    '30d_run_rate': '30-day daily average × 30',
    '7d_run_rate': '7-day daily average × 30',
    '24h_run_rate': '24-hour daily average × 30',
  };

  function projectionBasisLabel(basis) {
    return PROJECTION_LABELS[basis] || 'Filter daily average × 30';
  }

  /**
   * Format the secondary analytics snapshot.
   * @param {object} analytics
   * @param {object} summary
   * @param {object} ctx element refs + selectedRangeLabel
   */
  function updateAnalytics(analytics, summary, ctx) {
    const { formatCompactNumber } = window.DashboardUtils;
    const details = analytics && typeof analytics === 'object' ? analytics : {};
    const totals = summary && typeof summary === 'object' ? summary : {};
    // Two decimals from $1 up, four below so small per-session costs stay readable.
    const formatCurrency = (value) => {
      const amount = Number(value || 0);
      return window.DashboardUtils.formatUsd(amount, Math.abs(amount) >= 1 ? 2 : 4);
    };
    const formatDay = (value) => {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
      if (!match) return String(value || '');
      const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
      return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    };
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

    if (ctx.analyticsWindowBadge) {
      ctx.analyticsWindowBadge.textContent = ctx.selectedRangeLabel || 'Selected range';
    }
    if (ctx.analyticsCallCount) {
      ctx.analyticsCallCount.textContent = Number(totals.call_count || 0).toLocaleString();
    }
    if (ctx.analyticsActiveDays) {
      ctx.analyticsActiveDays.textContent = Number(details.active_days || 0).toLocaleString();
    }
    if (ctx.analyticsAvgCost) {
      ctx.analyticsAvgCost.textContent = formatCurrency(details.avg_cost_per_session_usd);
    }
    if (ctx.analyticsAvgTokens) {
      ctx.analyticsAvgTokens.textContent = formatCompactNumber(Math.round(Number(details.avg_tokens_per_session || 0)));
    }
    if (ctx.analyticsMonthlyProjection) {
      ctx.analyticsMonthlyProjection.textContent = formatCurrency(
        details.projected_30d_usd ?? details.monthly_projection_usd
      );
    }
    if (ctx.analyticsProjectionBasis) {
      ctx.analyticsProjectionBasis.textContent = projectionBasisLabel(details.projection_basis);
    }

    const peakDay = details.peak_day;
    if (ctx.analyticsPeakDayCost) {
      ctx.analyticsPeakDayCost.textContent = peakDay ? formatCurrency(peakDay.cost_cached_usd) : '$0.00';
    }
    if (ctx.analyticsPeakDayDetail) {
      ctx.analyticsPeakDayDetail.textContent = peakDay
        ? `${formatDay(peakDay.date)}, ${Number(peakDay.call_count || 0).toLocaleString()} calls`
        : 'No daily usage yet';
    }

    renderTopSessions(
      ctx.topSessionsTableBody,
      details.top_sessions || [],
      formatCurrency,
      ctx.timezone,
    );

    const comparison = details.comparison;
    if (ctx.comparisonSubtitle) {
      ctx.comparisonSubtitle.textContent = comparison
        ? `Compared with the ${comparison.label}`
        : 'Select a preset range to compare usage';
    }
    if (!ctx.comparisonContent) return;
    if (!comparison) {
      ctx.comparisonContent.innerHTML = '<p class="comparison__empty">No comparison available for this range.</p>';
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
    ctx.comparisonContent.innerHTML = `
      <div class="table-scroll" role="region" aria-label="Period comparison table" tabindex="0">
        <table class="data-table">
          <caption class="visually-hidden">Current period compared with the previous period</caption>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col" class="cell-right">Current</th>
              <th scope="col" class="cell-right">Previous</th>
              <th scope="col" class="cell-right">Change</th>
            </tr>
          </thead>
          <tbody>
            ${comparisonRows.map((row) => `
              <tr>
                <th scope="row" class="comparison__metric">${row.label}</th>
                <td class="cell-num">${row.current}</td>
                <td class="cell-num cell-muted">${row.previous}</td>
                <td class="cell-num ${changeClass(row.change, row.lowerIsBetter)}">${formatChange(row.change)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  /**
   * Render the five highest-cost sessions in the current view.
   */
  function renderTopSessions(tbody, sessions, formatCurrency, timezone = '') {
    if (!tbody) return;
    const {
      formatDateTime,
      providerBadge,
      escapeHtml,
      tokenProvenance,
      provenanceLabel,
      EST_TOOLTIP,
    } = window.DashboardUtils;

    const sessionList = (Array.isArray(sessions) ? sessions : []).filter((session) => session && typeof session === 'object');
    if (sessionList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No session cost data in this period</td></tr>';
      return;
    }

    tbody.innerHTML = sessionList.map((session) => {
      const tool = String(session.tool || '');
      const badge = providerBadge(tool);
      const provenance = tokenProvenance(session);
      const est = provenance !== 'reported';
      const estBadge = est
        ? `<span class="chip" title="${escapeHtml(EST_TOOLTIP)}" aria-label="${escapeHtml(provenanceLabel(session))} token provenance">${escapeHtml(provenanceLabel(session))}</span>`
        : '';
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      const tokPrefix = est ? '~' : '';
      const costStatus = String(session.pricing_status || session.cost_source || '').toLowerCase();
      const costAvailable = (
        session.cost_available === true
        || (session.cost_available !== false
          && session.cost_cached_usd != null
          && !['unknown', 'unpriced', 'ambiguous'].includes(costStatus))
      );
      const costText = costAvailable
        ? `${tokPrefix}${formatCurrency(session.cost_cached_usd)}`
        : '<span class="cost-unavailable" aria-label="Cost unavailable">–</span>';
      const costTitle = costAvailable ? tokTitle : ' title="Cost unavailable: no catalog rate"';
      const activity = formatDateTime(
        session.activity_at || session.created_at || session.start_time,
        timezone,
      );
      return `
        <tr>
          <td>
            <strong class="insight-session-title" title="${escapeHtml(String(session.title || 'Untitled session'))}">${escapeHtml(String(session.title || 'Untitled session'))}</strong>
            <span class="cell-sub">${escapeHtml(activity)}</span>
          </td>
          <td><div class="model-cell__meta"><span class="${badge.className}">${badge.text}</span>${estBadge}${costAvailable ? '' : '<span class="chip">unpriced</span>'}</div></td>
          <td>${escapeHtml(String(session.model || 'unknown'))}</td>
          <td class="cell-num"${tokTitle}>${tokPrefix}${Number(session.total_tokens || 0).toLocaleString()}</td>
          <td class="cell-num${costAvailable ? '' : ' cell-muted'}"${costTitle}>${costText}</td>
        </tr>
      `;
    }).join('');
  }

  window.DashboardAnalytics = {
    updateAnalytics,
    renderTopSessions,
    projectionBasisLabel,
  };
})();
