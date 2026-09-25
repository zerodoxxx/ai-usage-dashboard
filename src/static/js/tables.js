/**
 * DashboardTables - Per-model and sessions table renderers.
 * Depends on window.DashboardUtils and window.DashboardApi (getModelRates).
 */
(function () {
  'use strict';

  const SESSION_TITLE_MAX_LENGTH = 96;
  const CODEX_HISTORY_TITLE_PREFIX = /^The following is the Codex agent history whose request action you are assessing:\s*/i;

  /**
   * Return a compact, one-line session title for the table while leaving the
   * complete (escaped by the caller) value available to title/ARIA metadata.
   * Some provider exports prepend a verbose wrapper to the actual request;
   * remove only that known wrapper so ordinary user titles remain untouched.
   */
  function normalizeSessionTitle(value) {
    const raw = String(value ?? '').replace(/\s+/g, ' ').trim();
    if (!raw) return 'Untitled Session';

    const withoutWrapper = raw.replace(CODEX_HISTORY_TITLE_PREFIX, '').trim() || raw;
    if (withoutWrapper.length <= SESSION_TITLE_MAX_LENGTH) return withoutWrapper;
    return `${withoutWrapper.slice(0, SESSION_TITLE_MAX_LENGTH - 1).trimEnd()}…`;
  }

  function fullSessionTitle(value) {
    const normalized = String(value ?? '').replace(/\s+/g, ' ').trim();
    return normalized || 'Untitled Session';
  }

  function sessionCountLabel(displayCount, totalCount, query) {
    const display = Number(displayCount) || 0;
    const total = Number(totalCount) || 0;
    const noun = total === 1 ? 'session' : 'sessions';
    const matching = query ? ' matching' : '';
    if (display < total) return `Showing ${display} of ${total}${matching} ${noun}`;
    return `${display}${matching} ${noun}`;
  }

  /**
   * Render Per-Model Granularity Table with all 12 columns.
   * @param {Array} models
   * @param {object} ctx { tbody, countBadge, pricingData, unpricedBanner }
   */
  function renderModelTable(models, ctx) {
    const tbody = ctx && ctx.tbody;
    if (!tbody) return;
    const {
      escapeHtml,
      providerBadge,
      tokenProvenance,
      provenanceLabel,
      EST_TOOLTIP,
    } = window.DashboardUtils;

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
      const provenance = tokenProvenance(m);
      const est = provenance !== 'reported';
      if (est) hasEstimated = true;
      // "~" prefix + provenance badge mark heuristic or mixed counts.
      const estBadge = est
        ? ` <span class="est-badge ${provenance === 'mixed' ? 'mixed-badge' : ''}" title="${escapeHtml(EST_TOOLTIP)}" aria-label="${escapeHtml(provenanceLabel(m))} token provenance">${escapeHtml(provenanceLabel(m))}</span>`
        : '';
      const tok = (v) => (est ? '~' : '') + (v || 0).toLocaleString();
      const usd = (v) => (est ? '~' : '') + '$' + (v || 0).toFixed(4);
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      // Backend flags models with no catalog rate; never backfill fallback rates.
      const isUnpricedRow = m.unpriced === true || m.priced === false;

      const rates = isUnpricedRow ? null : window.DashboardApi.getModelRates(
        ctx.pricingData,
        m.canonical_model || modelName
      );
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
      const unavailableCost = `<strong class="cost-unavailable" title="${escapeHtml(unpricedTitle)}" aria-label="Unavailable; no catalog rate">—</strong>`;
      const cachedCost = isUnpricedRow ? unavailableCost : `<strong>${usd(m.est_cost_cached_usd)}</strong>`;
      const uncachedCost = isUnpricedRow ? unavailableCost : usd(m.est_cost_uncached_usd);
      const savingsValue = Number(m.est_savings_usd || 0);
      const savingsPrefix = savingsValue > 0 ? '+' : '';
      const savingsClass = savingsValue < 0 ? 'text-negative' : 'text-success';
      const savingsCost = isUnpricedRow
        ? unavailableCost
        : `${savingsPrefix}${usd(savingsValue)}`;

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
          <td class="cell-mono cell-right text-muted"${costTitle}>${uncachedCost}</td>
          <td class="cell-mono cell-right ${isUnpricedRow ? 'text-muted' : savingsClass}"${costTitle}>${savingsCost}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
    if (footnote) footnote.style.display = hasEstimated ? '' : 'none';
    updateUnpricedBanner();
  }

  function filterSessions(allSessions, searchQuery) {
    const query = String(searchQuery || '').trim().toLowerCase();
    const sessions = (Array.isArray(allSessions) ? allSessions : []).filter(
      (session) => session && typeof session === 'object'
    );
    if (!query) return sessions;
    return sessions.filter((session) => {
      const title = String(session.title || '').toLowerCase();
      const model = String(session.model || '').toLowerCase();
      const tool = String(session.tool || '').toLowerCase();
      const id = String(session.id || '').toLowerCase();
      return title.includes(query) || model.includes(query) || tool.includes(query) || id.includes(query);
    });
  }

  function csvCell(value) {
    const text = String(value ?? '');
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function csvText(value) {
    const text = String(value ?? '');
    // Prevent spreadsheet formula execution for user-controlled titles/IDs.
    return /^[=+\-@]/.test(text) ? `'${text}` : text;
  }

  /**
   * Download the sessions in the active time range (and active search) as CSV.
   * @returns {number} number of exported session rows
   */
  function exportSessionsCsv(allSessions, searchQuery, filename = 'ai-usage-sessions.csv', timezone = '', doc = document) {
    const sessions = filterSessions(allSessions, searchQuery);
    const { formatDateTime, tokenProvenance } = window.DashboardUtils;
    const header = [
      'Session ID',
      'Title',
      'Tool',
      'Model',
      'Call Count',
      'Total Tokens',
      'Cache Hit %',
      'Cost (USD)',
      'Date / Time',
      'Token Source',
    ];
    const rows = sessions.map((session) => [
      csvText(session.id || ''),
      csvText(session.title || 'Untitled Session'),
      csvText(session.tool || ''),
      csvText(session.model || 'unknown'),
      Number(session.call_count || 0),
      Number(session.total_tokens || 0),
      Number(session.cache_hit_rate || 0).toFixed(2),
      Number(session.cost_cached_usd || 0).toFixed(6),
      csvText(formatDateTime(
        session.activity_at || session.created_at || session.start_time,
        timezone,
      )),
      tokenProvenance(session),
    ]);
    const csv = `\uFEFF${[header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n')}\r\n`;
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const urlApi = window.URL || URL;
    const url = urlApi.createObjectURL(blob);
    const link = doc.createElement('a');
    link.href = url;
    link.download = String(filename || 'ai-usage-sessions.csv');
    link.style.display = 'none';
    doc.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => urlApi.revokeObjectURL(url), 0);
    return rows.length;
  }

  /**
   * Render Recent Sessions Explorer with real-time text search filtering.
   * @param {Array} allSessions
   * @param {string} searchQuery
   * @param {object} ctx { tbody, countBadge, timezone }
   */
  function renderSessionsTable(allSessions, searchQuery, ctx) {
    const tbody = ctx && ctx.tbody;
    if (!tbody) return;
    const {
      escapeHtml,
      formatDateTime,
      providerBadge,
      tokenProvenance,
      provenanceLabel,
      EST_TOOLTIP,
    } = window.DashboardUtils;

    const query = String(searchQuery || '').trim().toLowerCase();
    const filtered = filterSessions(allSessions, searchQuery);

    if (ctx.countBadge) {
      ctx.countBadge.textContent = sessionCountLabel(
        Math.min(filtered.length, 100),
        filtered.length,
        query,
      );
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
      const provenance = tokenProvenance(s);
      const est = provenance !== 'reported';
      const estBadge = est
        ? ` <span class="est-badge ${provenance === 'mixed' ? 'mixed-badge' : ''}" title="${escapeHtml(EST_TOOLTIP)}" aria-label="${escapeHtml(provenanceLabel(s))} token provenance">${escapeHtml(provenanceLabel(s))}</span>`
        : '';
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      const tokPrefix = est ? '~' : '';

      const hitRate = Number(s.cache_hit_rate) || 0;
      let hitRateClass = 'hit-rate-low';
      if (hitRate >= 75) hitRateClass = 'hit-rate-high';
      else if (hitRate >= 35) hitRateClass = 'hit-rate-mid';

      const dateStr = formatDateTime(
        s.activity_at || s.created_at || s.start_time,
        ctx.timezone,
      );
      const titleStr = normalizeSessionTitle(s.title);
      const fullTitle = fullSessionTitle(s.title);
      const idStr = String(s.id || '');
      const modelStr = String(s.model || 'unknown');
      const costStatus = String(s.pricing_status || s.cost_source || '').toLowerCase();
      const costAvailable = s.cost_available !== false
        && !['unknown', 'unpriced', 'ambiguous'].includes(costStatus);
      const costTitle = costAvailable
        ? (est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '')
        : ' title="Cost unavailable: no catalog rate" aria-label="Cost unavailable"';
      const costText = costAvailable
        ? `${tokPrefix}$${(s.cost_cached_usd || 0).toFixed(4)}`
        : '<strong class="cost-unavailable" aria-label="Cost unavailable">—</strong>';
      const unpricedBadge = costAvailable
        ? ''
        : ' <span class="unpriced-pill" title="Cost unavailable: no catalog rate">unpriced</span>';

      rowsHtml += `
        <tr>
          <td>
            <strong class="session-title" title="${escapeHtml(fullTitle)}" aria-label="${escapeHtml(fullTitle)}">${escapeHtml(titleStr)}</strong>
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
    normalizeSessionTitle,
    sessionCountLabel,
    filterSessions,
    exportSessionsCsv,
  };
})();
