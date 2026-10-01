/**
 * DashboardSkyline - the "Token skyline": the last-30-days heatmap as an isometric 3D city.
 *
 * Each day is a tower standing where its heatmap cell sits (Monday-first week rows). Height is
 * linear in total tokens, the top colour is the amber cost ramp (same quantile bucketing as the
 * heatmap). Drag, arrow keys or inertia rotate the city around its vertical axis.
 *
 * Rendering is Canvas 2D with an orthographic projection (fixed 32 degree elevation) and a
 * painter's algorithm. Every colour is read from the CSS custom properties at draw time and
 * re-read on `themechange` and OS colour scheme changes.
 *
 * Public API: window.DashboardSkyline = { update(data, ctx), setFocus(cells | null) }
 * - update(data, ctx): called by dashboard.js after every successful render.
 * - setFocus(cells): cells align 1:1 with heatmap_daily ({ total_tokens, call_count, cost_cached_usd }).
 *   The focused values become the solid towers and the full totals stay as translucent ghosts.
 */
(function () {
  'use strict';

  const VIEW_KEY = 'aiud.heatmapView';
  const EMPTY_TEXT = 'No usage in this period';
  const DAYS = 30;
  const COLS = 7;

  /* ---- projection and geometry (world units are cell pitches) ---- */
  const DEFAULT_YAW = -35;
  const ELEVATION = (32 * Math.PI) / 180;
  const SIN_E = Math.sin(ELEVATION);
  const COS_E = Math.cos(ELEVATION);
  const FOOTPRINT = 0.7;
  // The tallest day, in cell pitches: a little over half the height of the tallest calendar (six week rows).
  const MAX_TOWER = 3.2;
  const MIN_TOWER_PX = 3;
  const SLAB_PX = 6;
  // The city fills this share of the stage at its widest and tallest yaw (the rest is an even margin).
  const FILL = 0.94;
  const MIN_HEIGHT = 260;

  /* ---- lighting: a fixed light in camera space (right, up, toward the viewer), upper left ---- */
  const LIGHT = (() => {
    const v = [-0.6, 0.5, 0.62];
    const len = Math.hypot(v[0], v[1], v[2]);
    return v.map((n) => n / len);
  })();
  const AMBIENT = 0.55;
  const DIFFUSE = 0.45;
  // Brightest and darkest possible side face under that light (visible faces only).
  const LIT_MAX = AMBIENT + DIFFUSE * Math.hypot(LIGHT[0], COS_E * LIGHT[2] - SIN_E * LIGHT[1]);
  // How much darker (OKLab lightness) the most and least lit side faces are than the top.
  const LIT_DARKER = 0.12;
  const SHADOW_DARKER = 0.28;
  const EDGE_ALPHA = 0.35;
  const GHOST_ALPHA = 0.05;
  // Hover: other towers drop to 45% alpha over the plate.
  const DIM_MIX = 0.55;
  const DIM_CHROMA_LOSS = 0.4;

  /* ---- interaction and motion ---- */
  const DRAG_DEG_PER_PX = 0.5;
  const TAP_SLOP_PX = 4;
  const FRICTION_PER_FRAME = 0.92;
  const FRAME_MS = 1000 / 60;
  const STOP_DEG_PER_FRAME = 0.05;
  const RESET_MS = 400;
  const KEY_STEP_DEG = 15;
  const KEY_MS = 200;
  const RISE_MS = 700;
  const RISE_STAGGER_MS = 12;
  const REFRESH_MS = 400;
  const FOCUS_MS = 300;
  const DIM_MS = 120;
  const EPS = 1e-4;

  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const moneyFormat = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const dayFormat = new Intl.DateTimeFormat(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  });
  const shortDayFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

  /* ------------------------------------------------------------------ helpers */

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  }

  let reducedQuery = null;

  function prefersReducedMotion() {
    try {
      if (!reducedQuery) reducedQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
      return reducedQuery.matches;
    } catch {
      return false;
    }
  }

  function now() {
    return performance.now();
  }

  function fmtInt(value) {
    return numberFormat.format(Math.round(num(value)));
  }

  function fmtUsd(value) {
    const n = num(value);
    if (n === 0) return '$0.00';
    if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
    return `$${moneyFormat.format(n)}`;
  }

  function fmtCompact(value) {
    const utils = window.DashboardUtils || {};
    const n = num(value);
    if (typeof utils.formatCompactNumber === 'function') return utils.formatCompactNumber(n);
    const abs = Math.abs(n);
    if (abs >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
    return String(n);
  }

  function parseDay(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return null;
    return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  }

  function fmtDay(value) {
    const date = parseDay(value);
    return date ? dayFormat.format(date) : String(value || 'Unknown date');
  }

  function fmtShortDay(value) {
    const date = parseDay(value);
    return date ? shortDayFormat.format(date) : String(value || '');
  }

  /** Monday-first weekday column (0-6) of a YYYY-MM-DD date. */
  function weekdayColumn(value) {
    const date = parseDay(value);
    return date ? (date.getUTCDay() + 6) % 7 : 0;
  }

  /** CSS cubic-bezier(x1, y1, x2, y2) as a function of progress. */
  function cubicBezier(x1, y1, x2, y2) {
    const cx = 3 * x1;
    const bx = 3 * (x2 - x1) - cx;
    const ax = 1 - cx - bx;
    const cy = 3 * y1;
    const by = 3 * (y2 - y1) - cy;
    const ay = 1 - cy - by;
    const sampleX = (t) => ((ax * t + bx) * t + cx) * t;
    const sampleY = (t) => ((ay * t + by) * t + cy) * t;
    const slopeX = (t) => (3 * ax * t + 2 * bx) * t + cx;
    return (x) => {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      let t = x;
      for (let i = 0; i < 8; i += 1) {
        const error = sampleX(t) - x;
        if (Math.abs(error) < 1e-5) return sampleY(t);
        const slope = slopeX(t);
        if (Math.abs(slope) < 1e-6) break;
        t -= error / slope;
      }
      let lo = 0;
      let hi = 1;
      t = x;
      for (let i = 0; i < 24; i += 1) {
        const value = sampleX(t);
        if (Math.abs(value - x) < 1e-5) break;
        if (value < x) lo = t;
        else hi = t;
        t = (lo + hi) / 2;
      }
      return sampleY(t);
    };
  }

  const easeRise = cubicBezier(0.2, 0.7, 0.1, 1);
  const easeOutCubic = (p) => 1 - (1 - p) ** 3;

  /* ------------------------------------------------------------------ colour */

  let colorContext = null;
  const colorCache = new Map();

  /** Resolve any CSS colour string to [r, g, b, a] using the canvas colour parser. */
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

  function rgba(color, scale, alpha) {
    const k = scale == null ? 1 : scale;
    const a = alpha == null ? 1 : alpha;
    return `rgba(${Math.round(color[0] * k)}, ${Math.round(color[1] * k)}, ${Math.round(color[2] * k)}, ${Number(a.toFixed(3))})`;
  }

  function mix(a, b, t) {
    return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
  }

  /** Every colour comes straight from the CSS tokens, read at draw time (parsed colours are cached). */
  function readPalette() {
    const style = getComputedStyle(document.documentElement);
    const token = (name, fallback) => parseColor(style.getPropertyValue(`--${name}`).trim() || fallback);
    return {
      surface: token('surface', 'white'),
      surface2: token('surface-2', 'whitesmoke'),
      line: token('line', 'gray'),
      lineStrong: token('line-strong', style.getPropertyValue('--line').trim() || 'gray'),
      ink: token('ink', 'black'),
      ramp: [1, 2, 3, 4, 5, 6].map((step) => token(`money-${step}`, 'orange')),
    };
  }

  /* OKLab / OKLCH: shade and dim by moving lightness while the hue and (most of) the chroma stay put. */

  function srgbToLinear(value) {
    const c = value / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }

  function linearToSrgb(value) {
    const c = clamp(value, 0, 1);
    return 255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);
  }

  function toOklch(rgb) {
    const r = srgbToLinear(rgb[0]);
    const g = srgbToLinear(rgb[1]);
    const b = srgbToLinear(rgb[2]);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    const a = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
    const bb = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
    return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, Math.hypot(a, bb), Math.atan2(bb, a)];
  }

  function oklchToLinear(L, C, h) {
    const a = C * Math.cos(h);
    const b = C * Math.sin(h);
    const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
    const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
    const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
    return [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
    ];
  }

  /** OKLCH back to sRGB; if the colour is out of gamut, chroma is reduced (lightness and hue are kept). */
  function fromOklch(L, C, h) {
    const lightness = clamp(L, 0, 1);
    const inGamut = (lin) => lin.every((v) => v >= -0.0005 && v <= 1.0005);
    let lin = oklchToLinear(lightness, C, h);
    if (!inGamut(lin)) {
      let lo = 0;
      let hi = C;
      for (let i = 0; i < 10; i += 1) {
        const mid = (lo + hi) / 2;
        if (inGamut(oklchToLinear(lightness, mid, h))) lo = mid;
        else hi = mid;
      }
      lin = oklchToLinear(lightness, lo, h);
    }
    return lin.map(linearToSrgb);
  }

  /** Lighting of a side face as 0 (in shadow) to 1 (brightest possible), from the brightness formula. */
  function litness(brightnessValue) {
    return clamp((brightnessValue - AMBIENT) / (LIT_MAX - AMBIENT), 0, 1);
  }

  /** The face colour under the light: lower lightness, same hue and chroma (reduced only to stay in gamut). */
  function shadeColor(rgb, lit, extra) {
    const darker = lerp(SHADOW_DARKER, LIT_DARKER, lit) + (extra || 0);
    const [L, C, h] = toOklch(rgb);
    return fromOklch(L * (1 - darker), C, h);
  }

  /** 45% alpha over the plate, done in OKLCH: lightness moves towards the plate, hue stays, chroma fades less. */
  function dimColor(rgb, backdrop, amount) {
    if (amount <= 0) return rgb;
    const [L, C, h] = toOklch(rgb);
    const [Lb] = toOklch(backdrop);
    return fromOklch(lerp(L, Lb, amount), C * (1 - DIM_CHROMA_LOSS * amount), h);
  }

  /** A (possibly fractional) ramp step 1-6 as an rgb triple; fractions blend neighbouring steps. */
  function rampColor(pal, step) {
    const k = clamp(step, 1, 6);
    const lo = Math.floor(k);
    const hi = Math.min(6, Math.ceil(k));
    if (lo === hi) return pal.ramp[lo - 1];
    return mix(pal.ramp[lo - 1], pal.ramp[hi - 1], k - lo);
  }

  /** Quantile bucketing of the non-zero values into steps 1-6 (ties share a step); 0 stays 0. */
  function quantileSteps(values) {
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

  /* ------------------------------------------------------------------ state */

  const state = {
    ready: false,
    loaded: false,
    view: 'grid',
    cells: [],
    structureKey: '',
    empty: true,
    rows: 5,
    towers: [],
    focus: null,
    ghost: { value: 0, anim: null },
    yaw: DEFAULT_YAW,
    yawAnim: null,
    inertia: 0,
    hover: -1,
    selected: -1,
    width: 0,
    height: 0,
    fit: null,
    hits: [],
  };

  const dom = {};
  let ctx2d = null;
  let rafId = 0;
  let lastFrame = 0;
  let drag = null;
  let observedWidth = 0;
  let tooltipEl = null;
  let tooltipIndex = -1;
  let loggedDrawError = false;

  /* ------------------------------------------------------------------ persistence */

  function loadView() {
    try {
      const stored = window.localStorage.getItem(VIEW_KEY);
      if (stored === 'city' || stored === 'grid') return stored;
    } catch {
      /* storage can be blocked */
    }
    return 'grid';
  }

  function saveView(view) {
    try {
      window.localStorage.setItem(VIEW_KEY, view);
    } catch {
      /* storage can be blocked */
    }
  }

  /* ------------------------------------------------------------------ data */

  function normalizeRows(rows) {
    return (Array.isArray(rows) ? rows : [])
      .filter((row) => row && typeof row === 'object' && /^\d{4}-\d{2}-\d{2}$/.test(String(row.date || '')))
      .sort((a, b) => String(a.date).localeCompare(String(b.date)))
      .slice(-DAYS)
      .map((row) => ({
        date: String(row.date),
        tokens: num(row.total_tokens),
        cost: num(row.cost_cached_usd),
        calls: num(row.call_count),
      }));
  }

  function normalizeFocus(cells) {
    if (!Array.isArray(cells)) return null;
    return cells.map((cell) => ({
      tokens: num(cell && cell.total_tokens),
      cost: num(cell && cell.cost_cached_usd),
      calls: num(cell && cell.call_count),
    }));
  }

  function hasUsage(cells) {
    return cells.some((cell) => cell.tokens > 0 || cell.cost > 0 || cell.calls > 0);
  }

  /** Target heights (in cell pitches) and ramp steps for every day, from the totals or the focus. */
  function computeTargets() {
    const { cells, focus } = state;
    const maxTokens = cells.reduce((max, cell) => Math.max(max, cell.tokens), 0);
    const unit = maxTokens > 0 ? MAX_TOWER / maxTokens : 0;
    const totalSteps = quantileSteps(cells.map((cell) => cell.cost));
    if (!focus) {
      return cells.map((cell, i) => {
        const height = cell.tokens * unit;
        return { h: height, g: height, c: totalSteps[i] };
      });
    }
    const focused = cells.map((_, i) => focus[i] || { tokens: 0, cost: 0, calls: 0 });
    const focusSteps = quantileSteps(focused.map((cell) => cell.cost));
    return cells.map((cell, i) => {
      const solid = focused[i].tokens * unit;
      return { h: solid, g: Math.max(cell.tokens * unit, solid), c: focusSteps[i] };
    });
  }

  function layoutTowers() {
    const { cells } = state;
    const offset = cells.length ? weekdayColumn(cells[0].date) : 0;
    state.rows = Math.max(1, Math.ceil((offset + cells.length) / COLS));
    const targets = computeTargets();
    state.towers = cells.map((cell, i) => {
      const slot = offset + i;
      const col = slot % COLS;
      const row = Math.floor(slot / COLS);
      return {
        i,
        cell,
        cx: col + 0.5 - COLS / 2,
        cz: row + 0.5 - state.rows / 2,
        h: targets[i].h,
        g: targets[i].g,
        cs: targets[i].c,
        target: targets[i],
        anim: null,
        dim: 0,
        anchor: null,
      };
    });
    state.fit = null;
  }

  /* ------------------------------------------------------------------ tweens */

  function startTween(tower, to, duration, delay, ease) {
    const from = { h: tower.h, g: tower.g, c: tower.cs };
    // Colour only blends between two real ramp steps; entering or leaving zero is a hard switch.
    if (from.c <= 0 && to.c > 0) tower.cs = to.c;
    tower.target = to;
    tower.anim = { t0: now(), dur: duration, delay, from, to, ease };
  }

  function snapTower(tower, to) {
    tower.h = to.h;
    tower.g = to.g;
    tower.cs = to.c;
    tower.target = to;
    tower.anim = null;
  }

  function sameTarget(a, b) {
    return Math.abs(a.h - b.h) < EPS && Math.abs(a.g - b.g) < EPS && a.c === b.c;
  }

  function setGhost(target, animate, duration) {
    const g = state.ghost;
    if (!animate || prefersReducedMotion()) {
      g.value = target;
      g.anim = null;
      return;
    }
    g.anim = { t0: now(), dur: duration, from: g.value, to: target };
  }

  /** Move every tower to its (re)computed target. mode: 'snap' | 'refresh' | 'focus' | 'rise'. */
  function applyTargets(mode) {
    const targets = computeTargets();
    state.fit = null;
    const animate = mode !== 'snap' && state.view === 'city' && !prefersReducedMotion();
    state.towers.forEach((tower, i) => {
      const to = targets[i];
      if (!animate) {
        snapTower(tower, to);
        return;
      }
      if (mode === 'rise') {
        tower.h = 0;
        tower.g = 0;
        tower.cs = to.c;
        startTween(tower, to, RISE_MS, i * RISE_STAGGER_MS, easeRise);
        return;
      }
      if (sameTarget(tower.target, to)) return;
      startTween(tower, to, mode === 'focus' ? FOCUS_MS : REFRESH_MS, 0, easeRise);
    });
  }

  function stepTowers(time) {
    let active = false;
    state.towers.forEach((tower) => {
      const anim = tower.anim;
      if (!anim) return;
      const p = clamp((time - anim.t0 - anim.delay) / anim.dur, 0, 1);
      const e = anim.ease(p);
      tower.h = lerp(anim.from.h, anim.to.h, e);
      tower.g = lerp(anim.from.g, anim.to.g, e);
      if (anim.from.c > 0 && anim.to.c > 0) tower.cs = lerp(anim.from.c, anim.to.c, e);
      if (p >= 1) {
        tower.h = anim.to.h;
        tower.g = anim.to.g;
        tower.cs = anim.to.c;
        tower.anim = null;
      } else {
        active = true;
      }
    });
    return active;
  }

  function stepGhost(time) {
    const g = state.ghost;
    if (!g.anim) return false;
    const p = clamp((time - g.anim.t0) / g.anim.dur, 0, 1);
    g.value = lerp(g.anim.from, g.anim.to, easeOutCubic(p));
    if (p >= 1) {
      g.value = g.anim.to;
      g.anim = null;
      return false;
    }
    return true;
  }

  /** Hover emphasis: towers other than the active one fade towards the plate colour. */
  function stepDim(dt) {
    const emphasis = activeIndex();
    const reduced = prefersReducedMotion();
    let active = false;
    state.towers.forEach((tower) => {
      const target = emphasis >= 0 && tower.i !== emphasis ? 1 : 0;
      if (reduced) {
        tower.dim = target;
        return;
      }
      if (tower.dim === target) return;
      const step = dt / DIM_MS;
      tower.dim = target > tower.dim ? Math.min(target, tower.dim + step) : Math.max(target, tower.dim - step);
      if (tower.dim !== target) active = true;
    });
    return active;
  }

  function normalizeYaw(yaw) {
    return ((((yaw + 180) % 360) + 360) % 360) - 180;
  }

  function animateYaw(target, duration) {
    state.inertia = 0;
    if (prefersReducedMotion() || duration <= 0) {
      state.yaw = normalizeYaw(target);
      state.yawAnim = null;
      scheduleFrame();
      return;
    }
    state.yawAnim = { t0: now(), dur: duration, from: state.yaw, to: target };
    scheduleFrame();
  }

  function resetYaw() {
    const delta = normalizeYaw(DEFAULT_YAW - state.yaw);
    animateYaw(state.yaw + delta, RESET_MS);
  }

  function stepYaw(time, dt) {
    if (state.yawAnim) {
      const anim = state.yawAnim;
      const p = clamp((time - anim.t0) / anim.dur, 0, 1);
      state.yaw = lerp(anim.from, anim.to, easeOutCubic(p));
      if (p >= 1) {
        state.yaw = normalizeYaw(anim.to);
        state.yawAnim = null;
        return false;
      }
      return true;
    }
    if (state.inertia !== 0) {
      state.yaw = normalizeYaw(state.yaw + state.inertia * dt);
      state.inertia *= FRICTION_PER_FRAME ** (dt / FRAME_MS);
      if (Math.abs(state.inertia) * FRAME_MS < STOP_DEG_PER_FRAME) {
        state.inertia = 0;
        return false;
      }
      return true;
    }
    return false;
  }

  /* ------------------------------------------------------------------ frame loop */

  function isVisible() {
    return state.ready && state.view === 'city' && !state.empty && state.width > 0 && state.height > 0;
  }

  function scheduleFrame() {
    if (rafId || !isVisible()) return;
    lastFrame = now();
    rafId = window.requestAnimationFrame(frame);
  }

  function frame() {
    rafId = 0;
    if (!isVisible()) return;
    const time = now();
    const dt = clamp(time - lastFrame, 0, 64);
    lastFrame = time;
    let active = false;
    if (stepTowers(time)) active = true;
    if (stepGhost(time)) active = true;
    if (stepYaw(time, dt)) active = true;
    if (stepDim(dt)) active = true;
    try {
      draw();
    } catch (error) {
      if (!loggedDrawError && window.console && console.error) console.error('Skyline draw failed', error);
      loggedDrawError = true;
      return;
    }
    if (state.selected >= 0) positionSelectedTooltip();
    if (active) {
      rafId = window.requestAnimationFrame(frame);
    }
  }

  /* ------------------------------------------------------------------ projection */

  /**
   * The scale is fixed per canvas size and data: it is the largest one for which the plate and every
   * tower (at its full height) fit the canvas at every yaw, found from the rotated bounding boxes.
   * That keeps the city from breathing while it turns, and nothing ever clips.
   */
  function computeFit(width, height, rows, towers) {
    const halfW = COLS / 2;
    const halfD = rows / 2;
    const half = FOOTPRINT / 2;
    // Points that can ever be extremal: the plate corners and the roof corners of every tower.
    const points = [[-halfW, -halfD, 0], [halfW, -halfD, 0], [halfW, halfD, 0], [-halfW, halfD, 0]];
    towers.forEach((tower) => {
      const h = Math.max(tower.target.h, tower.target.g);
      if (h <= EPS) return;
      [-half, half].forEach((dx) => [-half, half].forEach((dz) => points.push([tower.cx + dx, tower.cz + dz, h])));
    });
    let minY = 0;
    let maxY = 0;
    let maxW = 0;
    for (let degrees = 0; degrees < 360; degrees += 3) {
      const rad = (degrees * Math.PI) / 180;
      const cos = Math.cos(rad);
      const sin = Math.sin(rad);
      let left = Infinity;
      let right = -Infinity;
      points.forEach(([x, z, y]) => {
        const sx = x * cos - z * sin;
        const sy = (x * sin + z * cos) * SIN_E - y * COS_E;
        left = Math.min(left, sx);
        right = Math.max(right, sx);
        minY = Math.min(minY, sy);
        maxY = Math.max(maxY, sy);
      });
      maxW = Math.max(maxW, right - left);
    }
    const availW = Math.max(40, width * FILL);
    const availH = Math.max(40, height * FILL);
    const scale = Math.max(8, Math.min(availW / maxW, (availH - SLAB_PX) / (maxY - minY)));
    // Anchor on the box that holds the city at every yaw (tallest tower at the back, plate corner
    // at the front) and centre that box, so the margins above and below are equal.
    const used = (maxY - minY) * scale + SLAB_PX;
    return {
      scale,
      ox: width / 2,
      oy: (height - used) / 2 - minY * scale,
    };
  }

  function syncCanvasSize() {
    if (!dom.canvas || !dom.stage) return false;
    const width = dom.stage.clientWidth;
    const height = dom.stage.clientHeight;
    if (!width || !height) {
      state.width = 0;
      state.height = 0;
      return false;
    }
    const dpr = window.devicePixelRatio || 1;
    const bitmapW = Math.round(width * dpr);
    const bitmapH = Math.round(height * dpr);
    if (dom.canvas.width !== bitmapW || dom.canvas.height !== bitmapH) {
      dom.canvas.width = bitmapW;
      dom.canvas.height = bitmapH;
    }
    state.width = width;
    state.height = height;
    state.fit = computeFit(width, height, state.rows, state.towers);
    return true;
  }

  /* ------------------------------------------------------------------ drawing */

  function tracePolygon(c, points) {
    c.beginPath();
    c.moveTo(points[0][0], points[0][1]);
    for (let i = 1; i < points.length; i += 1) c.lineTo(points[i][0], points[i][1]);
    c.closePath();
  }

  function convexHull(points) {
    const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (pts.length < 3) return pts;
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [];
    pts.forEach((p) => {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
      lower.push(p);
    });
    const upper = [];
    for (let i = pts.length - 1; i >= 0; i -= 1) {
      const p = pts[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
      upper.push(p);
    }
    lower.pop();
    upper.pop();
    return lower.concat(upper);
  }

  function pointInPolygon(x, y, points) {
    let inside = false;
    for (let i = 0, j = points.length - 1; i < points.length; j = i, i += 1) {
      const xi = points[i][0];
      const yi = points[i][1];
      const xj = points[j][0];
      const yj = points[j][1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function draw() {
    if (!ctx2d || !isVisible()) return;
    const pal = readPalette();
    if (!state.fit) state.fit = computeFit(state.width, state.height, state.rows, state.towers);
    const { scale, ox, oy } = state.fit;
    const bitmapW = dom.canvas.width;
    const bitmapH = dom.canvas.height;
    const c = ctx2d;
    c.setTransform(bitmapW / state.width, 0, 0, bitmapH / state.height, 0, 0);
    c.clearRect(0, 0, state.width, state.height);
    c.lineJoin = 'round';
    c.lineCap = 'round';

    const yawRad = (state.yaw * Math.PI) / 180;
    const cosY = Math.cos(yawRad);
    const sinY = Math.sin(yawRad);
    const slabUnits = SLAB_PX / (scale * COS_E);
    const minUnits = MIN_TOWER_PX / (scale * COS_E);
    const project = (x, y, z) => [
      ox + (x * cosY - z * sinY) * scale,
      oy + ((x * sinY + z * cosY) * SIN_E - y * COS_E) * scale,
    ];
    const viewDepth = (nx, nz) => nx * sinY + nz * cosY;
    /** Fixed camera-space light on a vertical face with world normal (nx, 0, nz). */
    const brightness = (nx, nz) => {
      const right = nx * cosY - nz * sinY;
      const toward = nx * sinY + nz * cosY;
      const dot = right * LIGHT[0] - toward * SIN_E * LIGHT[1] + toward * COS_E * LIGHT[2];
      return AMBIENT + DIFFUSE * Math.max(0, dot);
    };
    const edge = rgba(pal.surface, 1, EDGE_ALPHA);

    // Base plate: the size of the calendar grid, a flat-shaded slab.
    const halfW = COLS / 2;
    const halfD = state.rows / 2;
    const plateSides = [
      [1, 0, halfW, -halfD, halfW, halfD],
      [-1, 0, -halfW, halfD, -halfW, -halfD],
      [0, 1, halfW, halfD, -halfW, halfD],
      [0, -1, -halfW, -halfD, halfW, -halfD],
    ];
    plateSides.forEach(([nx, nz, xa, za, xb, zb]) => {
      if (viewDepth(nx, nz) <= EPS) return;
      const pts = [project(xa, 0, za), project(xb, 0, zb), project(xb, -slabUnits, zb), project(xa, -slabUnits, za)];
      tracePolygon(c, pts);
      c.fillStyle = rgba(shadeColor(pal.surface2, litness(brightness(nx, nz))));
      c.fill();
      c.strokeStyle = rgba(pal.line);
      c.lineWidth = 1;
      c.stroke();
    });
    tracePolygon(c, [project(-halfW, 0, -halfD), project(halfW, 0, -halfD), project(halfW, 0, halfD), project(-halfW, 0, halfD)]);
    c.fillStyle = rgba(pal.surface2);
    c.fill();
    c.strokeStyle = rgba(pal.line);
    c.lineWidth = 1;
    c.stroke();

    // Towers, far to near.
    const order = state.towers
      .map((tower) => ({ tower, depth: tower.cx * sinY + tower.cz * cosY }))
      .sort((a, b) => a.depth - b.depth || a.tower.i - b.tower.i);
    const half = FOOTPRINT / 2;
    const todayIndex = state.towers.length - 1;
    const hits = [];
    const ghostMix = state.ghost.value;

    order.forEach(({ tower }) => {
      const solidH = tower.h > EPS ? Math.max(tower.h, minUnits) : 0;
      const ghostH = ghostMix > EPS && tower.g > EPS ? Math.max(tower.g, minUnits) : 0;
      const boxH = Math.max(solidH, ghostH);
      const isTile = solidH === 0 && tower.cs <= 0;
      const dim = tower.dim * DIM_MIX;
      const x0 = tower.cx - half;
      const x1 = tower.cx + half;
      const z0 = tower.cz - half;
      const z1 = tower.cz + half;
      const sides = [
        [1, 0, x1, z0, x1, z1],
        [-1, 0, x0, z1, x0, z0],
        [0, 1, x1, z1, x0, z1],
        [0, -1, x0, z0, x1, z0],
      ].filter(([nx, nz]) => viewDepth(nx, nz) > EPS);
      const topAt = (y) => [project(x0, y, z0), project(x1, y, z0), project(x1, y, z1), project(x0, y, z1)];
      const sidePolys = (y) => sides.map(([, , xa, za, xb, zb]) => [
        project(xa, 0, za), project(xb, 0, zb), project(xb, y, zb), project(xa, y, za),
      ]);

      const topRgb = tower.cs > 0 ? rampColor(pal, tower.cs) : pal.surface2;
      const withDim = (rgb) => dimColor(rgb, pal.surface2, dim);
      c.lineWidth = 1;
      c.strokeStyle = edge;

      if (isTile) {
        tracePolygon(c, topAt(0));
        c.fillStyle = rgba(pal.surface2);
        c.fill();
        c.strokeStyle = rgba(pal.line);
        c.stroke();
      } else {
        if (solidH > 0) {
          const polys = sidePolys(solidH);
          sides.forEach(([nx, nz], k) => {
            tracePolygon(c, polys[k]);
            c.fillStyle = rgba(withDim(shadeColor(topRgb, litness(brightness(nx, nz)))));
            c.fill();
            c.stroke();
          });
        }
        tracePolygon(c, topAt(solidH));
        c.fillStyle = rgba(withDim(topRgb));
        c.fill();
        c.strokeStyle = tower.cs > 0 ? edge : rgba(pal.line);
        c.stroke();
      }

      if (ghostH > 0) {
        const polys = sidePolys(ghostH);
        c.fillStyle = rgba(pal.ink, 1, GHOST_ALPHA * ghostMix);
        c.strokeStyle = rgba(pal.lineStrong, 1, ghostMix);
        polys.concat([topAt(ghostH)]).forEach((poly) => {
          tracePolygon(c, poly);
          c.fill();
          c.stroke();
        });
      }

      const emphasised = tower.i === activeIndex();
      if (tower.i === todayIndex) {
        tracePolygon(c, topAt(solidH));
        c.strokeStyle = rgba(pal.ink, 1, 1 - dim);
        c.lineWidth = 1.5;
        c.stroke();
      }
      if (emphasised) {
        const base = topAt(0);
        const roof = topAt(boxH);
        tracePolygon(c, convexHull(base.concat(roof)));
        c.strokeStyle = rgba(pal.ink);
        c.lineWidth = 1.5;
        c.stroke();
      }

      const roof = topAt(boxH);
      tower.anchor = [
        (roof[0][0] + roof[1][0] + roof[2][0] + roof[3][0]) / 4,
        (roof[0][1] + roof[1][1] + roof[2][1] + roof[3][1]) / 4,
      ];
      hits.push({ i: tower.i, polys: [roof].concat(boxH > 0 ? sidePolys(boxH) : []) });
    });
    state.hits = hits;
  }

  /* ------------------------------------------------------------------ hover, tooltip, selection */

  function activeIndex() {
    return state.hover >= 0 ? state.hover : state.selected;
  }

  function hitTest(x, y) {
    for (let k = state.hits.length - 1; k >= 0; k -= 1) {
      const hit = state.hits[k];
      for (let p = 0; p < hit.polys.length; p += 1) {
        if (pointInPolygon(x, y, hit.polys[p])) return hit.i;
      }
    }
    return -1;
  }

  function ensureTooltip() {
    if (tooltipEl && tooltipEl.isConnected) return tooltipEl;
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'chart-tooltip skyline-tooltip';
    tooltipEl.setAttribute('aria-hidden', 'true');
    tooltipEl.hidden = true;
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function tooltipMarkup(cell) {
    return `<div class="chart-tooltip__title">${escapeHtml(fmtDay(cell.date))}</div>
      <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--square chart-swatch--money"></i><span class="chart-tooltip__label">Spent</span><span class="chart-tooltip__value">${escapeHtml(fmtUsd(cell.cost))}</span></div>
      <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--square chart-swatch--tokens"></i><span class="chart-tooltip__label">Tokens</span><span class="chart-tooltip__value">${escapeHtml(fmtInt(cell.tokens))}</span></div>
      <div class="chart-tooltip__row"><i class="chart-swatch chart-swatch--line chart-swatch--count"></i><span class="chart-tooltip__label">API calls</span><span class="chart-tooltip__value">${escapeHtml(fmtInt(cell.calls))}</span></div>`;
  }

  function placeTooltip(anchorX, anchorY) {
    const el = tooltipEl;
    if (!el || el.hidden) return;
    const margin = 8;
    const gap = 14;
    const width = el.offsetWidth;
    const height = el.offsetHeight;
    const viewWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewHeight = window.innerHeight;
    let x = anchorX + gap;
    if (x + width > viewWidth - margin) x = anchorX - gap - width;
    x = clamp(x, margin, Math.max(margin, viewWidth - width - margin));
    const y = clamp(anchorY - height / 2, margin, Math.max(margin, viewHeight - height - margin));
    el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
  }

  function showTooltip(index, anchorX, anchorY) {
    const tower = state.towers[index];
    if (!tower) {
      hideTooltip();
      return;
    }
    const el = ensureTooltip();
    if (tooltipIndex !== index || el.hidden) {
      el.innerHTML = tooltipMarkup(tower.cell);
      tooltipIndex = index;
    }
    el.hidden = false;
    placeTooltip(anchorX, anchorY);
  }

  function hideTooltip() {
    tooltipIndex = -1;
    if (tooltipEl) tooltipEl.hidden = true;
  }

  function positionSelectedTooltip() {
    if (state.hover >= 0 || state.selected < 0 || !dom.canvas) return;
    const tower = state.towers[state.selected];
    if (!tower || !tower.anchor) return;
    const rect = dom.canvas.getBoundingClientRect();
    showTooltip(state.selected, rect.left + tower.anchor[0], rect.top + tower.anchor[1]);
  }

  function setHover(index) {
    if (state.hover === index) return;
    state.hover = index;
    scheduleFrame();
  }

  function clearSelection() {
    if (state.selected < 0) return;
    state.selected = -1;
    if (state.hover < 0) hideTooltip();
    scheduleFrame();
  }

  /* ------------------------------------------------------------------ pointer and keyboard */

  function canvasPoint(event) {
    const rect = dom.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function onPointerDown(event) {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    state.inertia = 0;
    state.yawAnim = null;
    drag = {
      id: event.pointerId,
      type: event.pointerType,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      moved: false,
      samples: [{ t: now(), yaw: state.yaw }],
    };
    try {
      dom.canvas.setPointerCapture(event.pointerId);
    } catch {
      /* the pointer may already be gone */
    }
    dom.canvas.classList.add('is-grabbing');
  }

  function onPointerMove(event) {
    if (drag && event.pointerId === drag.id) {
      if (!drag.moved) {
        if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < TAP_SLOP_PX) return;
        drag.moved = true;
        setHover(-1);
        if (state.selected < 0) hideTooltip();
      }
      const dx = event.clientX - drag.lastX;
      drag.lastX = event.clientX;
      // Dragging right carries the near side of the city to the right.
      state.yaw = normalizeYaw(state.yaw - dx * DRAG_DEG_PER_PX);
      const time = now();
      drag.samples.push({ t: time, yaw: state.yaw });
      while (drag.samples.length > 2 && time - drag.samples[0].t > 100) drag.samples.shift();
      scheduleFrame();
      return;
    }
    if (event.pointerType === 'touch' || drag) return;
    const point = canvasPoint(event);
    const index = hitTest(point.x, point.y);
    setHover(index);
    if (index >= 0) showTooltip(index, event.clientX, event.clientY);
    else if (state.selected >= 0) positionSelectedTooltip();
    else hideTooltip();
  }

  function releaseDrag(event, cancelled) {
    if (!drag || event.pointerId !== drag.id) return;
    const finished = drag;
    drag = null;
    dom.canvas.classList.remove('is-grabbing');
    try {
      dom.canvas.releasePointerCapture(event.pointerId);
    } catch {
      /* already released */
    }
    if (cancelled) return;
    if (finished.moved) {
      if (prefersReducedMotion()) return;
      const time = now();
      const first = finished.samples[0];
      const last = finished.samples[finished.samples.length - 1];
      const span = last.t - first.t;
      if (time - last.t > 80 || span <= 0) return;
      const velocity = normalizeYaw(last.yaw - first.yaw) / span;
      if (Math.abs(velocity) * FRAME_MS >= STOP_DEG_PER_FRAME) {
        state.inertia = velocity;
        scheduleFrame();
      }
      return;
    }
    handleTap(event, finished.type);
  }

  function handleTap(event, pointerType) {
    if (pointerType === 'mouse') return;
    const point = canvasPoint(event);
    const index = hitTest(point.x, point.y);
    if (index < 0 || index === state.selected) {
      clearSelection();
      return;
    }
    state.selected = index;
    positionSelectedTooltip();
    scheduleFrame();
  }

  function onPointerLeave(event) {
    if (drag || event.pointerType === 'touch') return;
    setHover(-1);
    if (state.selected < 0) hideTooltip();
    else positionSelectedTooltip();
  }

  function onKeyDown(event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      // Left carries the near side of the city leftwards, matching a drag to the left.
      const direction = event.key === 'ArrowLeft' ? 1 : -1;
      const base = state.yawAnim ? state.yawAnim.to : state.yaw;
      animateYaw(base + direction * KEY_STEP_DEG, KEY_MS);
    } else if (event.key === 'Home') {
      event.preventDefault();
      resetYaw();
    } else if (event.key === 'Escape' && state.selected >= 0) {
      clearSelection();
    }
  }

  function bindCanvas() {
    const canvas = dom.canvas;
    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', (event) => releaseDrag(event, false));
    canvas.addEventListener('pointercancel', (event) => releaseDrag(event, true));
    canvas.addEventListener('lostpointercapture', (event) => releaseDrag(event, true));
    canvas.addEventListener('pointerleave', onPointerLeave);
    canvas.addEventListener('dblclick', resetYaw);
    canvas.addEventListener('keydown', onKeyDown);
    document.addEventListener('pointerdown', (event) => {
      if (state.selected >= 0 && !(dom.canvas && dom.canvas.contains(event.target))) clearSelection();
    });
    window.addEventListener('scroll', () => {
      setHover(-1);
      if (state.selected >= 0) positionSelectedTooltip();
      else hideTooltip();
    }, { passive: true });
  }

  /* ------------------------------------------------------------------ view switching */

  /** Height of the grid heatmap block (host plus legend), measured even while it is hidden. */
  function measureGrid() {
    const { tray, host } = dom;
    const legend = dom.gridLegend;
    if (!tray || !host) return 0;
    const wasCity = tray.classList.contains('is-city');
    if (wasCity) tray.classList.remove('is-city');
    const hostRect = host.getBoundingClientRect();
    let bottom = hostRect.bottom;
    if (legend && legend.offsetHeight > 0) bottom = Math.max(bottom, legend.getBoundingClientRect().bottom);
    if (wasCity) tray.classList.add('is-city');
    return Math.max(0, bottom - hostRect.top);
  }

  function sizeToGrid() {
    const measured = measureGrid();
    const height = state.loaded && state.empty && measured > 0 ? measured : Math.max(MIN_HEIGHT, measured);
    dom.skyline.style.height = `${Number(height.toFixed(2))}px`;
  }

  /** A pinned day card belongs to the grid; close it through the heatmap's own Escape handling. */
  function dismissGridDetail() {
    const card = document.getElementById('heatmap-detail-card');
    if (!card || card.hidden) return;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  }

  function syncToggle() {
    const city = state.view === 'city';
    const label = city ? 'Show as grid' : 'Show as 3D city';
    dom.toggle.setAttribute('aria-pressed', city ? 'true' : 'false');
    dom.toggle.setAttribute('aria-label', label);
    dom.toggle.title = label;
  }

  function setView(view, options) {
    const opts = options || {};
    if (view === state.view && !opts.force) return;
    if (view === 'city') {
      // Measure while the grid is still on screen so the tray keeps its height.
      if (state.view !== 'city') {
        sizeToGrid();
        dismissGridDetail();
      }
      state.view = 'city';
      dom.tray.classList.add('is-city');
      dom.skyline.hidden = false;
      syncToggle();
      showStage();
      if (opts.persist) saveView('city');
      riseIn();
    } else {
      state.view = 'grid';
      dom.tray.classList.remove('is-city');
      dom.skyline.hidden = true;
      syncToggle();
      setHover(-1);
      state.selected = -1;
      hideTooltip();
      state.inertia = 0;
      state.yawAnim = null;
      if (rafId) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
      }
      if (opts.persist) saveView('grid');
    }
  }

  /** The empty message only appears once data has arrived; until then the stage is simply blank. */
  function syncEmptyState() {
    dom.stage.classList.toggle('is-empty', state.loaded && state.empty);
    dom.legend.hidden = !state.loaded || state.empty;
  }

  function showStage() {
    syncEmptyState();
    syncCanvasSize();
  }

  /** Towers rise from the plate, a wave across the calendar. */
  function riseIn() {
    if (state.empty || !state.towers.length) return;
    applyTargets('rise');
    setGhost(state.focus ? 1 : 0, false);
    if (syncCanvasSize()) {
      draw();
      scheduleFrame();
    }
  }

  function onToggle() {
    setView(state.view === 'city' ? 'grid' : 'city', { persist: true });
  }

  /* ------------------------------------------------------------------ DOM */

  function buildDom() {
    const legend = document.createElement('div');
    legend.className = 'skyline__legend';
    legend.innerHTML = `<span class="skyline__note">Height shows tokens</span>
      <span class="skyline__ramp"><span>Cost</span><span>Less</span><span class="skyline__swatches" aria-hidden="true">${
  [1, 2, 3, 4, 5, 6].map((step) => `<span class="skyline__swatch" data-step="${step}"></span>`).join('')
}</span><span>More</span></span>`;

    const stage = document.createElement('div');
    stage.className = 'skyline__stage';
    const canvas = document.createElement('canvas');
    canvas.className = 'skyline__canvas';
    canvas.tabIndex = 0;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', '3D city of the last 30 days.');
    const empty = document.createElement('div');
    empty.className = 'skyline__empty';
    empty.textContent = EMPTY_TEXT;
    stage.appendChild(canvas);
    stage.appendChild(empty);

    dom.skyline.textContent = '';
    dom.skyline.appendChild(stage);
    dom.skyline.appendChild(legend);
    dom.stage = stage;
    dom.canvas = canvas;
    dom.legend = legend;
    ctx2d = canvas.getContext('2d');
  }

  function onResize() {
    if (state.view !== 'city') return;
    const width = dom.skyline.clientWidth;
    if (width !== observedWidth) {
      observedWidth = width;
      sizeToGrid();
    }
    if (syncCanvasSize()) {
      draw();
      scheduleFrame();
    }
  }

  function onThemeChange() {
    if (isVisible()) {
      draw();
      scheduleFrame();
    }
  }

  function init() {
    dom.tray = document.querySelector('.tray--heatmap');
    dom.host = document.getElementById('daily-usage-heatmap');
    dom.gridLegend = document.getElementById('heatmap-legend');
    dom.skyline = document.getElementById('skyline');
    dom.toggle = document.getElementById('skyline-toggle');
    if (!dom.tray || !dom.host || !dom.skyline || !dom.toggle) return;

    dom.skyline.classList.add('skyline');
    buildDom();

    dom.toggle.hidden = false;
    dom.toggle.addEventListener('click', onToggle);
    bindCanvas();

    document.addEventListener('themechange', onThemeChange);
    try {
      const scheme = window.matchMedia('(prefers-color-scheme: dark)');
      if (scheme.addEventListener) scheme.addEventListener('change', onThemeChange);
      else if (scheme.addListener) scheme.addListener(onThemeChange);
    } catch {
      /* matchMedia unavailable */
    }
    if (typeof ResizeObserver === 'function') {
      let pending = 0;
      const observer = new ResizeObserver(() => {
        if (pending) return;
        pending = window.requestAnimationFrame(() => {
          pending = 0;
          onResize();
        });
      });
      observer.observe(dom.skyline);
      observer.observe(dom.stage);
    }
    // Also covers device pixel ratio changes (browser zoom, moving between displays).
    window.addEventListener('resize', onResize);

    state.ready = true;
    state.view = 'grid';
    if (loadView() === 'city') {
      setView('city', { force: true });
    } else {
      syncToggle();
    }
  }

  /* ------------------------------------------------------------------ public API */

  function describe(cells) {
    if (!cells.length) return '3D city of the last 30 days.';
    const tallest = cells.reduce((best, cell) => (cell.tokens > best.tokens ? cell : best), cells[0]);
    return `3D city of the last ${cells.length} days. Tallest: ${fmtShortDay(tallest.date)}, ${fmtCompact(tallest.tokens)} tokens and ${fmtUsd(tallest.cost)} spent.`;
  }

  function update(data) {
    if (!state.ready) return;
    const cells = normalizeRows(data && data.heatmap_daily);
    const empty = !cells.length || !hasUsage(cells);
    const wasEmpty = state.empty;
    const structureKey = cells.map((cell) => cell.date).join(',');
    const structureChanged = structureKey !== state.structureKey;
    state.cells = cells;
    state.empty = empty;
    state.loaded = true;

    if (empty) {
      state.towers = [];
      state.structureKey = '';
      state.hover = -1;
      state.selected = -1;
      hideTooltip();
      dom.canvas.setAttribute('aria-label', '3D city of the last 30 days. No usage in this period.');
    } else {
      dom.canvas.setAttribute('aria-label', describe(cells));
      if (structureChanged || wasEmpty || !state.towers.length) {
        const fresh = wasEmpty || !state.towers.length;
        state.structureKey = structureKey;
        state.hover = -1;
        state.selected = -1;
        hideTooltip();
        layoutTowers();
        setGhost(state.focus ? 1 : 0, false);
        if (state.view === 'city') {
          // First appearance rises as a wave; a shifted calendar just grows in.
          if (fresh) {
            applyTargets('rise');
          } else {
            state.towers.forEach((tower) => {
              const to = tower.target;
              tower.h = 0;
              tower.g = 0;
              startTween(tower, to, REFRESH_MS, 0, easeRise);
            });
          }
        }
      } else {
        state.towers.forEach((tower, i) => { tower.cell = cells[i]; });
        applyTargets('refresh');
      }
    }

    if (state.view === 'city') {
      syncEmptyState();
      sizeToGrid();
      if (syncCanvasSize()) {
        draw();
        scheduleFrame();
      }
    }
  }

  function setFocus(cells) {
    if (!state.ready) return;
    state.focus = normalizeFocus(cells);
    if (!state.towers.length) return;
    applyTargets('focus');
    setGhost(state.focus ? 1 : 0, state.view === 'city', FOCUS_MS);
    if (isVisible()) {
      draw();
      scheduleFrame();
    }
  }

  window.DashboardSkyline = {
    update(data) {
      update(data);
    },
    setFocus(cells) {
      setFocus(cells);
    },
  };

  function boot() {
    try {
      init();
    } catch (error) {
      if (window.console && console.error) console.error('Skyline failed to initialise', error);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
