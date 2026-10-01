(function () {
  'use strict';

  const ROWS = '#model-ledger [data-model-key], #tool-donut-legend [data-tool]';
  const METRICS = ['total_tokens', 'call_count', 'cost_cached_usd'];
  let data = {};
  let focus = null;
  let pinned = false;
  let keyboardFocus = null;
  let skylineFocused = false;
  let clearTimer = 0;
  let lastTouch = -Infinity;
  let bound = false;

  function same(a, b) {
    return a === b || Boolean(a && b && a.type === b.type
      && (a.type === 'model' ? a.key === b.key : a.tool === b.tool));
  }

  function model(key) {
    return (data.models || []).find((row) => row.key === key);
  }

  function identity(key) {
    return (data.model_index || {})[key] || model(key) || {};
  }

  function exists(value) {
    if (!value) return false;
    if (value.type === 'model') return Boolean(model(value.key) || (data.model_index || {})[value.key]);
    return Object.values(data.model_index || {}).concat(data.models || [], data.sessions || [])
      .some((row) => row.tool === value.tool);
  }

  function series(map, base, value) {
    if (!map || !Array.isArray(base)) return null;
    // Each map has its own keys: the calendar can include models outside the selected window.
    const keys = value.type === 'model' ? [value.key]
      : Object.keys(map).filter((key) => (data.model_index[key] || {}).tool === value.tool);
    return base.map((day, index) => {
      const cell = { date: day.date, total_tokens: 0, call_count: 0, cost_cached_usd: 0 };
      keys.forEach((key) => {
        const source = map[key] && map[key][index];
        METRICS.forEach((metric) => {
          const number = Number(source && source[metric]);
          if (Number.isFinite(number)) cell[metric] += number;
        });
      });
      return cell;
    });
  }

  function resolve(value) {
    if (!value) return null;
    const entry = value.type === 'model' ? identity(value.key) : null;
    const tool = entry ? entry.tool : value.tool;
    const canResolve = Boolean(data.timeline_by_model && data.model_index);
    return {
      ...value,
      tool,
      label: entry ? entry.model || 'Unknown model' : window.DashboardUtils.toolLabel(tool),
      cost: entry ? Number((model(value.key) || {}).est_cost_cached_usd) || 0 : null,
      timeline: canResolve ? series(data.timeline_by_model, data.timeline, value) : null,
      heatmap: canResolve ? series(data.heatmap_by_model, data.heatmap_daily, value) : null,
    };
  }

  function paintRows(resolved) {
    document.querySelectorAll('#model-ledger [data-model-key]').forEach((row) => {
      const matches = resolved && (resolved.type === 'model'
        ? row.dataset.modelKey === resolved.key : identity(row.dataset.modelKey).tool === resolved.tool);
      row.classList.toggle('is-focused', Boolean(matches));
      row.classList.toggle('is-dimmed', Boolean(resolved && !matches));
    });
    document.querySelectorAll('#tool-donut-legend [data-tool]').forEach((row) => {
      const matches = resolved && row.dataset.tool === resolved.tool;
      row.classList.toggle('is-focused', Boolean(matches));
      row.classList.toggle('is-dimmed', Boolean(resolved && !matches));
    });
  }

  function cancelClear() {
    window.clearTimeout(clearTimer);
    clearTimer = 0;
  }

  function apply({ animate = true, charts = true } = {}) {
    const resolved = resolve(focus);
    paintRows(resolved);
    if (charts && window.DashboardCharts) window.DashboardCharts.setFocus(resolved, { animate });
    const cells = resolved && resolved.heatmap;
    if (window.DashboardSkyline && (cells || skylineFocused)) {
      window.DashboardSkyline.setFocus(cells);
      skylineFocused = Boolean(cells);
    }
  }

  function change(value, pin = false) {
    cancelClear();
    if (value && !exists(value)) return;
    const changed = !same(focus, value);
    focus = value;
    pinned = Boolean(value && pin);
    if (changed) apply();
  }

  function enter(value) {
    cancelClear();
    if (!pinned) change(value);
  }

  function leave() {
    if (pinned) return;
    cancelClear();
    clearTimer = window.setTimeout(() => change(keyboardFocus && exists(keyboardFocus) ? keyboardFocus : null), 60);
  }

  function tap(value) {
    change(pinned && same(focus, value) ? null : value, true);
  }

  function rowFor(target) {
    return target && target.closest ? target.closest(ROWS) : null;
  }

  function rowFocus(row) {
    return row.hasAttribute('data-model-key')
      ? { type: 'model', key: row.dataset.modelKey } : { type: 'tool', tool: row.dataset.tool };
  }

  function bind() {
    if (bound) return;
    bound = true;
    // Delegate to stable ancestors: every refresh replaces the ledger and legend rows.
    document.addEventListener('pointerover', (event) => {
      const row = rowFor(event.target);
      if (!row || event.pointerType === 'touch' || row.contains(event.relatedTarget)) return;
      enter(rowFocus(row));
    });
    document.addEventListener('pointerout', (event) => {
      const row = rowFor(event.target);
      if (!row || event.pointerType === 'touch' || row.contains(event.relatedTarget)) return;
      leave();
    });
    document.addEventListener('focusin', (event) => {
      const row = rowFor(event.target);
      if (row && performance.now() - lastTouch > 600) {
        keyboardFocus = rowFocus(row);
        change(keyboardFocus);
      } else if (!row && keyboardFocus) {
        keyboardFocus = null;
        leave();
      }
    });
    document.addEventListener('focusout', (event) => {
      if (rowFor(event.target)) {
        keyboardFocus = null;
        leave();
      }
    });
    document.addEventListener('pointerdown', (event) => {
      if (event.pointerType !== 'touch') return;
      lastTouch = performance.now();
      if (!pinned) return;
      const source = focus.type === 'model' ? '#model-ledger' : '#tool-donut-legend, #chart-cost-by-tool';
      if (!event.target.closest(source)) change(null);
    }, true);
    document.addEventListener('pointerup', (event) => {
      if (event.pointerType !== 'touch') return;
      const row = rowFor(event.target);
      if (row) tap(rowFocus(row));
    });
    document.addEventListener('dashboard:tool-focus', (event) => {
      const { tool, interaction } = event.detail;
      if (interaction === 'leave') leave();
      else if (interaction === 'tap') tap(tool ? { type: 'tool', tool } : null);
      else if (tool) enter({ type: 'tool', tool });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && focus) {
        keyboardFocus = null;
        change(null);
      }
    });
  }

  window.DashboardLinked = {
    update(payload, ctx) {
      data = payload || {};
      bind();
      cancelClear();
      const hadFocus = Boolean(focus);
      if (focus && !exists(focus)) {
        focus = null;
        pinned = false;
      }
      // Refreshes patch without motion; the first unfocused render keeps its boot draw-in.
      apply({ animate: false, charts: hadFocus });
    },
  };
})();
