/**
 * DashboardUtils - Shared pure helpers for the AI Usage dashboard.
 * No DOM state; safe to load before all other dashboard modules.
 */
(function () {
  'use strict';

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

  const PROVIDER_MARKS = {
    codex: '<svg class="provider-mark" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="8 1.5 14 5 14 11 8 14.5 2 11 2 5"></polygon><circle cx="8" cy="8" r="1.6"></circle></svg>',
    claude: '<svg class="provider-mark" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><line x1="8" y1="1.5" x2="8" y2="14.5"></line><line x1="2.4" y1="4.4" x2="13.6" y2="11.6"></line><line x1="13.6" y1="4.4" x2="2.4" y2="11.6"></line></svg>',
    agy: '<svg class="provider-mark" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><polygon points="8 2 14.5 13.5 1.5 13.5"></polygon></svg>',
  };

  const TOAST_ICONS = {
    error: '<svg class="toast-icon toast-icon-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>',
    info: '<svg class="toast-icon toast-icon-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>',
  };

  function providerBadge(tool) {
    const normalized = String(tool || '').toLowerCase();
    if (normalized === 'codex') {
      return { className: 'badge-codex', text: `${PROVIDER_MARKS.codex}<span>Codex</span>` };
    }
    if (normalized === 'claude' || normalized === 'claude-code') {
      return { className: 'badge-claude', text: `${PROVIDER_MARKS.claude}<span>Claude Code</span>` };
    }
    return { className: 'badge-agy', text: `${PROVIDER_MARKS.agy}<span>Antigravity</span>` };
  }

  // AGY (Antigravity) token counts are chars//4 estimates, unlike the exact
  // API-reported counts from Codex/Claude. The backend marks these rows with
  // `estimated:true` / `token_source:'estimated'` (cost provenance stays in
  // `cost_source`/`pricing_status`), so token and cost cells can carry a
  // "~"/"est." marker instead of looking exact.
  const EST_TOOLTIP = 'Estimated from transcript text (chars ÷ 4); single-turn assumes 0% cache, multi-turn assumes a flat 45% cached-input share. Codex/Claude counts are exact API reports.';
  function isEstimatedRow(row) {
    if (!row || typeof row !== 'object') return false;
    if (row.estimated === true) return true;
    if (String(row.token_source || '').toLowerCase() === 'estimated') return true;
    if (row.metadata && (row.metadata.estimated === true || String(row.metadata.token_source || '').toLowerCase() === 'estimated')) return true;
    const tool = String(row.tool || '').toLowerCase();
    return tool === 'antigravity' || tool === 'agy' || tool === 'google';
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
   * Toast notification display.
   * Looks up the container on each call so this helper stays
   * independent of the orchestrator's cached element registry.
   */
  function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type === 'error' ? 'toast-error' : ''}`;
    toast.innerHTML = `
      ${type === 'error' ? TOAST_ICONS.error : TOAST_ICONS.info}
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

  window.DashboardUtils = {
    formatCompactNumber,
    getCompactMetricParts,
    updateCompactMetric,
    formatDateTime,
    providerBadge,
    escapeHtml,
    showToast,
    isEstimatedRow,
    EST_TOOLTIP,
  };
})();
