/**
 * DashboardCharts - charts, heatmap, donut and sparklines for the AI usage dashboard.
 *
 * Encoding: amber (--money) = dollars, steel (--tokens) = tokens, ink (--count) = counts,
 * tool hues = identity only. Every color is read from the CSS custom properties defined in
 * dashboard.css (see readPalette) and re-read on `themechange` and OS scheme changes.
 *
 * Public API: window.DashboardCharts = { updateCharts(data, ctx), setFocus(focus) }
 * updateCharts resolves its own mounts by id on every call; missing mounts are skipped silently.
 */
(function () {
  'use strict';

  const FONT_FAMILY = 'Archivo, ui-sans-serif, system-ui, sans-serif';
  const EMPTY_TEXT = 'No usage in this period';
  const METRIC_STORAGE_KEY = 'aiud.heatmapMetric';
  const HEATMAP_METRICS = ['cost', 'tokens', 'calls'];
  const HEATMAP_DAYS = 30;
  const SPARK_POINTS = 30;
  const PALETTE_TOKENS = [
    'bg', 'surface', 'surface-2', 'surface-3', 'line', 'line-strong', 'ink', 'ink-2', 'ink-3',
    'money', 'tokens', 'count', 'tool-codex', 'tool-claude', 'tool-agy', 'tool-other',
  ];
  const MISSING_TOKEN_COLOR = 'gray';

  const TOOL_KEYS = {
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
  const TOOL_NAMES = { codex: 'Codex', claude: 'Claude Code', agy: 'Antigravity', other: 'Other' };

  let palette = {};
  let paletteReady = false;
  let initialized = false;
  let chartAnimationDefaults = null;

  const charts = { daily: null, cache: null, hourly: null, donut: null };
  const dailyState = { days: [], totals: { cost: 0, calls: 0, tokens: 0 }, multiYear: false };
  const heatmapState = { cells: [], timezone: '', metric: 'cost', structureKey: '' };
  let linkedFocus = null;
  let focusedDays = null;
  let focusedTotals = null;
  let totalHeatmapCells = [];

  /* ------------------------------------------------------------------ helpers */

  function utils() {
    return window.DashboardUtils || {};
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function escapeHtml(value) {
    const fn = utils().escapeHtml;
    if (fn) return fn(value === 0 ? '0' : value);
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  }

  function guard(name, fn) {
    try {
      fn();
    } catch (error) {
      if (window.console && console.error) console.error(`DashboardCharts.${name} failed`, error);
    }
  }

  function prefersReducedMotion() {
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch {
      return false;
    }
  }

  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const moneyFormat = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function fmtInt(value) {
    return numberFormat.format(Math.round(num(value)));
  }

  function fmtCompact(value) {
    const fn = utils().formatCompactNumber;
    const n = num(value);
    return fn ? fn(n) : String(n);
  }

  function fmtUsd(value) {
    const n = num(value);
    if (n === 0) return '$0.00';
    if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
    return `$${moneyFormat.format(n)}`;
  }

  /** Short dollar label for direct labels: $70.10, $1,234, $12.3K. */
  function fmtUsdShort(value) {
    const n = num(value);
    if (n >= 1e4) return `$${Number((n / 1e3).toFixed(1))}K`;
    if (n >= 1e3) return `$${numberFormat.format(Math.round(n))}`;
    return fmtUsd(n);
  }

  function trimTick(n, suffix) {
    return `${Number(n.toFixed(1))}${suffix}`;
  }

  /** Compact axis tick without a trailing ".0" (2K, 1.5M). */
  function compactTick(value) {
    const n = num(value);
    const abs = Math.abs(n);
    if (abs >= 1e9) return trimTick(n / 1e9, 'B');
    if (abs >= 1e6) return trimTick(n / 1e6, 'M');
    if (abs >= 1e3) return trimTick(n / 1e3, 'K');
    return String(Number(n.toFixed(2)));
  }

  function moneyTick(value) {
    const n = num(value);
    if (n === 0) return '$0';
    if (Math.abs(n) >= 1e3) return `$${compactTick(n)}`;
    if (Number.isInteger(n)) return `$${n}`;
    return `$${n.toFixed(2).replace(/(\.\d*?)0+$/, '$1')}`;
  }

  function fmtPercent(value) {
    const n = num(value);
    return `${Number(n.toFixed(n >= 99.95 || n === 0 ? 0 : 1))}%`;
  }

  function plural(n, one, many) {
    return n === 1 ? one : many;
  }

  const dateShortFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const dateDayFormat = new Intl.DateTimeFormat(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC',
  });
  const dateDayYearFormat = new Intl.DateTimeFormat(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  });

  function parseDay(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return null;
    return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  }

  function fmtTickDate(value, multiYear) {
    const date = parseDay(value);
    if (!date) return String(value || '');
    const text = dateShortFormat.format(date);
    return multiYear ? `${text} ’${String(date.getUTCFullYear()).slice(-2)}` : text;
  }

  function fmtDay(value, withYear) {
    const date = parseDay(value);
    if (!date) return String(value || 'Unknown date');
    return (withYear ? dateDayYearFormat : dateDayFormat).format(date);
  }

  function todayKey(timezone) {
    try {
      const options = { year: 'numeric', month: '2-digit', day: '2-digit' };
      if (timezone) options.timeZone = String(timezone);
      return new Intl.DateTimeFormat('en-CA', options).format(new Date());
    } catch {
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    }
  }

  function toolKey(row) {
    if (!row || typeof row !== 'object') return 'other';
    const tool = String(row.tool || '').toLowerCase();
    const raw = tool && tool !== 'all' ? tool : String(row.provider || tool || '').toLowerCase();
    return TOOL_KEYS[raw] || 'other';
  }

  /* ------------------------------------------------------------------ theme layer */

  let colorContext = null;
  const colorCache = new Map();

  /** Resolve any CSS color string to [r, g, b, a] using the canvas color parser. */
  function parseColor(input) {
    const key = String(input);
    if (colorCache.has(key)) return colorCache.get(key);
    if (!colorContext) colorContext = document.createElement('canvas').getContext('2d');
    colorContext.fillStyle = '#000000';
    colorContext.fillStyle = key;
    const resolved = String(colorContext.fillStyle);
    let rgba = [0, 0, 0, 1];
    if (resolved[0] === '#') {
      rgba = [
        parseInt(resolved.slice(1, 3), 16),
        parseInt(resolved.slice(3, 5), 16),
        parseInt(resolved.slice(5, 7), 16),
        1,
      ];
    } else {
      const match = /rgba?\(([^)]+)\)/.exec(resolved);
      if (match) {
        const parts = match[1].split(',').map((part) => parseFloat(part));
        rgba = [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
      }
    }
    colorCache.set(key, rgba);
    return rgba;
  }

  function withAlpha(color, alpha) {
    const [r, g, b, a] = parseColor(color);
    return `rgba(${r}, ${g}, ${b}, ${Number((a * alpha).toFixed(3))})`;
  }

  function readPalette() {
    const style = getComputedStyle(document.documentElement);
    const next = {};
    PALETTE_TOKENS.forEach((name) => {
      next[name] = style.getPropertyValue(`--${name}`).trim() || MISSING_TOKEN_COLOR;
    });
    palette = next;
    paletteReady = true;
  }

  function applyChartDefaults() {
    if (typeof Chart === 'undefined') return;
    const defaults = Chart.defaults;
    if (!chartAnimationDefaults && defaults.animation && typeof defaults.animation === 'object') {
      chartAnimationDefaults = { ...defaults.animation };
    }
    defaults.font.family = FONT_FAMILY;
    defaults.font.size = 11;
    defaults.color = palette['ink-3'];
    defaults.borderColor = palette.line;
    defaults.responsive = true;
    defaults.maintainAspectRatio = false;
    defaults.plugins.legend.display = false;
    defaults.plugins.tooltip.enabled = false;
    defaults.elements.line.borderDash = [];
    defaults.elements.line.borderWidth = 1.5;
    defaults.transitions.active.animation.duration = 0;
    defaults.animation = prefersReducedMotion() ? false : {
      ...(chartAnimationDefaults || {}),
      duration: 520,
      easing: 'easeOutCubic',
    };
  }

  function refreshTheme() {
    readPalette();
    applyChartDefaults();
    Object.keys(charts).forEach((key) => {
      const inst = charts[key];
      if (!inst || !inst.chart) return;
      guard('refreshTheme', () => inst.chart.update('none'));
    });
  }

  function tickFont() {
    return { family: FONT_FAMILY, size: 11 };
  }

  function tickColor() {
    return palette['ink-3'];
  }

  function gridColor(context) {
    return context.tick && context.tick.value === 0 ? palette['line-strong'] : palette.line;
  }

  /* ------------------------------------------------------------------ mounts and empty states */

  /** Returns the canvas for a chart mount id. */
  function canvasFor(id) {
    return byId(id);
  }

  function frameFor(canvas) {
    return canvas.closest('.chart-frame') || canvas.parentElement;
  }

  function setFrameEmpty(canvas, empty) {
    const frame = frameFor(canvas);
    if (!frame) return;
    let note = frame.querySelector(':scope > .chart-empty');
    if (empty) {
      if (!note) {
        note = document.createElement('div');
        note.className = 'chart-empty';
        note.textContent = EMPTY_TEXT;
        frame.appendChild(note);
      }
      frame.classList.add('is-empty');
    } else {
      frame.classList.remove('is-empty');
      if (note) note.remove();
    }
  }

  function describeCanvas(canvas, text) {
    if (!canvas.hasAttribute('aria-labelledby') && !canvas.getAttribute('aria-label')) {
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', text);
    }
  }

  function discardIfStale(key, canvas) {
    const inst = charts[key];
    if (inst && (inst.canvas !== canvas || !canvas.isConnected)) {
      if (inst.unbindFocus) inst.unbindFocus();
      try { inst.chart.destroy(); } catch { /* already gone */ }
      charts[key] = null;
    }
    if (!charts[key] && typeof Chart !== 'undefined') {
      const stray = Chart.getChart(canvas);
      if (stray) stray.destroy();
    }
    return charts[key];
  }

  function commit(inst) {
    // Only the very first render animates (and only when motion is allowed).
    inst.chart.update(inst.rendered ? 'none' : undefined);
    inst.rendered = true;
  }

  function scheduleUpdate(chart) {
    if (chart.$aiudPending) return;
    chart.$aiudPending = window.requestAnimationFrame(() => {
      chart.$aiudPending = 0;
      if (!chart.canvas || !chart.ctx) return;
      chart.update('none');
    });
  }

  /* ------------------------------------------------------------------ shared floating tooltip */

  let tooltipEl = null;
  let tooltipOwner = null;

  function tooltipNode() {
    if (tooltipEl && tooltipEl.isConnected) return tooltipEl;
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'chart-tooltip';
    tooltipEl.setAttribute('aria-hidden', 'true');
    tooltipEl.hidden = true;
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function hideTooltip(owner) {
    if (!tooltipEl) return;
    if (owner && tooltipOwner !== owner) return;
    tooltipEl.hidden = true;
    tooltipOwner = null;
  }

  function swatchMarkup(row) {
    if (!row.swatch) return '<i class="chart-swatch chart-swatch--none"></i>';
    return `<i class="chart-swatch chart-swatch--${row.shape || 'square'} chart-swatch--${row.swatch}"></i>`;
  }

  function tooltipMarkup(content) {
    const rows = (content.rows || []).map((row) => (
      `<div class="chart-tooltip__row">${swatchMarkup(row)}<span class="chart-tooltip__label">${escapeHtml(row.label)}</span><span class="chart-tooltip__value">${escapeHtml(row.value)}</span></div>`
    )).join('');
    return `<div class="chart-tooltip__title">${escapeHtml(content.title)}</div>${rows}`;
  }

  function showTooltip(owner, content, anchor) {
    const el = tooltipNode();
    el.innerHTML = tooltipMarkup(content);
    el.hidden = false;
    tooltipOwner = owner;
    const margin = 8;
    const gap = 14;
    const width = el.offsetWidth;
    const height = el.offsetHeight;
    const viewWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewHeight = window.innerHeight;
    let x = anchor.x + gap;
    if (x + width > viewWidth - margin) x = anchor.x - gap - width;
    x = clamp(x, margin, Math.max(margin, viewWidth - width - margin));
    const y = clamp(anchor.y - height / 2, margin, Math.max(margin, viewHeight - height - margin));
    el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
  }

  /**
   * Chart.js external tooltip handler factory. `build(index, chart)` returns
   * { title, rows: [{ swatch, shape, label, value }] }. `snapToIndex` anchors on the hovered x.
   */
  function externalTooltip(ui, build, snapToIndex) {
    return (context) => {
      const { chart, tooltip } = context;
      const active = typeof tooltip.getActiveElements === 'function' ? tooltip.getActiveElements() : [];
      if (tooltip.opacity === 0 || !active.length || !tooltip.dataPoints || !tooltip.dataPoints.length) {
        hideTooltip(chart.id);
        return;
      }
      const index = tooltip.dataPoints[0].dataIndex;
      const content = build(index, chart);
      if (!content) {
        hideTooltip(chart.id);
        return;
      }
      const rect = chart.canvas.getBoundingClientRect();
      let x = tooltip.caretX;
      let y = tooltip.caretY;
      if (ui.pointer) {
        y = ui.pointer.y;
        x = ui.pointer.x;
      }
      if (snapToIndex && chart.scales.x) {
        x = chart.scales.x.getPixelForValue(index);
      }
      showTooltip(chart.id, content, { x: rect.left + x, y: rect.top + y });
    };
  }

  function pointerPlugin(ui) {
    return {
      id: 'aiudPointer',
      beforeEvent(chart, args) {
        const event = args.event;
        ui.pointer = event && event.x != null ? { x: event.x, y: event.y } : null;
        if (event && event.type === 'mouseout') hideTooltip(chart.id);
      },
    };
  }

  /* ------------------------------------------------------------------ crosshair, hover points, record flags */

  function roundRectPath(ctx, x, y, width, height, radius) {
    if (typeof ctx.roundRect === 'function') {
      ctx.roundRect(x, y, width, height, radius);
      return;
    }
    const r = Math.min(radius, width / 2, height / 2);
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + width - r, y);
    ctx.quadraticCurveTo(x + width, y, x + width, y + r);
    ctx.lineTo(x + width, y + height - r);
    ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    ctx.lineTo(x + r, y + height);
    ctx.quadraticCurveTo(x, y + height, x, y + height - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function drawRecordFlag(ctx, chart, ui, point) {
    const area = chart.chartArea;
    const label = ui.peakLabel;
    const value = ui.peakValue;
    if (!area || !label || !value || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return;

    const fontLabel = `500 11px ${FONT_FAMILY}`;
    const fontValue = `600 11px ${FONT_FAMILY}`;
    ctx.save();
    ctx.globalAlpha = ui.peakOpacity;
    ctx.font = fontLabel;
    const labelWidth = ctx.measureText(label).width;
    const spaceWidth = ctx.measureText(' ').width;
    ctx.font = fontValue;
    const valueWidth = ctx.measureText(value).width;
    const width = Math.ceil(labelWidth + spaceWidth + valueWidth + 12);
    const height = 18;
    const poleLength = 14;
    const poleTop = Math.max(area.top, point.y - poleLength);
    const fullRectY = poleTop - height;
    const sideHung = fullRectY < area.top;
    const rectY = sideHung
      ? clamp(poleTop - height / 2, area.top, area.bottom - height)
      : fullRectY;
    const poleEndY = sideHung ? clamp(poleTop, rectY, rectY + height) : rectY + height;

    let rectX = point.x;
    if (rectX + width > area.right) rectX = point.x - width;
    if (rectX < area.left) rectX = area.left;
    if (rectX + width > area.right) rectX = area.right - width;

    const token = ui.peakToken || 'money';
    const border = palette['line-strong'] === MISSING_TOKEN_COLOR ? palette.line : palette['line-strong'];
    ctx.beginPath();
    ctx.strokeStyle = palette['ink-3'];
    ctx.lineWidth = 1;
    ctx.moveTo(point.x + 0.5, poleEndY);
    ctx.lineTo(point.x + 0.5, point.y);
    ctx.stroke();

    ctx.beginPath();
    roundRectPath(ctx, rectX + 0.5, rectY + 0.5, width - 1, height - 1, 4);
    ctx.fillStyle = palette.surface;
    ctx.fill();
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.stroke();

    const textX = rectX + 6;
    const textY = rectY + height / 2;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.font = fontLabel;
    ctx.fillStyle = palette['ink-2'];
    ctx.fillText(label, textX, textY);
    ctx.font = fontValue;
    ctx.fillStyle = palette.ink;
    ctx.fillText(value, textX + labelWidth + spaceWidth, textY);

    ctx.beginPath();
    ctx.arc(point.x, point.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = palette.surface;
    ctx.fill();
    ctx.strokeStyle = palette[token];
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();
  }

  function cancelBootFlagFade(inst) {
    if (inst.bootFlagTimer) window.clearTimeout(inst.bootFlagTimer);
    if (inst.bootFlagFrame) window.cancelAnimationFrame(inst.bootFlagFrame);
    inst.bootFlagTimer = 0;
    inst.bootFlagFrame = 0;
  }

  function fadeBootFlag(inst) {
    const ui = inst.ui;
    ui.peakOpacity = 0;
    inst.bootFlagTimer = window.setTimeout(() => {
      inst.bootFlagTimer = 0;
      let startedAt = 0;
      const draw = (now) => {
        if (!inst.chart || !inst.chart.ctx) return;
        if (!startedAt) startedAt = now;
        ui.peakOpacity = clamp((now - startedAt) / 200, 0, 1);
        inst.chart.draw();
        if (ui.peakOpacity < 1) {
          inst.bootFlagFrame = window.requestAnimationFrame(draw);
        } else {
          inst.bootFlagFrame = 0;
        }
      };
      inst.bootFlagFrame = window.requestAnimationFrame(draw);
    }, 1100);
  }

  function finishBootAnimation(inst) {
    inst.bootAnimating = false;
    inst.bootAnimationTimer = 0;
    if (inst.fontRefreshPending && inst.chart && inst.chart.ctx) {
      inst.fontRefreshPending = false;
      inst.chart.update('none');
    }
  }

  function interactionPlugin(ui) {
    return {
      id: 'aiudInteraction',
      afterEvent(chart, args) {
        const event = args.event;
        let index = -1;
        if (event.type !== 'mouseout') {
          const active = chart.getActiveElements();
          if (active.length) index = active[0].index;
        }
        if (index !== ui.hover) {
          ui.hover = index;
          if (ui.onHoverChange) ui.onHoverChange(index);
          if (ui.highlightBars) scheduleUpdate(chart);
        }
      },
      beforeDatasetsDraw(chart) {
        if (ui.hover < 0 || !chart.scales.x) return;
        const { top, bottom, left, right } = chart.chartArea;
        const x = chart.scales.x.getPixelForValue(ui.hover);
        if (!(x >= left && x <= right)) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.strokeStyle = palette['line-strong'];
        ctx.lineWidth = 1;
        const px = Math.round(x) + 0.5;
        ctx.moveTo(px, top);
        ctx.lineTo(px, bottom);
        ctx.stroke();
        ctx.restore();
      },
      afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        if (ui.hover >= 0) {
          (ui.points || []).forEach((spec) => {
            const meta = chart.getDatasetMeta(spec.datasetIndex);
            const point = meta && meta.data[ui.hover];
            if (!point || point.skip || !Number.isFinite(point.y)) return;
            ctx.save();
            ctx.beginPath();
            ctx.arc(point.x, point.y, 5, 0, Math.PI * 2);
            ctx.lineWidth = 2;
            ctx.strokeStyle = palette.surface;
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(point.x, point.y, 4, 0, Math.PI * 2);
            ctx.fillStyle = palette[spec.token];
            ctx.fill();
            ctx.restore();
          });
        }
        if (!ui.hidePeak && ui.peakIndex >= 0 && ui.peakLabel && ui.peakValue && ui.peakOpacity > 0) {
          const meta = chart.getDatasetMeta(ui.peakDatasetIndex);
          const point = meta && meta.data[ui.peakIndex];
          if (!point || point.skip || !Number.isFinite(point.y)) return;
          drawRecordFlag(ctx, chart, ui, point);
        }
      },
    };
  }

  /* ------------------------------------------------------------------ data normalizers */

  function normalizeTimeline(timeline) {
    return (Array.isArray(timeline) ? timeline : [])
      .filter((row) => row && typeof row === 'object' && row.date)
      .map((row) => {
        const cached = num(row.cached_input);
        const uncached = num(row.uncached_input);
        const cacheable = cached + uncached;
        let cacheRate = null;
        if (cacheable > 0) {
          cacheRate = row.cache_hit_rate !== undefined && row.cache_hit_rate !== null
            ? num(row.cache_hit_rate)
            : (cached / cacheable) * 100;
        }
        return {
          date: String(row.date),
          cost: num(row.cost_cached_usd),
          calls: num(row.call_count),
          tokens: num(row.total_tokens),
          cacheRate,
        };
      })
      .sort((a, b) => a.date.localeCompare(b.date));
  }

  function spansYears(days) {
    if (!days.length) return false;
    return days[0].date.slice(0, 4) !== days[days.length - 1].date.slice(0, 4);
  }

  /* ------------------------------------------------------------------ overlay charts (daily, hourly) */

  const NICE_STEPS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

  /** A rounded top tick and step; both visible scales use four aligned intervals. */
  function niceAxis(peak, divisions, headroom, integer) {
    const top = peak > 0 ? peak * (1 + headroom) : 0;
    const raw = top > 0 ? top / divisions : 1;
    const base = Math.pow(10, Math.floor(Math.log10(raw)));
    let step = 10 * base;
    for (let i = 0; i < NICE_STEPS.length; i += 1) {
      if (NICE_STEPS[i] * base >= raw * 0.9999) {
        step = NICE_STEPS[i] * base;
        break;
      }
    }
    if (integer) step = Math.max(1, Math.ceil(step));
    step = Number(step.toPrecision(12));
    return { step, max: Number((step * divisions).toPrecision(12)) };
  }

  function overlayScale(position, format, showGrid) {
    return {
      type: 'linear',
      position,
      min: 0,
      max: 4,
      border: { display: false },
      grid: { display: showGrid, color: gridColor, lineWidth: 1, drawTicks: false },
      ticks: {
        color: tickColor,
        font: tickFont,
        padding: 8,
        autoSkip: false,
        maxTicksLimit: 5,
        stepSize: 1,
        callback: (value) => format(value),
      },
    };
  }

  function axisCaptionPlugin() {
    return {
      id: 'aiudAxisCaptions',
      afterDraw(chart) {
        const { ctx, chartArea: area } = chart;
        ctx.save();
        ctx.font = `400 11px ${FONT_FAMILY}`;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        [
          { label: 'Spent', token: 'money', right: false },
          { label: 'Calls', token: 'count', right: true },
        ].forEach((caption) => {
          const width = 14 + ctx.measureText(caption.label).width;
          const x = caption.right ? area.right - width : area.left;
          const y = area.top - 12;
          ctx.beginPath();
          ctx.strokeStyle = palette[caption.token];
          ctx.lineWidth = 2;
          ctx.moveTo(x, y);
          ctx.lineTo(x + 10, y);
          ctx.stroke();
          ctx.fillStyle = palette['ink-3'];
          ctx.fillText(caption.label, x + 14, y);
        });
        ctx.restore();
      },
    };
  }

  function overlayLine(label, axis, token, width, order) {
    return {
      type: 'line',
      label,
      data: [],
      yAxisID: axis,
      order,
      borderColor: () => palette[token],
      borderWidth: width,
      borderJoinStyle: 'round',
      cubicInterpolationMode: 'monotone',
      fill: false,
      pointRadius: 0,
      pointHoverRadius: 0,
      pointHitRadius: 0,
      clip: false,
    };
  }

  function createOverlayChart(canvas, spec) {
    const ui = {
      hover: -1,
      pointer: null,
      peakIndex: -1,
      peakLabel: '',
      peakValue: '',
      peakToken: 'money',
      peakOpacity: 1,
      peakDatasetIndex: 2,
      highlightBars: true,
      points: [{ datasetIndex: 1, token: 'count' }, { datasetIndex: 2, token: 'money' }],
      onHoverChange: spec.onHoverChange,
    };
    const tooltip = spec.tooltipBuilder
      ? { enabled: false, mode: 'index', intersect: false, external: externalTooltip(ui, spec.tooltipBuilder, true) }
      : { enabled: false };
    const bootConfig = spec.bootAnimation ? bootAnimationOptions(spec.dataCount) : null;

    const chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: [],
        datasets: [
          {
            type: 'bar',
            label: 'Tokens',
            data: [],
            yAxisID: 'yTokens',
            order: 3,
            backgroundColor: (context) => withAlpha(palette.tokens, context.dataIndex === ui.hover ? 0.7 : 0.35),
            hoverBackgroundColor: () => withAlpha(palette.tokens, 0.7),
            borderWidth: 0,
            borderRadius: { topLeft: 3, topRight: 3, bottomLeft: 0, bottomRight: 0 },
            borderSkipped: 'start',
            barPercentage: 0.68,
            categoryPercentage: 1,
            maxBarThickness: 18,
          },
          overlayLine('API calls', 'yCalls', 'count', 2, 2),
          overlayLine('Spent', 'yCost', 'money', 2.5, 1),
        ],
      },
      options: {
        ...(bootConfig ? { animation: false, datasets: bootConfig.datasets } : {}),
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 24, right: 4, bottom: 0, left: 0 } },
        interaction: { mode: 'index', intersect: false, axis: 'x' },
        plugins: { legend: { display: false }, tooltip },
        scales: {
          x: {
            type: 'category',
            offset: true,
            border: { display: false },
            grid: { display: false, drawTicks: false },
            ticks: {
              color: tickColor,
              font: tickFont,
              padding: 8,
              minRotation: 0,
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: 14,
              maxTicksLimit: spec.xMaxTicks,
              callback(value) {
                return spec.xTick(this.getLabelForValue(value));
              },
            },
          },
          yCost: overlayScale('left', moneyTick, true),
          yCalls: overlayScale('right', compactTick, false),
          yTokens: { type: 'linear', display: false, min: 0, max: 1 },
        },
      },
      plugins: [pointerPlugin(ui), interactionPlugin(ui), axisCaptionPlugin()],
    });

    return {
      chart, canvas, ui, rendered: false,
      bootFlagTimer: 0, bootFlagFrame: 0, bootAnimationTimer: 0,
      bootAnimating: false, fontRefreshPending: false,
      bootAnimationConfig: bootConfig && bootConfig.animation,
    };
  }

  function bootAnimationOptions(count) {
    const lineStep = 1100 / Math.max(1, count);
    const lineDelay = (context) => (context.type === 'data' ? context.index * lineStep : 0);
    const previousY = (context) => {
      if (context.index === 0) {
        const dataset = context.chart.data.datasets[context.datasetIndex];
        return context.chart.scales[dataset.yAxisID].getPixelForValue(0);
      }
      return context.chart.getDatasetMeta(context.datasetIndex).data[context.index - 1]
        .getProps(['y'], true).y;
    };

    return {
      animation: {
        duration: 420,
        easing: 'easeOutCubic',
        x: {
          type: 'number',
          easing: 'linear',
          duration: lineStep,
          from: NaN,
          delay: lineDelay,
        },
        y: {
          type: 'number',
          easing: 'linear',
          duration: lineStep,
          from: previousY,
          delay: lineDelay,
        },
      },
      datasets: {
        bar: {
          animations: {
            y: {
              type: 'number',
              duration: 420,
              easing: 'easeOutCubic',
              from: (context) => context.chart.scales.yTokens.getPixelForValue(0),
              delay(context) {
                if (context.type !== 'data' || context.mode !== 'default') return 0;
                return count > 1 ? 900 * context.index / (count - 1) : 0;
              },
            },
          },
        },
      },
    };
  }

  function prepareBootDraw(chart) {
    // Reset data elements after scales have their final bounds; bars begin on the real baseline.
    chart.update('reset');
    [1, 2].forEach((datasetIndex) => {
      chart.getDatasetMeta(datasetIndex).data.forEach((point) => { point.x = NaN; });
    });
  }

  function updateOverlayChart(inst, labels, tokens, calls, costs, peak, boot) {
    const chart = inst.chart;
    chart.data.labels = labels;
    chart.data.datasets[0].data = tokens;
    chart.data.datasets[1].data = calls;
    chart.data.datasets[2].data = costs;
    let peakIndex = -1;
    let peakValue = 0;
    if (peak && Array.isArray(peak.values)) {
      peak.values.forEach((value, index) => {
        if (value > peakValue) {
          peakValue = value;
          peakIndex = index;
        }
      });
    }
    const tokenPeak = tokens.reduce((m, v) => Math.max(m, v), 0);
    const costAxis = niceAxis(costs.reduce((m, v) => Math.max(m, v), 0), 4, 0.12, false);
    const callsAxis = niceAxis(calls.reduce((m, v) => Math.max(m, v), 0), 4, 0.12, true);
    chart.options.scales.yTokens.max = tokenPeak > 0 ? tokenPeak * 1.12 : 1;
    chart.options.scales.yCost.max = costAxis.max;
    chart.options.scales.yCost.ticks.stepSize = costAxis.step;
    chart.options.scales.yCalls.max = callsAxis.max;
    chart.options.scales.yCalls.ticks.stepSize = callsAxis.step;
    inst.ui.peakIndex = peakIndex;
    inst.ui.peakLabel = peakIndex >= 0 ? peak.label : '';
    inst.ui.peakValue = peakIndex >= 0 ? peak.format(peakValue, peakIndex) : '';
    inst.ui.peakToken = peak ? peak.token : 'money';
    inst.ui.peakDatasetIndex = peak && Number.isInteger(peak.datasetIndex) ? peak.datasetIndex : 2;
    const bootDraw = Boolean(boot && !inst.rendered && !prefersReducedMotion());
    cancelBootFlagFade(inst);
    if (inst.bootAnimationTimer) window.clearTimeout(inst.bootAnimationTimer);
    inst.bootAnimationTimer = 0;
    inst.bootAnimating = false;
    inst.fontRefreshPending = false;
    inst.ui.peakOpacity = bootDraw && peakIndex >= 0 ? 0 : 1;
    if (bootDraw) {
      chart.config.options.animation = inst.bootAnimationConfig;
      inst.bootAnimating = true;
      prepareBootDraw(chart);
    }
    if (inst.ui.hover >= labels.length) inst.ui.hover = -1;
    commit(inst);
    if (bootDraw) {
      if (peakIndex >= 0) fadeBootFlag(inst);
      inst.bootAnimationTimer = window.setTimeout(() => finishBootAnimation(inst), 1350);
    }
    return inst.ui.hover;
  }

  /* ------------------------------------------------------------------ daily chart and readout */

  function dailyReadoutMarkup(day, totals, multiYear) {
    const isDay = Boolean(day);
    const source = isDay ? day : totals;
    const title = isDay ? fmtDay(day.date, multiYear) : 'Period total';
    const rate = source.tokens > 0
      ? `<span class="daily-readout__item"><strong>$${escapeHtml((source.cost / source.tokens * 1e6).toFixed(3))}</strong><span class="daily-readout__label">per 1M</span></span>`
      : '';
    return `
      <div class="daily-readout" data-mode="${isDay ? 'day' : 'total'}">
        <span class="daily-readout__title">${escapeHtml(title)}</span>
        <span class="daily-readout__item"><i class="chart-swatch chart-swatch--line chart-swatch--money"></i><strong>${escapeHtml(fmtUsd(source.cost))}</strong><span class="daily-readout__label">spent</span></span>
        <span class="daily-readout__item"><i class="chart-swatch chart-swatch--line chart-swatch--count"></i><strong>${escapeHtml(fmtInt(source.calls))}</strong><span class="daily-readout__label">${source.calls === 1 ? 'call' : 'calls'}</span></span>
        <span class="daily-readout__item"><i class="chart-swatch chart-swatch--square chart-swatch--tokens"></i><strong>${escapeHtml(fmtCompact(source.tokens))}</strong><span class="daily-readout__label">tokens</span></span>
        ${rate}
      </div>`;
  }

  function renderDailyReadout(index) {
    const el = byId('daily-readout');
    if (!el) return;
    const days = focusedDays || dailyState.days;
    const day = index >= 0 ? days[index] : null;
    el.innerHTML = dailyReadoutMarkup(day || null, focusedTotals || dailyState.totals, dailyState.multiYear);
    if (focusedDays && linkedFocus) {
      const label = document.createElement('span');
      label.className = 'linked-focus-label daily-readout__focus';
      label.textContent = linkedFocus.label;
      el.firstElementChild.prepend(label);
    }
  }

  function updateDailyChart(data, ctx) {
    const days = normalizeTimeline(data.timeline);
    const summary = data.summary && typeof data.summary === 'object' ? data.summary : null;
    const sum = (key) => days.reduce((total, day) => total + day[key], 0);
    dailyState.days = days;
    dailyState.multiYear = spansYears(days);
    dailyState.totals = {
      cost: summary && summary.cost_cached_usd != null ? num(summary.cost_cached_usd) : sum('cost'),
      calls: summary && summary.call_count != null ? num(summary.call_count) : sum('calls'),
      tokens: summary && summary.total_tokens != null ? num(summary.total_tokens) : sum('tokens'),
    };

    const canvas = canvasFor('chart-daily');
    let hover = -1;
    if (canvas && typeof Chart !== 'undefined') {
      const empty = !days.some((day) => day.cost > 0 || day.calls > 0 || day.tokens > 0);
      setFrameEmpty(canvas, empty);
      canvas.setAttribute('aria-label', 'Daily spend and API calls lines over token volume bars');
      let inst = discardIfStale('daily', canvas);
      if (!inst) {
        const bootAnimation = Boolean(ctx && ctx.boot && !prefersReducedMotion());
        inst = createOverlayChart(canvas, {
          xTick: (label) => fmtTickDate(label, dailyState.multiYear),
          onHoverChange: (index) => renderDailyReadout(index),
          bootAnimation,
          dataCount: days.length,
        });
        charts.daily = inst;
      }
      hover = updateOverlayChart(
        inst,
        days.map((day) => day.date),
        days.map((day) => day.tokens),
        days.map((day) => day.calls),
        days.map((day) => day.cost),
        {
          label: 'Top day',
          token: 'money',
          values: days.map((day) => day.cost),
          format: fmtUsdShort,
        },
        Boolean(ctx && ctx.boot),
      );
    }
    renderDailyReadout(hover);
  }

  /* ------------------------------------------------------------------ hourly chart */

  function localTimeZoneLabel(zone) {
    try {
      const options = { timeZoneName: 'short' };
      if (zone) options.timeZone = String(zone);
      const parts = new Intl.DateTimeFormat(undefined, options).formatToParts(new Date());
      const tz = parts.find((part) => part.type === 'timeZoneName');
      if (tz && tz.value) return tz.value;
    } catch {
      /* keep the fallback label */
    }
    return 'local time';
  }

  /** Uses the backend's 24 local-hour buckets; missing hours are zeros (never re-bucketed client side). */
  function updateHourlyChart(data, ctx) {
    const titleEl = byId('hourly-activity-title');
    if (titleEl) titleEl.textContent = `Activity by hour (${localTimeZoneLabel(data.timezone)})`;

    const canvas = canvasFor('chart-hourly-activity');
    if (!canvas || typeof Chart === 'undefined') return;

    const labels = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);
    const tokens = new Array(24).fill(0);
    const calls = new Array(24).fill(0);
    const costs = new Array(24).fill(0);
    (Array.isArray(data.hourly_timeline) ? data.hourly_timeline : []).forEach((row) => {
      if (!row || typeof row !== 'object') return;
      const hour = Math.trunc(Number(row.hour));
      if (!Number.isFinite(hour) || hour < 0 || hour > 23) return;
      tokens[hour] = num(row.total_tokens);
      calls[hour] = num(row.call_count);
      costs[hour] = num(row.cost_cached_usd);
      if (row.label) labels[hour] = String(row.label);
    });

    setFrameEmpty(canvas, !tokens.some((v) => v > 0) && !calls.some((v) => v > 0) && !costs.some((v) => v > 0));
    describeCanvas(canvas, 'Hourly spend and API calls lines over token volume bars');
    let inst = discardIfStale('hourly', canvas);
    if (!inst) {
      const bootAnimation = Boolean(ctx && ctx.boot && !prefersReducedMotion());
      inst = createOverlayChart(canvas, {
        xTick: (label) => label,
        xMaxTicks: 8,
        bootAnimation,
        dataCount: labels.length,
        tooltipBuilder: (index, chart) => {
          const rows = chart.$aiudRows;
          const row = rows && rows[index];
          if (!row) return null;
          return {
            title: row.label,
            rows: [
              { swatch: 'money', shape: 'line', label: 'Spent', value: fmtUsd(row.cost) },
              { swatch: 'count', shape: 'line', label: 'API calls', value: fmtInt(row.calls) },
              { swatch: 'tokens', shape: 'square', label: 'Tokens', value: fmtCompact(row.tokens) },
            ],
          };
        },
      });
      charts.hourly = inst;
    }
    inst.chart.$aiudRows = labels.map((label, i) => ({ label, tokens: tokens[i], calls: calls[i], cost: costs[i] }));
    updateOverlayChart(inst, labels, tokens, calls, costs, {
      label: 'Busiest hour',
      token: 'count',
      datasetIndex: 1,
      values: calls,
      format: (value, index) => labels[index],
    }, Boolean(ctx && ctx.boot));
  }

  /* ------------------------------------------------------------------ cache hit rate chart */

  const CACHE_GAP_DAYS = 7;

  /** For every index, the nearest index with data at or before it and at or after it. */
  function gapLookup(values) {
    const prev = new Array(values.length);
    const next = new Array(values.length);
    let last = -Infinity;
    values.forEach((value, index) => {
      if (value !== null) last = index;
      prev[index] = last;
    });
    let following = Infinity;
    for (let index = values.length - 1; index >= 0; index -= 1) {
      if (values[index] !== null) following = index;
      next[index] = following;
    }
    return { prev, next };
  }

  /** True for line segments that only bridge a quiet spell longer than CACHE_GAP_DAYS. */
  function isLongGap(context) {
    const gaps = context.chart.$aiudGaps;
    if (!gaps) return false;
    return gaps.next[context.p1DataIndex] - gaps.prev[context.p0DataIndex] > CACHE_GAP_DAYS;
  }

  /** Indexes of points with no neighbour within CACHE_GAP_DAYS, which the line cannot reach. */
  function isolatedIndexes(values) {
    const isolated = new Set();
    const present = [];
    values.forEach((value, index) => { if (value !== null) present.push(index); });
    present.forEach((index, position) => {
      const before = position > 0 ? present[position - 1] : -Infinity;
      const after = position < present.length - 1 ? present[position + 1] : Infinity;
      if (index - before > CACHE_GAP_DAYS && after - index > CACHE_GAP_DAYS) isolated.add(index);
    });
    return isolated;
  }

  function updateCacheChart(data) {
    const canvas = canvasFor('chart-cache-trend');
    if (!canvas || typeof Chart === 'undefined') return;
    const days = normalizeTimeline(data.timeline);
    const multiYear = spansYears(days);
    setFrameEmpty(canvas, !days.some((day) => day.cacheRate !== null));
    describeCanvas(canvas, 'Daily prompt-cache hit rate');

    let inst = discardIfStale('cache', canvas);
    if (!inst) {
      const ui = {
        hover: -1, pointer: null, peakIndex: -1, peakText: '',
        points: [{ datasetIndex: 0, token: 'tokens' }],
      };
      const chart = new Chart(canvas, {
        type: 'line',
        data: {
          labels: [],
          datasets: [{
            label: 'Cache hit rate',
            data: [],
            borderColor: () => palette.tokens,
            borderWidth: 2,
            borderJoinStyle: 'round',
            fill: false,
            tension: 0,
            // Connect days with usage across short quiet spells; segments over long spells are not drawn.
            spanGaps: true,
            segment: {
              borderColor: (context) => (isLongGap(context) ? 'transparent' : undefined),
            },
            // A day with usage that connects to nothing would otherwise vanish, so it gets a small dot.
            pointRadius: (context) => (
              context.chart.$aiudIsolated && context.chart.$aiudIsolated.has(context.dataIndex) ? 2 : 0
            ),
            pointBackgroundColor: () => palette.tokens,
            pointBorderWidth: 0,
            pointHoverRadius: 0,
            pointHitRadius: 0,
            clip: false,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 6, right: 4, bottom: 0, left: 0 } },
          interaction: { mode: 'index', intersect: false, axis: 'x' },
          plugins: {
            legend: { display: false },
            tooltip: {
              enabled: false,
              mode: 'index',
              intersect: false,
              external: externalTooltip(ui, (index, instChart) => {
                const day = instChart.$aiudDays && instChart.$aiudDays[index];
                if (!day) return null;
                return {
                  title: fmtDay(day.date, instChart.$aiudMultiYear),
                  rows: [{
                    swatch: day.cacheRate === null ? null : 'tokens',
                    shape: 'line',
                    label: 'Cache hit rate',
                    value: day.cacheRate === null ? 'No usage' : fmtPercent(day.cacheRate),
                  }],
                };
              }, true),
            },
          },
          scales: {
            x: {
              type: 'category',
              offset: false,
              border: { display: false },
              grid: { display: false, drawTicks: false },
              ticks: {
                color: tickColor,
                font: tickFont,
                padding: 8,
                minRotation: 0,
                maxRotation: 0,
                autoSkip: true,
                autoSkipPadding: 14,
                callback(value) {
                  return fmtTickDate(this.getLabelForValue(value), inst && inst.multiYear);
                },
              },
            },
            y: {
              type: 'linear',
              min: 0,
              max: 100,
              border: { display: false },
              grid: { color: gridColor, lineWidth: 1, drawTicks: false },
              ticks: {
                color: tickColor,
                font: tickFont,
                padding: 8,
                stepSize: 50,
                callback: (value) => `${value}%`,
              },
            },
          },
        },
        plugins: [pointerPlugin(ui), interactionPlugin(ui)],
      });
      inst = { chart, canvas, ui, rendered: false, multiYear: false };
      charts.cache = inst;
    }
    inst.multiYear = multiYear;
    inst.chart.$aiudDays = days;
    inst.chart.$aiudMultiYear = multiYear;
    inst.chart.data.labels = days.map((day) => day.date);
    const rates = days.map((day) => (day.cacheRate === null ? null : day.cacheRate));
    inst.chart.$aiudIsolated = isolatedIndexes(rates);
    inst.chart.$aiudGaps = gapLookup(rates);
    inst.chart.data.datasets[0].data = rates;
    if (inst.ui.hover >= days.length) inst.ui.hover = -1;
    commit(inst);
  }

  /* ------------------------------------------------------------------ donut: cost by tool */

  function aggregateCostByTool(data) {
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const models = !sessions.length && Array.isArray(data.models) ? data.models : [];
    const items = sessions.length ? sessions : models;
    const totals = { codex: 0, claude: 0, agy: 0, other: 0 };
    const tools = { codex: 'codex', claude: 'claude-code', agy: 'antigravity', other: 'other' };
    items.forEach((item) => {
      if (!item || typeof item !== 'object') return;
      const key = toolKey(item);
      totals[key] += num(item.cost_cached_usd ?? item.est_cost_cached_usd);
      if (item.tool && item.tool !== 'all') tools[key] = item.tool;
    });
    return Object.keys(totals)
      .map((key) => ({ key, tool: tools[key], name: TOOL_NAMES[key], cost: Math.round(totals[key] * 10000) / 10000 }))
      .filter((row) => row.cost > 0)
      .sort((a, b) => b.cost - a.cost);
  }

  function fmtShare(value) {
    if (value > 0 && value < 0.1) return '<0.1%';
    return `${value.toFixed(1)}%`;
  }

  function renderDonutText(rows, total) {
    const center = byId('tool-donut-center');
    const legend = byId('tool-donut-legend');
    if (center) {
      center.innerHTML = rows.length
        ? `<span class="donut-center__value">${escapeHtml(fmtUsd(total))}</span><span class="donut-center__label">${rows.length} ${plural(rows.length, 'tool', 'tools')}</span>`
        : '';
    }
    if (legend) {
      legend.innerHTML = rows.map((row) => {
        const share = total > 0 ? (row.cost / total) * 100 : 0;
        return `<li class="donut-legend__row" data-tool="${escapeHtml(row.tool)}" tabindex="0"><span class="chart-dot chart-dot--${row.key}" aria-hidden="true"></span><span class="donut-legend__name">${escapeHtml(row.name)}</span><span class="donut-legend__cost">${escapeHtml(fmtUsd(row.cost))}</span><span class="donut-legend__share">${escapeHtml(fmtShare(share))}</span></li>`;
      }).join('');
    }
  }

  function updateDonutChart(data) {
    const rows = aggregateCostByTool(data);
    const total = rows.reduce((sum, row) => sum + row.cost, 0);
    renderDonutText(rows, total);

    const canvas = canvasFor('chart-cost-by-tool');
    if (!canvas || typeof Chart === 'undefined') return;
    setFrameEmpty(canvas, rows.length === 0);
    describeCanvas(canvas, 'Estimated cost split by tool');

    let inst = discardIfStale('donut', canvas);
    if (!inst) {
      const ui = { pointer: null };
      const chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
          labels: [],
          datasets: [{
            data: [],
            backgroundColor: (context) => {
              const row = inst && inst.rows[context.dataIndex];
              const color = palette[`tool-${row ? row.key : 'other'}`];
              return linkedFocus && row && row.key !== toolKey(linkedFocus) ? withAlpha(color, 0.35) : color;
            },
            hoverBackgroundColor: (context) => {
              const row = inst && inst.rows[context.dataIndex];
              const color = palette[`tool-${row ? row.key : 'other'}`];
              return linkedFocus && row && row.key !== toolKey(linkedFocus) ? withAlpha(color, 0.35) : color;
            },
            borderColor: () => palette.surface,
            hoverBorderColor: () => palette.surface,
            borderWidth: (context) => (context.chart.data.datasets[0].data.length > 1 ? 2 : 0),
            hoverBorderWidth: (context) => (context.chart.data.datasets[0].data.length > 1 ? 2 : 0),
            hoverOffset: 0,
            offset: (context) => {
              const row = inst && inst.rows[context.dataIndex];
              return linkedFocus && row && row.key === toolKey(linkedFocus) ? 10 : 0;
            },
            borderRadius: 0,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: '72%',
          layout: { padding: 2 },
          plugins: {
            legend: { display: false },
            tooltip: {
              enabled: false,
              external: externalTooltip(ui, (index) => {
                const row = inst && inst.rows[index];
                if (!row) return null;
                const share = inst.total > 0 ? (row.cost / inst.total) * 100 : 0;
                return {
                  title: row.name,
                  rows: [
                    { swatch: row.key, shape: 'dot', label: 'Spent', value: fmtUsd(row.cost) },
                    { swatch: null, label: 'Share', value: fmtShare(share) },
                  ],
                };
              }, false),
            },
          },
        },
        plugins: [pointerPlugin(ui)],
      });
      inst = { chart, canvas, ui, rendered: false, rows: [], total: 0 };
      charts.donut = inst;
      bindDonutFocus(inst);
    }
    inst.rows = rows;
    inst.total = total;
    inst.chart.data.labels = rows.map((row) => row.name);
    inst.chart.data.datasets[0].data = rows.map((row) => row.cost);
    commit(inst);
  }

  function bindDonutFocus(inst) {
    const notify = (interaction, tool) => inst.canvas.dispatchEvent(new CustomEvent('dashboard:tool-focus', {
      bubbles: true, detail: { interaction, tool },
    }));
    const toolAt = (event) => {
      const hit = inst.chart.getElementsAtEventForMode(event, 'nearest', { intersect: true }, false)[0];
      return hit && inst.rows[hit.index] ? inst.rows[hit.index].tool : null;
    };
    let hoveredTool = null;
    const move = (event) => {
      if (event.pointerType === 'touch') return;
      const tool = toolAt(event);
      if (tool === hoveredTool) return;
      hoveredTool = tool;
      notify(tool ? 'enter' : 'leave', tool);
    };
    const leave = () => {
      hoveredTool = null;
      notify('leave', null);
    };
    const tap = (event) => {
      if (event.pointerType === 'touch') notify('tap', toolAt(event));
    };
    inst.canvas.addEventListener('pointermove', move);
    inst.canvas.addEventListener('pointerleave', leave);
    inst.canvas.addEventListener('pointerup', tap);
    inst.unbindFocus = () => {
      inst.canvas.removeEventListener('pointermove', move);
      inst.canvas.removeEventListener('pointerleave', leave);
      inst.canvas.removeEventListener('pointerup', tap);
    };
  }

  /* ------------------------------------------------------------------ heatmap */

  let heatmapDetailCard = null;
  let heatmapDetailPinnedCell = null;
  let heatmapDetailAnchorCell = null;
  let heatmapHoveredCell = null;
  let heatmapFocusedCell = null;
  let heatmapSuppressedCell = null;
  let heatmapPreviewMode = 'pointer';
  let heatmapInteractionsBound = false;
  const heatmapCellData = new WeakMap();
  const WEEKDAYS = [
    ['M', 'Monday'], ['T', 'Tuesday'], ['W', 'Wednesday'], ['T', 'Thursday'],
    ['F', 'Friday'], ['S', 'Saturday'], ['S', 'Sunday'],
  ];

  function weekdayColumn(dateValue) {
    const date = parseDay(dateValue);
    return date ? (date.getUTCDay() + 6) % 7 : 0;
  }

  function heatmapValue(cell, metric) {
    if (metric === 'tokens') return num(cell.total_tokens);
    if (metric === 'calls') return num(cell.call_count);
    return num(cell.cost_cached_usd);
  }

  /** Quantile bucketing of the non-zero values into steps 1-6 (ties share a step). */
  function heatmapSteps(list, metric) {
    const values = list.map((cell) => heatmapValue(cell, metric));
    const sorted = values.filter((v) => v > 0).sort((a, b) => a - b);
    const count = sorted.length;
    return values.map((value) => {
      if (value <= 0 || count === 0) return 0;
      let first = 0;
      let last = count - 1;
      while (first < count && sorted[first] < value) first += 1;
      while (last >= 0 && sorted[last] > value) last -= 1;
      const position = ((first + last) / 2 + 0.5) / count;
      return clamp(Math.floor(position * 6) + 1, 1, 6);
    });
  }

  function heatmapMetricSummary(cell) {
    return `${fmtUsd(cell.cost_cached_usd)} spent, ${fmtInt(cell.call_count)} calls, ${fmtCompact(cell.total_tokens)} tokens`;
  }

  function ensureHeatmapDetailCard() {
    if (heatmapDetailCard && heatmapDetailCard.isConnected) return heatmapDetailCard;
    heatmapDetailCard = document.createElement('div');
    heatmapDetailCard.id = 'heatmap-detail-card';
    heatmapDetailCard.className = 'heatmap-detail-card';
    heatmapDetailCard.hidden = true;
    heatmapDetailCard.addEventListener('click', (event) => {
      if (!event.target.closest('.heatmap-detail-close')) return;
      dismissHeatmapDetail({ restoreFocus: true });
    });
    document.body.appendChild(heatmapDetailCard);
    return heatmapDetailCard;
  }

  function heatmapDetailMarkup(cell, pinned) {
    const breakdown = [
      ['Cache reads', cell.cached_input],
      ['Cache writes', cell.cache_write],
      ['Fresh input', cell.uncached_input],
      ['Output', cell.output],
    ].map(([label, value]) => (
      `<div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--none"></i><span class="chart-tooltip__label">${label}</span><span class="chart-tooltip__value">${escapeHtml(fmtInt(value))}</span></div>`
    )).join('');
    const close = pinned
      ? '<button type="button" class="heatmap-detail-close" aria-label="Close daily usage details"><svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"></path></svg></button>'
      : '';
    return `
      <div class="heatmap-detail-header">
        <div id="heatmap-detail-title" class="chart-tooltip__title">${linkedFocus && linkedFocus.heatmap ? `<div class="linked-focus-label">${escapeHtml(linkedFocus.label)}</div>` : ''}${escapeHtml(fmtDay(cell.date, true))}</div>
        ${close}
      </div>
      <div id="heatmap-detail-totals">
        <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--square chart-swatch--money"></i><span class="chart-tooltip__label">Spent</span><span class="chart-tooltip__value">${escapeHtml(fmtUsd(cell.cost_cached_usd))}</span></div>
        <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--square chart-swatch--tokens"></i><span class="chart-tooltip__label">Tokens</span><span class="chart-tooltip__value">${escapeHtml(fmtInt(cell.total_tokens))}</span></div>
        <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--line chart-swatch--count"></i><span class="chart-tooltip__label">API calls</span><span class="chart-tooltip__value">${escapeHtml(fmtInt(cell.call_count))}</span></div>
      </div>
      <div class="heatmap-detail-breakdown" id="heatmap-detail-breakdown" aria-label="Token breakdown"${linkedFocus && linkedFocus.heatmap ? ' hidden' : ''}>${breakdown}</div>`;
  }

  function positionHeatmapDetail(anchor) {
    if (!heatmapDetailCard || heatmapDetailCard.hidden || !anchor || !anchor.isConnected) return;
    const margin = 12;
    const anchorRect = anchor.getBoundingClientRect();
    const cardRect = heatmapDetailCard.getBoundingClientRect();
    const cardWidth = cardRect.width;
    const cardHeight = cardRect.height;
    const left = Math.max(
      margin,
      Math.min(anchorRect.left + anchorRect.width / 2 - cardWidth / 2, window.innerWidth - cardWidth - margin),
    );
    const above = anchorRect.top - cardHeight - 8;
    const below = anchorRect.bottom + 8;
    const spaceAbove = anchorRect.top - margin;
    const spaceBelow = window.innerHeight - anchorRect.bottom - margin;
    let top = spaceBelow >= cardHeight || spaceBelow >= spaceAbove ? below : above;
    top = Math.max(margin, Math.min(top, window.innerHeight - cardHeight - margin));
    heatmapDetailCard.style.left = `${left}px`;
    heatmapDetailCard.style.top = `${top}px`;
  }

  function showHeatmapDetail(cellElement, pinned = false) {
    const cell = heatmapCellData.get(cellElement);
    if (!cell) return;
    const card = ensureHeatmapDetailCard();
    const restoreCloseFocus = pinned && document.activeElement === card.querySelector('.heatmap-detail-close');
    hideTooltip();
    if (heatmapDetailAnchorCell && heatmapDetailAnchorCell !== cellElement) {
      heatmapDetailAnchorCell.removeAttribute('aria-describedby');
      heatmapDetailAnchorCell.setAttribute('aria-expanded', 'false');
    }
    heatmapDetailAnchorCell = cellElement;
    cellElement.setAttribute('aria-describedby', card.id);
    cellElement.setAttribute('aria-expanded', pinned ? 'true' : 'false');
    card.innerHTML = heatmapDetailMarkup(cell, pinned);
    card.setAttribute('role', pinned ? 'dialog' : 'tooltip');
    card.setAttribute('aria-label', `Daily usage details for ${fmtDay(cell.date, true)}`);
    card.dataset.pinned = pinned ? 'true' : 'false';
    if (pinned) {
      card.setAttribute('aria-labelledby', 'heatmap-detail-title');
      card.setAttribute('aria-describedby', 'heatmap-detail-totals heatmap-detail-breakdown');
    } else {
      card.removeAttribute('aria-labelledby');
      card.removeAttribute('aria-describedby');
    }
    card.hidden = false;
    positionHeatmapDetail(cellElement);
    if (restoreCloseFocus) card.querySelector('.heatmap-detail-close').focus({ preventScroll: true });
  }

  function hideHeatmapDetail({ suppress = false, clearPreviews = false, restoreFocus = false } = {}) {
    const activeCell = heatmapDetailPinnedCell || heatmapDetailAnchorCell;
    if (suppress) {
      heatmapSuppressedCell = activeCell || heatmapHoveredCell || heatmapFocusedCell;
    }
    if (clearPreviews) {
      heatmapHoveredCell = null;
      heatmapFocusedCell = null;
    }
    if (activeCell) {
      activeCell.removeAttribute('aria-describedby');
      activeCell.setAttribute('aria-expanded', 'false');
    }
    heatmapDetailPinnedCell = null;
    heatmapDetailAnchorCell = null;
    if (heatmapDetailCard) {
      heatmapDetailCard.hidden = true;
      heatmapDetailCard.dataset.pinned = 'false';
      heatmapDetailCard.removeAttribute('role');
      heatmapDetailCard.removeAttribute('aria-labelledby');
      heatmapDetailCard.removeAttribute('aria-describedby');
    }
    if (restoreFocus && activeCell && activeCell.isConnected) activeCell.focus();
  }

  function dismissHeatmapDetail({ restoreFocus = false } = {}) {
    hideHeatmapDetail({ suppress: true, clearPreviews: true, restoreFocus });
  }

  function refreshHeatmapPreview() {
    if (heatmapDetailPinnedCell) return;
    let candidate = heatmapPreviewMode === 'focus' ? heatmapFocusedCell : heatmapHoveredCell;
    if (!candidate || candidate === heatmapSuppressedCell) {
      const fallback = heatmapPreviewMode === 'focus' ? heatmapHoveredCell : heatmapFocusedCell;
      candidate = fallback === heatmapSuppressedCell ? null : fallback;
    }
    if (!candidate) {
      hideHeatmapDetail();
      return;
    }
    heatmapSuppressedCell = null;
    showHeatmapDetail(candidate, false);
  }

  function toggleHeatmapPin(cellElement, focusCloseButton = false) {
    if (heatmapDetailPinnedCell === cellElement) {
      dismissHeatmapDetail({
        restoreFocus: Boolean(heatmapDetailCard && heatmapDetailCard.contains(document.activeElement)),
      });
      return;
    }
    heatmapSuppressedCell = null;
    heatmapDetailPinnedCell = cellElement;
    showHeatmapDetail(cellElement, true);
    if (focusCloseButton) {
      const button = heatmapDetailCard.querySelector('.heatmap-detail-close');
      if (button) button.focus();
    }
  }

  function attachHeatmapInteractions(container) {
    if (heatmapInteractionsBound) return;
    heatmapInteractionsBound = true;
    ensureHeatmapDetailCard();

    container.addEventListener('pointerover', (event) => {
      const cell = event.target.closest('.heatmap-cell');
      if (!cell || !container.contains(cell) || (event.relatedTarget && cell.contains(event.relatedTarget))) return;
      heatmapHoveredCell = cell;
      heatmapPreviewMode = 'pointer';
      if (heatmapSuppressedCell !== cell) heatmapSuppressedCell = null;
      refreshHeatmapPreview();
    });
    container.addEventListener('pointerout', (event) => {
      const cell = event.target.closest('.heatmap-cell');
      if (!cell || !container.contains(cell) || (event.relatedTarget && cell.contains(event.relatedTarget))) return;
      if (heatmapHoveredCell === cell) heatmapHoveredCell = null;
      if (heatmapSuppressedCell === cell && heatmapFocusedCell !== cell) heatmapSuppressedCell = null;
      refreshHeatmapPreview();
    });
    container.addEventListener('focusin', (event) => {
      const cell = event.target.closest('.heatmap-cell');
      if (!cell || !container.contains(cell)) return;
      heatmapFocusedCell = cell;
      heatmapPreviewMode = 'focus';
      if (heatmapSuppressedCell !== cell) heatmapSuppressedCell = null;
      refreshHeatmapPreview();
    });
    container.addEventListener('focusout', (event) => {
      const cell = event.target.closest('.heatmap-cell');
      if (!cell || !container.contains(cell)) return;
      if (heatmapFocusedCell === cell) heatmapFocusedCell = null;
      if (heatmapSuppressedCell === cell && heatmapHoveredCell !== cell) heatmapSuppressedCell = null;
      refreshHeatmapPreview();
    });
    container.addEventListener('click', (event) => {
      const cell = event.target.closest('.heatmap-cell');
      if (cell && container.contains(cell)) toggleHeatmapPin(cell);
    });
    container.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      const cell = event.target.closest('.heatmap-cell');
      if (!cell || !container.contains(cell)) return;
      event.preventDefault();
      toggleHeatmapPin(cell, true);
    });

    document.addEventListener('pointerdown', (event) => {
      if (!heatmapDetailPinnedCell && !heatmapDetailAnchorCell) return;
      if (heatmapDetailCard && heatmapDetailCard.contains(event.target)) return;
      const cell = event.target.closest && event.target.closest('.heatmap-cell');
      if (cell && container.contains(cell)) return;
      dismissHeatmapDetail({
        restoreFocus: Boolean(heatmapDetailCard && heatmapDetailCard.contains(document.activeElement)),
      });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !heatmapDetailAnchorCell) return;
      const focusCloseButton = Boolean(heatmapDetailCard && heatmapDetailCard.contains(document.activeElement));
      event.preventDefault();
      dismissHeatmapDetail({ restoreFocus: focusCloseButton });
    });
    window.addEventListener('resize', () => positionHeatmapDetail(heatmapDetailAnchorCell));
    window.addEventListener('scroll', () => positionHeatmapDetail(heatmapDetailAnchorCell), true);
  }

  function renderHeatmapLegend(metric) {
    const legend = byId('heatmap-legend');
    if (!legend) return;
    legend.classList.add('hm');
    legend.dataset.metric = metric;
    legend.innerHTML = `<span>Less</span><span class="heatmap-legend__swatches" aria-hidden="true">${
      [1, 2, 3, 4, 5, 6].map((step) => `<span class="heatmap-swatch" data-step="${step}"></span>`).join('')
    }</span><span>More</span>`;
  }

  function clearHeatmap(container, legend) {
    hideHeatmapDetail();
    heatmapState.structureKey = '';
    container.innerHTML = `<div class="chart-empty chart-empty--static">${EMPTY_TEXT}</div>`;
    if (legend) legend.innerHTML = '';
  }

  function updateHeatmapStreak() {
    const el = byId('heatmap-streak');
    if (!el) return;
    el.title = 'Days in a row with at least one call';
    const cells = heatmapState.cells;
    let index = cells.length - 1;
    if (index >= 0 && num(cells[index].call_count) <= 0) index -= 1;
    let streak = 0;
    while (index >= 0 && num(cells[index].call_count) > 0) {
      streak += 1;
      index -= 1;
    }
    if (streak >= 2) {
      el.textContent = streak === 30 ? 'Active all 30 days' : `${streak}-day streak`;
      el.hidden = false;
    } else {
      el.textContent = '';
      el.hidden = true;
    }
  }

  /** Render (or patch in place) the API's local calendar dates as a seven-column calendar. */
  function renderHeatmap() {
    const container = byId('daily-usage-heatmap');
    if (!container) return;
    updateHeatmapStreak();
    const legend = byId('heatmap-legend');
    const metric = heatmapState.metric;
    const list = heatmapState.cells;
    container.classList.add('hm');
    container.dataset.metric = metric;

    const hasUsage = list.some((cell) => (
      heatmapValue(cell, 'cost') > 0 || heatmapValue(cell, 'tokens') > 0 || heatmapValue(cell, 'calls') > 0
    ));
    if (!list.length || (!hasUsage && !(linkedFocus && linkedFocus.heatmap))) {
      clearHeatmap(container, legend);
      return;
    }

    const steps = heatmapSteps(list, metric);
    const today = todayKey(heatmapState.timezone);
    const structureKey = `${list.map((cell) => cell.date).join(',')}|${today}`;
    const cellsByDate = new Map(list.map((cell, index) => [cell.date, { cell, step: steps[index] }]));
    renderHeatmapLegend(metric);

    if (heatmapState.structureKey === structureKey && container.querySelector('.heatmap-calendar')) {
      container.querySelectorAll('.heatmap-cell').forEach((element) => {
        const entry = cellsByDate.get(element.dataset.date);
        if (!entry) return;
        heatmapCellData.set(element, entry.cell);
        element.dataset.step = String(entry.step);
        element.setAttribute('aria-label', heatmapCellLabel(entry.cell));
      });
      if (heatmapDetailAnchorCell && heatmapDetailAnchorCell.isConnected && heatmapDetailCard && !heatmapDetailCard.hidden) {
        showHeatmapDetail(heatmapDetailAnchorCell, Boolean(heatmapDetailPinnedCell));
      }
      return;
    }

    const activeElement = document.activeElement;
    const restoreCloseFocus = heatmapDetailCard && activeElement === heatmapDetailCard.querySelector('.heatmap-detail-close');
    const focusedGridCell = container.contains(activeElement) ? activeElement.closest('.heatmap-cell') : null;
    const restoreFocusDate = (focusedGridCell && focusedGridCell.dataset.date)
      || (heatmapDetailCard && heatmapDetailCard.contains(activeElement) && heatmapDetailAnchorCell
        ? heatmapDetailAnchorCell.dataset.date : null);
    const pinnedDate = heatmapDetailPinnedCell ? heatmapDetailPinnedCell.dataset.date : null;
    hideHeatmapDetail();
    heatmapHoveredCell = null;
    heatmapFocusedCell = null;
    heatmapSuppressedCell = null;

    const firstOffset = weekdayColumn(list[0].date);
    const header = `<div class="heatmap-row" role="row">${WEEKDAYS.map(([letter, name], index) => (
      `<div class="heatmap-weekday" role="columnheader" aria-colindex="${index + 1}" aria-label="${name}">${letter}</div>`
    )).join('')}</div>`;
    const spacer = '<div class="heatmap-spacer" role="presentation" aria-hidden="true"></div>';
    const rows = [];
    let rowCells = [];
    for (let i = 0; i < firstOffset; i += 1) rowCells.push(spacer);
    list.forEach((cell, index) => {
      const column = ((firstOffset + index) % 7) + 1;
      const isToday = cell.date === today ? ' data-today="true"' : '';
      rowCells.push(`<div class="heatmap-cell" role="gridcell" aria-colindex="${column}" aria-haspopup="dialog" aria-controls="heatmap-detail-card" aria-expanded="false" tabindex="0" aria-label="${escapeHtml(heatmapCellLabel(cell))}" data-date="${escapeHtml(cell.date)}" data-step="${steps[index]}"${isToday}><span aria-hidden="true">${Number(cell.date.slice(-2))}</span></div>`);
      if (rowCells.length === 7) {
        rows.push(`<div class="heatmap-row" role="row">${rowCells.join('')}</div>`);
        rowCells = [];
      }
    });
    if (rowCells.length) {
      while (rowCells.length < 7) rowCells.push(spacer);
      rows.push(`<div class="heatmap-row" role="row">${rowCells.join('')}</div>`);
    }

    const labelled = byId('heatmap-title')
      ? 'aria-labelledby="heatmap-title"'
      : 'aria-label="Daily usage calendar"';
    container.innerHTML = `<div class="heatmap-calendar" role="grid" ${labelled} aria-rowcount="${rows.length + 1}" aria-colcount="7">${header}${rows.join('')}</div>`;
    container.querySelectorAll('.heatmap-cell').forEach((element) => {
      const entry = cellsByDate.get(element.dataset.date);
      if (entry) heatmapCellData.set(element, entry.cell);
    });
    heatmapState.structureKey = structureKey;
    attachHeatmapInteractions(container);

    const find = (date) => Array.from(container.querySelectorAll('.heatmap-cell')).find((el) => el.dataset.date === date);
    if (pinnedDate) {
      const replacement = find(pinnedDate);
      if (replacement) {
        heatmapDetailPinnedCell = replacement;
        showHeatmapDetail(replacement, true);
        if (restoreCloseFocus) heatmapDetailCard.querySelector('.heatmap-detail-close').focus({ preventScroll: true });
      }
    }
    if (restoreFocusDate && !(restoreCloseFocus && heatmapDetailPinnedCell)) {
      const replacement = find(restoreFocusDate);
      if (replacement) replacement.focus();
    }
  }

  function heatmapCellLabel(cell) {
    const label = linkedFocus && linkedFocus.heatmap ? `${linkedFocus.label}. ` : '';
    return `${label}${fmtDay(cell.date, true)}: ${heatmapMetricSummary(cell)}. Press Enter or Space to pin details.`;
  }

  function updateDailyHeatmap(data) {
    const cells = (Array.isArray(data.heatmap_daily) ? data.heatmap_daily : [])
      .filter((cell) => cell && typeof cell === 'object' && /^\d{4}-\d{2}-\d{2}$/.test(String(cell.date || '')))
      .sort((a, b) => String(a.date).localeCompare(String(b.date)))
      .slice(-HEATMAP_DAYS);
    totalHeatmapCells = cells;
    heatmapState.cells = cells;
    heatmapState.timezone = data.timezone || '';
    renderHeatmap();
  }

  function loadHeatmapMetric() {
    try {
      const stored = window.localStorage.getItem(METRIC_STORAGE_KEY);
      if (HEATMAP_METRICS.includes(stored)) return stored;
    } catch {
      /* storage can be blocked */
    }
    return 'cost';
  }

  function saveHeatmapMetric(metric) {
    try {
      window.localStorage.setItem(METRIC_STORAGE_KEY, metric);
    } catch {
      /* storage can be blocked */
    }
  }

  function syncMetricControl(group) {
    Array.from(group.querySelectorAll('[data-metric]')).forEach((button) => {
      const selected = button.dataset.metric === heatmapState.metric;
      button.setAttribute('aria-checked', selected ? 'true' : 'false');
      button.tabIndex = selected ? 0 : -1;
    });
  }

  function setHeatmapMetric(metric, { persist = true } = {}) {
    if (!HEATMAP_METRICS.includes(metric)) return;
    heatmapState.metric = metric;
    if (persist) saveHeatmapMetric(metric);
    const group = byId('heatmap-metric');
    if (group) syncMetricControl(group);
    guard('renderHeatmap', renderHeatmap);
  }

  function bindHeatmapMetric() {
    const group = byId('heatmap-metric');
    if (!group || group.dataset.chartsBound === 'true') return;
    group.dataset.chartsBound = 'true';
    syncMetricControl(group);
    group.addEventListener('click', (event) => {
      const button = event.target.closest('[data-metric]');
      if (button && group.contains(button)) setHeatmapMetric(button.dataset.metric);
    });
    group.addEventListener('keydown', (event) => {
      const buttons = Array.from(group.querySelectorAll('[data-metric]'));
      const current = buttons.indexOf(event.target.closest('[data-metric]'));
      if (current < 0) return;
      let next = -1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % buttons.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + buttons.length) % buttons.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = buttons.length - 1;
      if (next < 0) return;
      event.preventDefault();
      buttons[next].focus();
      setHeatmapMetric(buttons[next].dataset.metric);
    });
  }

  /* ------------------------------------------------------------------ sparklines */

  const SPARK_WIDTH = 96;
  const SPARK_HEIGHT = 28;

  function renderSparkline(mount, values, token) {
    if (!mount) return;
    const series = (Array.isArray(values) ? values : []).slice(-SPARK_POINTS).map(num);
    const max = series.reduce((m, v) => Math.max(m, v), 0);
    if (series.length === 0 || max <= 0) {
      mount.innerHTML = '';
      return;
    }
    const padX = 3;
    const padTop = 3.5;
    const padBottom = 3.5;
    const plotHeight = SPARK_HEIGHT - padTop - padBottom;
    const step = series.length > 1 ? (SPARK_WIDTH - padX * 2) / (series.length - 1) : 0;
    const points = series.map((value, index) => [
      series.length > 1 ? padX + index * step : SPARK_WIDTH - padX,
      padTop + plotHeight - (value / max) * plotHeight,
    ]);
    const line = points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
    const first = points[0];
    const last = points[points.length - 1];
    const area = `M${first[0].toFixed(2)},${SPARK_HEIGHT} L${line.replace(/ /g, ' L')} L${last[0].toFixed(2)},${SPARK_HEIGHT} Z`;
    mount.innerHTML = `<svg class="spark-svg spark-svg--${token}" width="${SPARK_WIDTH}" height="${SPARK_HEIGHT}" viewBox="0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}" aria-hidden="true" focusable="false"><path d="${area}" fill="currentColor" fill-opacity="0.1" stroke="none"></path><polyline points="${line}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></polyline><circle cx="${last[0].toFixed(2)}" cy="${last[1].toFixed(2)}" r="2.5" fill="currentColor"></circle></svg>`;
  }

  function updateSparklines(data) {
    const days = normalizeTimeline(data.timeline);
    renderSparkline(byId('sparkline-burn'), days.map((day) => day.cost), 'money');
    renderSparkline(byId('sparkline-tokens'), days.map((day) => day.tokens), 'tokens');
    renderSparkline(byId('sparkline-calls'), days.map((day) => day.calls), 'count');
  }

  /* ------------------------------------------------------------------ lifecycle and public API */

  function updateFocusChart(inst, animate) {
    // The boot recorder has delayed x/y animations. A user focus ends that draw-in,
    // then uses a dedicated short transition with no delays or reset positions.
    inst.chart.stop();
    if (inst.bootAnimating) {
      cancelBootFlagFade(inst);
      window.clearTimeout(inst.bootAnimationTimer);
      inst.bootAnimationTimer = 0;
      inst.bootAnimating = false;
      inst.ui.peakOpacity = 1;
    }
    inst.chart.config.options.animation = prefersReducedMotion() ? false : { duration: 160, easing: 'easeOutCubic' };
    if (inst.bootAnimationConfig) inst.chart.config.options.datasets = {};
    inst.chart.config.options.transitions = {
      ...inst.chart.config.options.transitions,
      linked: { animation: { duration: 160, easing: 'easeOutCubic', delay: 0 } },
    };
    inst.chart.update(animate && !prefersReducedMotion() ? 'linked' : 'none');
  }

  function focusDailyChart(inst, animate) {
    const chart = inst.chart;
    const datasets = chart.data.datasets;
    if (!inst.totalStyles) inst.totalStyles = datasets.slice(0, 3).map((dataset) => ({ ...dataset }));
    datasets.slice(0, 3).forEach((dataset, index) => {
      Object.assign(dataset, inst.totalStyles[index], { data: dataset.data });
      ['grouped', 'borderDash'].forEach((property) => {
        if (!(property in inst.totalStyles[index])) delete dataset[property];
      });
    });
    if (focusedDays) {
      datasets[0].grouped = false;
      datasets[0].backgroundColor = () => withAlpha(palette.tokens, 0.25);
      datasets[0].hoverBackgroundColor = () => withAlpha(palette.tokens, 0.25);
      [1, 2].forEach((index) => {
        datasets[index].borderColor = () => withAlpha(palette[index === 1 ? 'count' : 'money'], 0.3);
        datasets[index].borderDash = [3, 3];
      });
      const layers = [
        { ...inst.totalStyles[0], label: 'Focused tokens', data: focusedDays.map((day) => day.tokens), grouped: false, order: 0 },
        { ...overlayLine('Focused API calls', 'yCalls', 'count', 2, -1), data: focusedDays.map((day) => day.calls) },
        { ...overlayLine('Focused spent', 'yCost', 'money', 2, -2), data: focusedDays.map((day) => day.cost) },
      ];
      layers.forEach((layer, index) => {
        if (datasets[index + 3]) Object.assign(datasets[index + 3], layer);
        else datasets.push(layer);
      });
    } else {
      datasets.splice(3);
    }
    inst.ui.hidePeak = Boolean(focusedDays);
    inst.ui.points = focusedDays
      ? [{ datasetIndex: 4, token: 'count' }, { datasetIndex: 5, token: 'money' }]
      : [{ datasetIndex: 1, token: 'count' }, { datasetIndex: 2, token: 'money' }];
    // No scale updates: both bar layers use the same category centers and total bounds.
    updateFocusChart(inst, animate);
  }

  function setFocus(focus, { animate = true } = {}) {
    const wasDailyFocus = Boolean(focusedDays);
    const wasHeatmapFocus = Boolean(linkedFocus && linkedFocus.heatmap);
    linkedFocus = focus || null;
    focusedDays = null;
    focusedTotals = null;
    if (linkedFocus && Array.isArray(linkedFocus.timeline)) {
      const byDate = new Map(normalizeTimeline(linkedFocus.timeline).map((day) => [day.date, day]));
      focusedDays = dailyState.days.map((day) => byDate.get(day.date)
        || { date: day.date, cost: 0, calls: 0, tokens: 0 });
      focusedTotals = focusedDays.reduce((total, day) => ({
        cost: total.cost + day.cost, calls: total.calls + day.calls, tokens: total.tokens + day.tokens,
      }), { cost: 0, calls: 0, tokens: 0 });
    }
    if (charts.daily && (focusedDays || wasDailyFocus)) focusDailyChart(charts.daily, animate);
    renderDailyReadout(charts.daily ? charts.daily.ui.hover : -1);

    if (linkedFocus && Array.isArray(linkedFocus.heatmap)) {
      const byDate = new Map(linkedFocus.heatmap.map((cell) => [cell.date, cell]));
      heatmapState.cells = totalHeatmapCells.map((cell) => byDate.get(cell.date)
        || { date: cell.date, total_tokens: 0, call_count: 0, cost_cached_usd: 0 });
    } else {
      heatmapState.cells = totalHeatmapCells;
    }
    if (wasHeatmapFocus || (linkedFocus && linkedFocus.heatmap)) renderHeatmap();

    const donut = charts.donut;
    if (donut) {
      const center = byId('tool-donut-center');
      if (center) {
        const cost = linkedFocus ? linkedFocus.type === 'model' ? linkedFocus.cost
          : (donut.rows.find((row) => row.key === toolKey(linkedFocus)) || {}).cost : donut.total;
        const label = linkedFocus ? linkedFocus.label : `${donut.rows.length} ${plural(donut.rows.length, 'tool', 'tools')}`;
        center.innerHTML = donut.rows.length || linkedFocus
          ? `<span class="donut-center__value">${escapeHtml(fmtUsd(cost))}</span><span class="donut-center__label">${escapeHtml(label)}</span>` : '';
      }
      updateFocusChart(donut, animate);
    }
  }

  function init() {
    if (initialized) return;
    initialized = true;
    readPalette();
    applyChartDefaults();
    heatmapState.metric = loadHeatmapMetric();
    bindHeatmapMetric();
    document.addEventListener('themechange', refreshTheme);
    try {
      const scheme = window.matchMedia('(prefers-color-scheme: dark)');
      if (scheme.addEventListener) scheme.addEventListener('change', refreshTheme);
      else if (scheme.addListener) scheme.addListener(refreshTheme);
    } catch {
      /* matchMedia unavailable */
    }
    window.addEventListener('scroll', () => hideTooltip(), { passive: true });
    if (document.fonts && document.fonts.ready) {
      // Canvas text needs the web font before the first measured draw; redraw once it is ready.
      document.fonts.ready.then(() => {
        Object.keys(charts).forEach((key) => {
          const inst = charts[key];
          if (!inst || !inst.chart) return;
          if (inst.bootAnimating) inst.fontRefreshPending = true;
          else guard('fonts', () => inst.chart.update('none'));
        });
      });
    }
  }

  function updateCharts(data, ctx) {
    init();
    if (!paletteReady) readPalette();
    const payload = data && typeof data === 'object' ? data : {};
    guard('updateDailyChart', () => updateDailyChart(payload, ctx));
    guard('updateDailyHeatmap', () => updateDailyHeatmap(payload));
    guard('updateDonutChart', () => updateDonutChart(payload));
    guard('updateCacheChart', () => updateCacheChart(payload));
    guard('updateHourlyChart', () => updateHourlyChart(payload, ctx));
    guard('updateSparklines', () => updateSparklines(payload));
  }

  window.DashboardCharts = {
    updateCharts,
    setFocus,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
