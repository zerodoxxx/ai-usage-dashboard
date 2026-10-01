/**
 * DashboardTables - Model ledger plus the per-model and sessions tables.
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
    if (!raw) return 'Untitled session';

    const withoutWrapper = raw.replace(CODEX_HISTORY_TITLE_PREFIX, '').trim() || raw;
    if (withoutWrapper.length <= SESSION_TITLE_MAX_LENGTH) return withoutWrapper;
    return `${withoutWrapper.slice(0, SESSION_TITLE_MAX_LENGTH - 1).trimEnd()}…`;
  }

  function fullSessionTitle(value) {
    const normalized = String(value ?? '').replace(/\s+/g, ' ').trim();
    return normalized || 'Untitled session';
  }

  function sessionCountLabel(displayCount, totalCount, query) {
    const display = Number(displayCount) || 0;
    const total = Number(totalCount) || 0;
    const noun = total === 1 ? 'session' : 'sessions';
    const matching = query ? ' matching' : '';
    if (display < total) return `Showing ${display} of ${total}${matching} ${noun}`;
    return `${display}${matching} ${noun}`;
  }

  const SPEED_HEADER_TITLE = 'Effective output speed: output tokens, including reasoning, divided by the time from request to completed response. Includes queueing and time to first token.';
  const SPEED_UNAVAILABLE_TITLE = "Speed isn't measured for this source yet";

  const MODELS_SORT_KEY = 'aiud.modelsSort';
  // Matches the existing table's column order; token totals remain the default.
  const MODEL_SORT_COLUMNS = [
    'model', 'uncached_input', 'cached_input', 'total_input', 'output',
    'reasoning_output', 'total_tokens', 'cache_hit_rate', 'api_rates',
    'est_cost_cached_usd', 'est_cost_uncached_usd', 'est_savings_usd',
  ];
  const modelTableContexts = new WeakMap();
  let modelsSort = { column: 'total_tokens', direction: 'descending' };
  try {
    const saved = JSON.parse(localStorage.getItem(MODELS_SORT_KEY));
    if (saved && MODEL_SORT_COLUMNS.includes(saved.column)
        && ['ascending', 'descending'].includes(saved.direction)) {
      modelsSort = { column: saved.column, direction: saved.direction };
    }
  } catch (err) {
    // A blocked or invalid storage entry leaves the default sort intact.
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function modelTableBadge(m) {
    const modelName = String(m.model || 'unknown');
    return window.DashboardUtils.providerBadge(
      (m.tool && m.tool !== 'all' ? m.tool : '')
      || m.provider
      || (/claude|sonnet|opus|haiku/i.test(modelName) ? 'claude' : (/gpt|o1|o3/i.test(modelName) ? 'codex' : 'antigravity'))
    );
  }

  function modelSortValue(m, column, pricingData) {
    if (column === 'model') {
      return m.model ? [String(m.model), modelTableBadge(m).label] : null;
    }
    const unpriced = m.unpriced === true || m.priced === false || m.cost_available === false;
    if (column === 'api_rates') {
      const rates = unpriced ? null : window.DashboardApi.getModelRates(pricingData, m.canonical_model || m.model || 'unknown');
      const input = finiteNumber(rates && rates.uncached_input);
      const output = finiteNumber(rates && rates.output);
      return input === null || output === null ? null : [input, output];
    }
    if (column.startsWith('est_') && unpriced) return null;
    return finiteNumber(m[column]);
  }

  function sortModelRows(models, pricingData) {
    const { column, direction } = modelsSort;
    const sign = direction === 'ascending' ? 1 : -1;
    return models.map((m) => ({ m, value: modelSortValue(m, column, pricingData) }))
      .sort((a, b) => {
        // Missing values stay last before applying the direction to real values.
        if (a.value === null || b.value === null) {
          return a.value === b.value ? 0 : (a.value === null ? 1 : -1);
        }
        const left = Array.isArray(a.value) ? a.value : [a.value];
        const right = Array.isArray(b.value) ? b.value : [b.value];
        for (let i = 0; i < left.length; i += 1) {
          const result = typeof left[i] === 'string'
            ? left[i].localeCompare(right[i], undefined, { sensitivity: 'base' })
            : left[i] - right[i];
          if (result) return result * sign;
        }
        return 0;
      }).map(({ m }) => m);
  }

  function updateModelSortHeaders(tbody, models, ctx) {
    modelTableContexts.set(tbody, { models, ctx });
    const table = tbody.closest('table');
    if (!table || !table.tHead) return;
    Array.from(table.tHead.querySelectorAll('th')).forEach((th, index) => {
      const column = MODEL_SORT_COLUMNS[index];
      if (!column) return;
      let button = th.querySelector('.model-sort');
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'model-sort';
        button.innerHTML = `<span>${window.DashboardUtils.escapeHtml(th.textContent.trim())}</span><svg class="model-sort__chevron" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M2 3.5 5 6.5 8 3.5"></path></svg>`;
        button.addEventListener('click', () => {
          modelsSort = {
            column,
            direction: modelsSort.column === column
              ? (modelsSort.direction === 'descending' ? 'ascending' : 'descending')
              : (column === 'model' ? 'ascending' : 'descending'),
          };
          try {
            localStorage.setItem(MODELS_SORT_KEY, JSON.stringify(modelsSort));
          } catch (err) {
            // Sorting still works for this page when storage is blocked.
          }
          const latest = modelTableContexts.get(tbody);
          renderModelTable(latest.models, latest.ctx);
        });
        th.replaceChildren(button);
      }
      th.setAttribute('aria-sort', modelsSort.column === column ? modelsSort.direction : 'none');
    });
  }

  /**
   * Output speed for a model row, tolerant of missing fields (older API payloads).
   * @returns {{text: string, title: string, label: string, available: boolean}}
   */
  function describeSpeed(m) {
    const { formatInt } = window.DashboardUtils;
    const tps = finiteNumber(m.tps);
    if (tps === null || m.tps_status === 'unavailable') {
      return {
        text: '–',
        title: SPEED_UNAVAILABLE_TITLE,
        label: `No speed data. ${SPEED_UNAVAILABLE_TITLE}`,
        available: false,
      };
    }

    const median = finiteNumber(m.tps_median);
    const p10 = finiteNumber(m.tps_p10);
    const p90 = finiteNumber(m.tps_p90);
    const calls = finiteNumber(m.tps_calls);
    const parts = [];
    if (median !== null) parts.push(`median ${Math.round(median)} tok/s`);
    if (p10 !== null && p90 !== null) parts.push(`middle 80% ${Math.round(p10)}–${Math.round(p90)} tok/s`);
    if (calls !== null && calls > 0) parts.push(`from ${formatInt(calls)} timed call${calls === 1 ? '' : 's'}`);
    const detail = parts.join(', ');
    const title = detail ? detail.charAt(0).toUpperCase() + detail.slice(1) : 'Approximate output speed';
    const text = `${Math.round(tps)} tok/s`;
    return { text, title, label: `${text}. ${title}`, available: true };
  }

  /**
   * Render the model ledger: one row per model, sorted by cost, with a stacked
   * token bar (cached input, fresh input, output) and the per-1M blended rate.
   * @param {Array} models
   * @param {HTMLElement} container #model-ledger
   */
  function renderModelLedger(models, container) {
    if (!container) return;
    const {
      escapeHtml,
      providerBadge,
      tokenProvenance,
      provenanceLabel,
      formatCompactNumber,
      formatUsd,
      formatRate,
      toolDotHtml,
      EST_TOOLTIP,
    } = window.DashboardUtils;

    const list = (Array.isArray(models) ? models : []).filter((m) => m && typeof m === 'object');
    if (list.length === 0) {
      container.removeAttribute('role');
      container.removeAttribute('aria-label');
      container.innerHTML = '<p class="empty-state">No usage in this period</p>';
      return;
    }

    const isUnpriced = (m) => m.unpriced === true || m.priced === false || m.cost_available === false;
    const rows = list.map((m) => {
      const tokens = Math.max(0, finiteNumber(m.total_tokens) ?? 0);
      const knownCost = finiteNumber(m.est_cost_cached_usd);
      const cost = knownCost ?? 0;
      const unpriced = isUnpriced(m);
      const rate = !unpriced && tokens > 0 && knownCost !== null ? (cost / tokens) * 1e6 : null;
      return {
        m,
        tokens,
        cost,
        unpriced,
        rate: finiteNumber(rate),
      };
    });
    rows.sort((a, b) => {
      if (a.unpriced !== b.unpriced) return a.unpriced ? 1 : -1;
      return a.unpriced ? b.tokens - a.tokens : b.cost - a.cost;
    });

    const maxTokens = Math.max(1, ...rows.map((r) => r.tokens));
    const maxRate = Math.max(0, ...rows.map((r) => r.rate || 0));

    const rowsHtml = rows.map(({ m, tokens, cost, unpriced, rate }) => {
      const modelName = String(m.model || 'unknown');
      const badge = providerBadge((m.tool && m.tool !== 'all' ? m.tool : '') || m.provider || '');
      const provenance = tokenProvenance(m);
      const estChip = provenance !== 'reported'
        ? `<span class="chip" title="${escapeHtml(EST_TOOLTIP)}">${escapeHtml(provenanceLabel(m))}</span>`
        : '';

      const cached = Math.max(0, Number(m.cached_input) || 0);
      const output = Math.max(0, Number(m.output) || 0);
      const fresh = Math.max(0, tokens - cached - output);
      const segments = [
        ['cached', 'Cached input', cached],
        ['fresh', 'Fresh input', fresh],
        ['output', 'Output', output],
      ].filter((segment) => segment[2] > 0);
      const segmentsHtml = segments
        .map(([kind, label, value]) => `<span class="tokbar__seg tokbar__seg--${kind}" style="flex-grow:${value}" title="${label} ${escapeHtml(formatCompactNumber(value))}"></span>`)
        .join('');
      const barWidth = Math.max(0, Math.min(100, (tokens / maxTokens) * 100));
      const breakdown = segments.map(([, label, value]) => `${label} ${formatCompactNumber(value)}`).join(', ');

      const speed = describeSpeed(m);

      const rateHtml = unpriced
        ? '<span class="ledger__rate-num">Unpriced</span>'
        : rate === null
          ? '<span class="ledger__rate-num">–</span>'
          : `<span class="ledger__rate-num">${escapeHtml(formatRate(rate))}</span><span class="ratebar" style="width:${maxRate > 0 ? Math.max(0, Math.min(100, (rate / maxRate) * 100)) : 0}%"></span>`;
      const costHtml = unpriced
        ? '<span aria-label="Unavailable; no catalog rate">–</span>'
        : escapeHtml(formatUsd(cost, 2));

      return `
        <div class="ledger__row" role="row" data-model-key="${escapeHtml(m.key || '')}" tabindex="0">
          <div class="ledger__model" role="cell">
            ${toolDotHtml(badge.key)}
            <div class="ledger__model-text">
              <div class="ledger__nameline"><span class="ledger__name" title="${escapeHtml(modelName)}">${escapeHtml(modelName)}</span>${estChip}</div>
              <span class="ledger__tool">${escapeHtml(badge.label)}</span>
            </div>
          </div>
          <div class="ledger__tokens" role="cell">
            <div class="ledger__track">
              <div class="tokbar" style="width:${barWidth}%" aria-hidden="true">${segmentsHtml}</div>
              <span class="visually-hidden">${escapeHtml(breakdown)}</span>
            </div>
            <span class="ledger__total">${escapeHtml(formatCompactNumber(tokens))}</span>
          </div>
          <div class="ledger__cache" role="cell">${(Number(m.cache_hit_rate) || 0).toFixed(1)}%</div>
          <div class="ledger__speed${speed.available ? '' : ' ledger__speed--none'}" role="cell" title="${escapeHtml(speed.title)}" aria-label="${escapeHtml(speed.label)}">${escapeHtml(speed.text)}</div>
          <div class="ledger__rate${rate === null ? ' ledger__rate--none' : ''}" role="cell"${!unpriced && rate === null ? ' title="No tokens in this period"' : ''}>${rateHtml}</div>
          <div class="ledger__cost${unpriced ? ' ledger__cost--none' : ''}" role="cell">${costHtml}</div>
        </div>`;
    }).join('');

    container.setAttribute('role', 'table');
    container.setAttribute('aria-label', 'Models sorted by cost');
    container.innerHTML = `
      <div class="ledger__head" role="row">
        <span role="columnheader">Model</span>
        <span role="columnheader">Tokens</span>
        <span role="columnheader" class="ledger__hcache">Cache hits</span>
        <span role="columnheader" class="ledger__hspeed" title="${escapeHtml(SPEED_HEADER_TITLE)}">Speed</span>
        <span role="columnheader">Per 1M tokens</span>
        <span role="columnheader" class="ledger__hcost">Cost</span>
      </div>${rowsHtml}`;
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
      tokenProvenance,
      provenanceLabel,
      EST_TOOLTIP,
    } = window.DashboardUtils;

    const modelList = (Array.isArray(models) ? models : []).filter((m) => m && typeof m === 'object');
    updateModelSortHeaders(tbody, modelList, ctx);

    if (ctx.countBadge) {
      ctx.countBadge.textContent = `${modelList.length} model${modelList.length === 1 ? '' : 's'}`;
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
        unpricedBanner.textContent = `${unpricedRows.length} model${unpricedRows.length === 1 ? '' : 's'} unpriced: add rates in PricingCatalog (${names}${extra})`;
        unpricedBanner.hidden = false;
      } else {
        unpricedBanner.textContent = '';
        unpricedBanner.hidden = true;
      }
    };

    const footnote = document.getElementById('models-est-footnote');

    if (modelList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No usage in this period</td></tr>';
      if (footnote) footnote.hidden = true;
      updateUnpricedBanner();
      return;
    }

    let rowsHtml = '';
    let hasEstimated = false;
    sortModelRows(modelList, ctx.pricingData).forEach((m) => {
      const modelName = String(m.model || 'unknown');
      const badge = modelTableBadge(m);
      const provenance = tokenProvenance(m);
      const est = provenance !== 'reported';
      if (est) hasEstimated = true;
      // "~" prefix + provenance badge mark heuristic or mixed counts.
      const estBadge = est
        ? `<span class="chip" title="${escapeHtml(EST_TOOLTIP)}" aria-label="${escapeHtml(provenanceLabel(m))} token provenance">${escapeHtml(provenanceLabel(m))}</span>`
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

      const providerLabel = String(m.provider || m.tool || '');
      const unpricedTitle = `Unpriced model: no catalog rate for ${modelName}${providerLabel ? ` (${providerLabel})` : ''}`;
      const unpricedPill = isUnpricedRow
        ? `<span class="chip" title="${escapeHtml(unpricedTitle)}">unpriced</span>`
        : '';
      const mixedCostPill = m.pricing_status === 'mixed'
        ? '<span class="chip" title="Some cost contributions are reported and others are estimated">mixed cost</span>'
        : '';
      const costTitle = isUnpricedRow ? ` title="${escapeHtml(unpricedTitle)}"` : tokTitle;
      const unavailableCost = `<span class="cost-unavailable" title="${escapeHtml(unpricedTitle)}" aria-label="Unavailable; no catalog rate">–</span>`;
      const cachedCost = isUnpricedRow ? unavailableCost : `<strong>${usd(m.est_cost_cached_usd)}</strong>`;
      const uncachedCost = isUnpricedRow ? unavailableCost : usd(m.est_cost_uncached_usd);
      const savingsValue = Number(m.est_savings_usd || 0);
      const savingsPrefix = savingsValue > 0 ? '+' : '';
      const savingsClass = savingsValue < 0 ? 'cell-negative' : 'cell-muted';
      const savingsCost = isUnpricedRow
        ? unavailableCost
        : `${savingsPrefix}${usd(savingsValue)}`;

      rowsHtml += `
        <tr class="${isUnpricedRow ? 'unpriced-row' : ''}">
          <td>
            <div class="model-cell__meta"><strong class="model-name">${escapeHtml(modelName)}</strong>${estBadge}${unpricedPill}${mixedCostPill}</div>
            <div class="cell-sub"><span class="${badge.className}">${badge.text}</span></div>
          </td>
          <td class="cell-num"${tokTitle}>${tok(m.uncached_input)}</td>
          <td class="cell-num"${tokTitle}>${tok(m.cached_input)}</td>
          <td class="cell-num"${tokTitle}>${tok(m.total_input)}</td>
          <td class="cell-num"${tokTitle}>${tok(m.output)}</td>
          <td class="cell-num"${tokTitle}>${tok(m.reasoning_output)}</td>
          <td class="cell-num"${tokTitle}><strong>${tok(m.total_tokens)}</strong></td>
          <td class="cell-num cell-muted">${hitRate.toFixed(2)}%</td>
          <td class="cell-num cell-muted">${ratesStr}</td>
          <td class="cell-num"${costTitle}>${cachedCost}</td>
          <td class="cell-num cell-muted"${costTitle}>${uncachedCost}</td>
          <td class="cell-num ${isUnpricedRow ? 'cell-muted' : savingsClass}"${costTitle}>${savingsCost}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
    if (footnote) footnote.hidden = !hasEstimated;
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
      tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No matching sessions</td></tr>';
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
        ? `<span class="chip" title="${escapeHtml(EST_TOOLTIP)}" aria-label="${escapeHtml(provenanceLabel(s))} token provenance">${escapeHtml(provenanceLabel(s))}</span>`
        : '';
      const tokTitle = est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '';
      const tokPrefix = est ? '~' : '';

      const hitRate = Number(s.cache_hit_rate) || 0;

      const dateStr = formatDateTime(
        s.activity_at || s.created_at || s.start_time,
        ctx.timezone,
      );
      const titleStr = normalizeSessionTitle(s.title);
      const fullTitle = fullSessionTitle(s.title);
      const idStr = String(s.id || '');
      const modelStr = String(s.model || 'unknown');
      const costStatus = String(s.pricing_status || s.cost_source || '').toLowerCase();
      const costAvailable = (
        s.cost_available === true
        || (s.cost_available !== false
          && s.cost_cached_usd != null
          && !['unknown', 'unpriced', 'ambiguous'].includes(costStatus))
      );
      const costTitle = costAvailable
        ? (est ? ` title="${escapeHtml(EST_TOOLTIP)}"` : '')
        : ' title="Cost unavailable: no catalog rate" aria-label="Cost unavailable"';
      const costText = costAvailable
        ? `${tokPrefix}$${(s.cost_cached_usd || 0).toFixed(4)}`
        : '<span class="cost-unavailable" aria-label="Cost unavailable">–</span>';
      const unpricedBadge = costAvailable
        ? ''
        : '<span class="chip" title="Cost unavailable: no catalog rate">unpriced</span>';

      rowsHtml += `
        <tr>
          <td>
            <strong class="session-title" title="${escapeHtml(fullTitle)}" aria-label="${escapeHtml(fullTitle)}">${escapeHtml(titleStr)}</strong>
            <span class="cell-sub">${escapeHtml(idStr)}</span>
          </td>
          <td>
            <div class="model-cell__meta"><span class="${badge.className}">${badge.text}</span>${estBadge}${unpricedBadge}</div>
          </td>
          <td>${escapeHtml(modelStr)}</td>
          <td class="cell-num"${tokTitle}><strong>${tokPrefix}${(s.total_tokens || 0).toLocaleString()}</strong></td>
          <td class="cell-num cell-muted">${hitRate.toFixed(1)}%</td>
          <td class="cell-num${costAvailable ? '' : ' cell-muted'}"${costTitle}>${costText}</td>
          <td class="cell-num cell-muted">${dateStr}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
  }

  window.DashboardTables = {
    renderModelLedger,
    renderModelTable,
    renderSessionsTable,
    normalizeSessionTitle,
    sessionCountLabel,
    filterSessions,
  };
})();
