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
    const { formatCompactNumber, formatDateTime, providerBadge, escapeHtml } = window.DashboardUtils;
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
      ctx.analyticsPeakDayCost.textContent = peakDay ? formatCurrency(peakDay.cost_cached_usd) : '$0.0000';
    }
    if (ctx.analyticsPeakDayDetail) {
      ctx.analyticsPeakDayDetail.textContent = peakDay
        ? `${peakDay.date} · ${Number(peakDay.call_count || 0).toLocaleString()} calls`
        : 'No daily usage yet';
    }

    renderTopSessions(ctx.topSessionsTableBody, details.top_sessions || [], formatCurrency);

    const comparison = details.comparison;
    if (ctx.comparisonSubtitle) {
      ctx.comparisonSubtitle.textContent = comparison
        ? `Compared with the ${comparison.label}`
        : 'Select a preset range to compare usage';
    }
    if (!ctx.comparisonContent) return;
    if (!comparison) {
      ctx.comparisonContent.innerHTML = '<div class="comparison-empty">No comparison available for All time.</div>';
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
  function renderTopSessions(tbody, sessions, formatCurrency) {
    if (!tbody) return;
    const { formatDateTime, providerBadge, escapeHtml, isEstimatedRow, EST_TOOLTIP } = window.DashboardUtils;

    const sessionList = (Array.isArray(sessions) ? sessions : []).filter((session) => session && typeof session === 'object');
    if (sessionList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No session cost data available.</td></tr>';
      return;
    }

    tbody.innerHTML = sessionList.map((session) => {
      const tool = String(session.tool || '');
      const badge = providerBadge(tool);
      const est = isEstimatedRow(session);
      const estBadge = est
        ? ` <span class="est-badge" title="${escapeHtml(EST_TOOLTIP)}" aria-label="Estimated tokens">est.</span>`
        : '';
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      const tokPrefix = est ? '~' : '';
      const activity = formatDateTime(session.activity_at || session.created_at || session.start_time);
      return `
        <tr>
          <td>
            <strong class="insight-session-title" title="${escapeHtml(String(session.title || 'Untitled Session'))}">${escapeHtml(String(session.title || 'Untitled Session'))}</strong>
            <div class="text-muted" style="font-size: 10px; font-family: var(--font-mono);">${escapeHtml(activity)}</div>
          </td>
          <td><span class="provider-badge ${badge.className}">${badge.text}</span>${estBadge}</td>
          <td class="cell-mono">${escapeHtml(String(session.model || 'unknown'))}</td>
          <td class="cell-mono cell-right"${tokTitle}>${tokPrefix}${Number(session.total_tokens || 0).toLocaleString()}</td>
          <td class="cell-mono cell-right text-success"${tokTitle}>${tokPrefix}${formatCurrency(session.cost_cached_usd)}</td>
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
