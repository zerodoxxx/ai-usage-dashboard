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
   * Format date strings cleanly
   */
  function formatDateTime(isoString, timeZone) {
    if (!isoString) return '—';
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString).slice(0, 19);
      if (timeZone) {
        const parts = new Intl.DateTimeFormat('en-CA', {
          timeZone: String(timeZone),
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
          hourCycle: 'h23',
        }).formatToParts(d);
        const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
        return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
      }
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
      return String(isoString).slice(0, 19);
    }
  }

  // Tool identity: one mapping used by every renderer. Display names are fixed.
  const TOOL_LABELS = {
    codex: 'Codex',
    claude: 'Claude Code',
    agy: 'Antigravity',
    other: 'Other',
  };
  const TOOL_ALIASES = {
    codex: 'codex',
    claude: 'claude',
    'claude-code': 'claude',
    claude_code: 'claude',
    anthropic: 'claude',
    agy: 'agy',
    antigravity: 'agy',
    gemini: 'agy',
    google: 'agy',
  };

  function toolKey(value) {
    return TOOL_ALIASES[String(value || '').toLowerCase().trim()] || 'other';
  }

  function toolLabel(value) {
    return TOOL_LABELS[toolKey(value)];
  }

  function toolDotHtml(value) {
    return `<span class="dot dot--${toolKey(value)}" aria-hidden="true"></span>`;
  }

  /**
   * Provider identity as [tool dot][tool name]; the name always accompanies the dot.
   */
  function providerBadge(tool) {
    const key = toolKey(tool);
    return {
      key,
      label: TOOL_LABELS[key],
      className: 'provider',
      text: `${toolDotHtml(key)}<span>${TOOL_LABELS[key]}</span>`,
    };
  }

  // AGY transcript token counts are chars//4 estimates, unlike the exact
  // API-reported counts from Codex/Claude. The backend marks those rows with
  // `estimated:true` / `token_source:'estimated'`; token_usage.db rows carry
  // explicit reported provenance. Cost provenance stays in
  // `cost_source`/`pricing_status`.
  const EST_TOOLTIP = 'Estimated from transcript text (chars ÷ 4); single-turn assumes 0% cache, multi-turn assumes a flat 45% cached-input share. Codex/Claude counts are exact API reports.';
  const PROVENANCE_LABELS = {
    reported: 'reported',
    estimated: 'est.',
    mixed: 'mixed',
  };
  function tokenProvenance(row) {
    if (!row || typeof row !== 'object') return 'reported';
    const source = String(row.token_source || (row.metadata && row.metadata.token_source) || '').toLowerCase();
    if (source === 'estimated' || source === 'mixed' || source === 'reported') return source;
    if (row.estimated === true || (row.metadata && row.metadata.estimated === true)) return 'estimated';
    if (Object.prototype.hasOwnProperty.call(row, 'estimated') || Object.prototype.hasOwnProperty.call(row, 'token_source')) {
      return 'reported';
    }
    const tool = String(row.tool || '').toLowerCase();
    return tool === 'antigravity' || tool === 'agy' || tool === 'google' ? 'estimated' : 'reported';
  }
  function isEstimatedRow(row) {
    return tokenProvenance(row) !== 'reported';
  }
  function provenanceLabel(row) {
    return PROVENANCE_LABELS[tokenProvenance(row)] || PROVENANCE_LABELS.reported;
  }

  /**
   * Compact number to three significant digits (2.62B, 26.2M, 262M, 842).
   */
  function formatCompactSig(num) {
    const n = Number(num);
    if (!Number.isFinite(n)) return '0';
    const abs = Math.abs(n);
    const units = [['', 1], ['K', 1e3], ['M', 1e6], ['B', 1e9], ['T', 1e12]];
    let idx = 0;
    while (idx < units.length - 1 && abs >= units[idx + 1][1]) idx += 1;
    let scaled = n / units[idx][1];
    if (Math.abs(Number(scaled.toPrecision(3))) >= 1000 && idx < units.length - 1) {
      idx += 1;
      scaled = n / units[idx][1];
    }
    if (idx === 0) return String(Math.round(n));
    const magnitude = Math.abs(scaled);
    const digits = magnitude >= 100 ? 0 : (magnitude >= 10 ? 1 : 2);
    return `${scaled.toFixed(digits)}${units[idx][0]}`;
  }

  /** US-formatted dollar amount, e.g. $2,663.05. */
  function formatUsd(value, decimals = 2) {
    const n = Number(value);
    const safe = Number.isFinite(n) ? n : 0;
    return `${safe < 0 ? '-' : ''}$${Math.abs(safe).toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })}`;
  }

  /** Dollars per 1M tokens: three decimals under $1, two otherwise. */
  function formatRate(value) {
    const n = Number(value);
    const safe = Number.isFinite(n) ? n : 0;
    return formatUsd(safe, Math.abs(safe) < 1 ? 3 : 2);
  }

  /** Integer with thousands separators. */
  function formatInt(value) {
    const n = Number(value);
    return (Number.isFinite(n) ? Math.round(n) : 0).toLocaleString('en-US');
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
    toast.className = 'toast';
    toast.innerHTML = `${type === 'error' ? '<span class="toast__dot" aria-hidden="true"></span>' : ''}<span>${escapeHtml(String(message))}</span>`;

    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 130);
    }, 4000);
  }

  window.DashboardUtils = {
    formatCompactNumber,
    formatDateTime,
    providerBadge,
    toolKey,
    toolLabel,
    toolDotHtml,
    formatCompactSig,
    formatUsd,
    formatRate,
    formatInt,
    escapeHtml,
    showToast,
    tokenProvenance,
    isEstimatedRow,
    provenanceLabel,
    EST_TOOLTIP,
  };
})();
