/**
 * DashboardApi - Network layer for the AI Usage dashboard.
 * Owns fetch calls, the in-flight AbortController, and the
 * isFetching guard. Has no UI side effects except the pricing
 * badge helper (which takes its badge element explicitly).
 *
 * Depends on: window.DashboardUtils (escapeHtml only via callers, not here).
 */
(function () {
  'use strict';

  // Active in-flight abort controller for user-initiated fetches
  let currentAbortController = null;
  let isFetching = false;

  /**
   * Fetch reference pricing table from /api/pricing.
   * @returns {Promise<object>} pricing payload
   */
  async function fetchPricing() {
    const res = await fetch('/api/pricing');
    if (!res.ok) {
      throw new Error(`Failed to fetch pricing: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  function formatPricingAge(fetchedAt) {
    if (!fetchedAt) return '';
    const fetched = new Date(fetchedAt).getTime();
    if (!Number.isFinite(fetched)) return '';
    const diffMs = Date.now() - fetched;
    if (diffMs < 0) return 'just now';
    const minutes = Math.floor(diffMs / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  function updatePricingStatus(badge, metadata) {
    if (!badge) return;
    const escapeHtml = (window.DashboardUtils && window.DashboardUtils.escapeHtml) || ((s) => String(s));
    const meta = metadata && typeof metadata === 'object' ? metadata : {};
    const source = String(meta.source || 'unavailable');
    const age = formatPricingAge(meta.fetched_at);
    const detail = [
      `source: ${source}`,
      meta.fetched_at ? `fetched: ${meta.fetched_at}` : null,
      meta.stale ? 'stale: true' : 'stale: false',
      meta.error ? `error: ${meta.error}` : null,
      meta.source_url ? `url: ${meta.source_url}` : null,
    ].filter(Boolean).join(' · ');
    if (source === 'openai' && !meta.stale) {
      const text = age && age !== 'just now' ? `Pricing: live ${age}` : 'Pricing: live just now';
      badge.innerHTML = `<span class="status-dot status-live"></span>${escapeHtml(text)}`;
      badge.title = detail || 'Official OpenAI rates are fresh';
    } else if (meta.stale) {
      badge.innerHTML = `<span class="status-dot status-stale"></span>Pricing: stale`;
      badge.title = detail || 'Using cached/stale rates';
    } else {
      badge.innerHTML = `<span class="status-dot status-offline"></span>Pricing: offline`;
      badge.title = detail || 'Using bundled fallback rates';
    }
  }

  /**
   * Resolve pricing rates for a given model against a pricing payload.
   */
  function getModelRates(pricingData, modelName) {
    if (!pricingData) {
      return null;
    }
    if (pricingData[modelName]) {
      return pricingData[modelName];
    }
    const norm = (modelName || '').toLowerCase().trim();
    const comparable = (value) => String(value || '').toLowerCase().trim().replace(/[\s_.]+/g, '-');
    const sortedPricing = Object.entries(pricingData)
      .filter(([k]) => !String(k).startsWith('__'))
      .sort((a, b) => b[0].length - a[0].length);
    for (const [k, v] of sortedPricing) {
      if (comparable(k) === comparable(norm)) return v;
    }
    for (const [k, v] of sortedPricing) {
      if (comparable(norm).includes(comparable(k))) return v;
    }
    return null;
  }

  /**
   * Abort-aware usage fetch with the same concurrency semantics as the
   * original monolith: user-initiated requests abort the in-flight one,
   * background polls are skipped while a fetch is running.
   *
   * @param {string} tool
   * @param {string} timeRange
   * @param {boolean} [isUserInitiated=false]
   * @param {{start?: string, end?: string}} [customRange]
   * @returns {Promise<{status: 'ok'|'skipped'|'aborted', data?: object}>}
   */
  async function requestUsage(tool, timeRange, isUserInitiated = false, customRange = {}) {
    if (isUserInitiated) {
      if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
      }
      isFetching = false;
    } else if (isFetching) {
      return { status: 'skipped' };
    }

    isFetching = true;
    const controller = new AbortController();
    currentAbortController = controller;

    try {
      const query = new URLSearchParams({
        tool,
        time_range: timeRange,
      });
      if (timeRange === 'custom') {
        if (customRange && customRange.start) query.set('start', customRange.start);
        if (customRange && customRange.end) query.set('end', customRange.end);
      }
      const res = await fetch(`/api/usage?${query.toString()}`, {
        signal: controller.signal,
      });
      if (!res.ok) {
        const errorText = await res.text();
        throw new Error(`API error ${res.status}: ${errorText}`);
      }
      const data = await res.json();
      return { status: 'ok', data };
    } catch (err) {
      if (err && err.name === 'AbortError') {
        return { status: 'aborted' };
      }
      throw err;
    } finally {
      if (currentAbortController === controller) {
        currentAbortController = null;
        isFetching = false;
      }
    }
  }

  window.DashboardApi = {
    fetchPricing,
    updatePricingStatus,
    getModelRates,
    requestUsage,
  };
})();
