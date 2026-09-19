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

  function providerBadge(tool) {
    const normalized = String(tool || '').toLowerCase();
    if (normalized === 'codex') return { className: 'badge-codex', text: 'Codex' };
    if (normalized === 'claude' || normalized === 'claude-code') {
      return { className: 'badge-claude', text: 'Claude Code' };
    }
    return { className: 'badge-agy', text: 'Antigravity' };
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
      <span>${type === 'error' ? '⚠️' : 'ℹ️'}</span>
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
