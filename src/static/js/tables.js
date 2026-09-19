/**
 * DashboardTables - Per-model and sessions table renderers.
 * Depends on window.DashboardUtils and window.DashboardApi (getModelRates).
 */
(function () {
  'use strict';

  /**
   * Render Per-Model Granularity Table with all 12 columns.
   * @param {Array} models
   * @param {object} ctx { tbody, countBadge, pricingData, unpricedBanner }
   */
  function renderModelTable(models, ctx) {
    const tbody = ctx && ctx.tbody;
    if (!tbody) return;
    const { escapeHtml, providerBadge, isEstimatedRow, EST_TOOLTIP } = window.DashboardUtils;

    const modelList = Array.isArray(models) ? models : [];

    if (ctx.countBadge) {
      ctx.countBadge.textContent = `${modelList.length} Model${modelList.length === 1 ? '' : 's'}`;
    }

    const unpricedBanner = (ctx && ctx.unpricedBanner) || document.getElementById('unpriced-banner');
    const unpricedRows = modelList.filter((m) => m && (m.unpriced === true || m.priced === false));
    const updateUnpricedBanner = () => {
      if (!unpricedBanner) return;
      if (unpricedRows.length > 0) {
        const names = unpricedRows
          .slice(0, 3)
          .map((m) => `${m.model || 'unknown'} (${m.provider || m.tool || 'unknown provider'})`)
          .join(', ');
        const extra = unpricedRows.length > 3 ? ` +${unpricedRows.length - 3} more` : '';
        unpricedBanner.textContent = `${unpricedRows.length} model${unpricedRows.length === 1 ? '' : 's'} unpriced - add rates in PricingCatalog (${names}${extra})`;
        unpricedBanner.hidden = false;
      } else {
        unpricedBanner.textContent = '';
        unpricedBanner.hidden = true;
      }
    };

    const footnote = document.getElementById('models-est-footnote');

    if (modelList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No model usage data recorded.</td></tr>';
      if (footnote) footnote.style.display = 'none';
      updateUnpricedBanner();
      return;
    }

    let rowsHtml = '';
    let hasEstimated = false;
    modelList.forEach((m) => {
      const modelName = String(m.model || 'unknown');
      const badge = providerBadge(m.tool || (/gpt|o1|o3/i.test(modelName) ? 'codex' : 'antigravity'));
      const est = isEstimatedRow(m);
      if (est) hasEstimated = true;
      // "~" prefix + "est." badge mark heuristic counts; title/aria carry the rationale.
      const estBadge = est
        ? ` <span class="est-badge" title="${escapeHtml(EST_TOOLTIP)}" aria-label="Estimated tokens">est.</span>`
        : '';
      const tok = (v) => (est ? '~' : '') + (v || 0).toLocaleString();
      const usd = (v) => (est ? '~' : '') + '$' + (v || 0).toFixed(4);
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      // Backend flags models with no catalog rate; never backfill fallback rates.
      const isUnpricedRow = m.unpriced === true || m.priced === false;

      const rates = isUnpricedRow ? null : window.DashboardApi.getModelRates(ctx.pricingData, modelName);
      const ratesStr = rates
        ? `$${(rates.uncached_input ?? 0).toFixed(2)} / $${(rates.cached_input ?? 0).toFixed(3)} / $${(rates.output ?? 0).toFixed(2)}`
        : 'N/A';

      const hitRate = Number(m.cache_hit_rate) || 0;
      let hitRateClass = 'hit-rate-low';
      if (hitRate >= 75) hitRateClass = 'hit-rate-high';
      else if (hitRate >= 35) hitRateClass = 'hit-rate-mid';

      const providerLabel = String(m.provider || m.tool || '');
      const unpricedTitle = `Unpriced model - no catalog rate for ${modelName}${providerLabel ? ` (${providerLabel})` : ''}`;
      const unpricedPill = isUnpricedRow
        ? ` <span class="unpriced-pill" title="${escapeHtml(unpricedTitle)}">unpriced</span>`
        : '';
      const costTitle = isUnpricedRow ? ` title="${escapeHtml(unpricedTitle)}"` : tokTitle;
      const cachedCost = isUnpricedRow ? '<strong>$0.0000</strong>' : `<strong>${usd(m.est_cost_cached_usd)}</strong>`;

      rowsHtml += `
        <tr class="${isUnpricedRow ? 'unpriced-row' : ''}">
          <td>
            <strong>${escapeHtml(modelName)}</strong>
            <span class="provider-badge ${badge.className}" style="margin-left: 8px;">${badge.text}</span>${estBadge}${unpricedPill}
          </td>
          <td class="cell-mono cell-right"${tokTitle}>${tok(m.uncached_input)}</td>
          <td class="cell-mono cell-right"${tokTitle}>${tok(m.cached_input)}</td>
          <td class="cell-mono cell-right"${tokTitle}>${tok(m.total_input)}</td>
          <td class="cell-mono cell-right"${tokTitle}>${tok(m.output)}</td>
          <td class="cell-mono cell-right"${tokTitle}>${tok(m.reasoning_output)}</td>
          <td class="cell-mono cell-right"${tokTitle}><strong>${tok(m.total_tokens)}</strong></td>
          <td class="cell-right">
            <span class="hit-rate-pill ${hitRateClass}">${hitRate.toFixed(2)}%</span>
          </td>
          <td class="cell-mono cell-right text-muted">${ratesStr}</td>
          <td class="cell-mono cell-right"${costTitle}>${cachedCost}</td>
          <td class="cell-mono cell-right text-muted"${tokTitle}>${usd(m.est_cost_uncached_usd)}</td>
          <td class="cell-mono cell-right text-success"${tokTitle}>+${usd(m.est_savings_usd)}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
    if (footnote) footnote.style.display = hasEstimated ? '' : 'none';
    updateUnpricedBanner();
  }

  /**
   * Render Recent Sessions Explorer with real-time text search filtering.
   * @param {Array} allSessions
   * @param {string} searchQuery
   * @param {object} ctx { tbody, countBadge }
   */
  function renderSessionsTable(allSessions, searchQuery, ctx) {
    const tbody = ctx && ctx.tbody;
    if (!tbody) return;
    const { escapeHtml, formatDateTime, providerBadge, isEstimatedRow, EST_TOOLTIP } = window.DashboardUtils;

    const query = String(searchQuery || '').trim().toLowerCase();
    const sessionList = (Array.isArray(allSessions) ? allSessions : []).filter(
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

    if (ctx.countBadge) {
      ctx.countBadge.textContent = query
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
      const badge = providerBadge(toolStr);
      const est = isEstimatedRow(s);
      const estBadge = est
        ? ` <span class="est-badge" title="${escapeHtml(EST_TOOLTIP)}" aria-label="Estimated tokens">est.</span>`
        : '';
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      const tokPrefix = est ? '~' : '';

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
            <span class="provider-badge ${badge.className}">${badge.text}</span>${estBadge}
          </td>
          <td class="cell-mono">${escapeHtml(modelStr)}</td>
          <td class="cell-mono cell-right"${tokTitle}><strong>${tokPrefix}${(s.total_tokens || 0).toLocaleString()}</strong></td>
          <td class="cell-right">
            <span class="hit-rate-pill ${hitRateClass}">${hitRate.toFixed(1)}%</span>
          </td>
          <td class="cell-mono cell-right text-success"${tokTitle}>${tokPrefix}$${(s.cost_cached_usd || 0).toFixed(4)}</td>
          <td class="cell-mono cell-right text-muted">${dateStr}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
  }

  window.DashboardTables = {
    renderModelTable,
    renderSessionsTable,
  };
})();
