/**
 * Milestone celebrations for the register.
 *
 * `update(data, ctx)` runs after every render. When a live refresh of the same
 * query pushes the total cost (or total tokens) across a round threshold, the
 * register celebrates and a toast announces it. Boots and query changes never
 * celebrate; they only reset the baseline.
 *
 * Thresholds follow a 1-2.5-5 series: cost from $10 (10, 25, 50, 100, ...),
 * tokens from 10M (10M, 25M, 50M, 100M, ...). If several are crossed in one
 * refresh the highest wins, and cost beats tokens.
 */
(function () {
  'use strict';

  const COST_BASE = 10;
  const TOKEN_BASE = 1e7;
  const SERIES = [1, 2.5, 5];
  const CELEBRATE_MS = 1200;
  const MAX_DECADES = 40;

  const previous = { queryKey: null, cost: 0, tokens: 0, seen: false };
  const timers = { cost: 0, tokens: 0 };

  function prefersReducedMotion() {
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function finiteNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  /**
   * Highest threshold of the 1-2.5-5 series (from `base`) with prev < t <= cur,
   * or null when none was crossed.
   */
  function highestCrossed(prev, cur, base) {
    if (!(cur > prev)) return null;
    let found = null;
    for (let decade = 0; decade < MAX_DECADES; decade++) {
      const scale = base * Math.pow(10, decade);
      for (let i = 0; i < SERIES.length; i++) {
        const threshold = scale * SERIES[i];
        if (threshold > cur) return found;
        if (threshold > prev) found = threshold;
      }
    }
    return found;
  }

  function formatCost(threshold) {
    return `$${threshold.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  }

  function formatTokens(threshold) {
    const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M']];
    for (let i = 0; i < units.length; i++) {
      const [size, suffix] = units[i];
      if (threshold >= size) {
        const scaled = Math.round((threshold / size) * 100) / 100;
        return `${scaled}${suffix}`;
      }
    }
    return threshold.toLocaleString('en-US');
  }

  /** Restart a one-shot class so back-to-back celebrations replay. */
  function pulseClass(el, kind) {
    window.clearTimeout(timers[kind]);
    el.classList.remove('is-celebrating');
    void el.offsetWidth;
    el.classList.add('is-celebrating');
    timers[kind] = window.setTimeout(() => el.classList.remove('is-celebrating'), CELEBRATE_MS);
  }

  function toast(message) {
    const utils = window.DashboardUtils;
    if (utils && typeof utils.showToast === 'function') utils.showToast(message);
  }

  /**
   * Celebrate a milestone: the visual (skipped under reduced motion) plus the toast.
   * @param {'cost'|'tokens'} kind
   * @param {number} threshold - dollars for 'cost', tokens for 'tokens'
   */
  function celebrate(kind, threshold) {
    const value = finiteNumber(threshold);
    if (kind === 'cost') {
      if (!prefersReducedMotion()) {
        const register = document.getElementById('odo-total-cost');
        if (register) pulseClass(register, 'cost');
      }
      toast(`You passed ${formatCost(value)} of API value`);
    } else if (kind === 'tokens') {
      if (!prefersReducedMotion()) {
        const readout = document.getElementById('readout-tokens');
        if (readout) pulseClass(readout, 'tokens');
      }
      toast(`You passed ${formatTokens(value)} tokens`);
    }
  }

  /**
   * @param {Object} data - the usage payload
   * @param {{boot: boolean, queryKey: string}} ctx
   */
  function update(data, ctx) {
    const summary = data && typeof data === 'object' && data.summary && typeof data.summary === 'object'
      ? data.summary
      : {};
    // Same reading the register shows: cached cost, rounded to cents.
    const cost = Math.round(finiteNumber(summary.cost_cached_usd) * 100) / 100;
    const tokens = finiteNumber(summary.total_tokens);
    const queryKey = ctx ? ctx.queryKey : null;
    const boot = !!(ctx && ctx.boot);

    const comparable = previous.seen && !boot && queryKey === previous.queryKey;
    const prevCost = previous.cost;
    const prevTokens = previous.tokens;

    previous.queryKey = queryKey;
    previous.cost = cost;
    previous.tokens = tokens;
    previous.seen = true;

    if (!comparable) return;

    const costThreshold = highestCrossed(prevCost, cost, COST_BASE);
    if (costThreshold !== null) {
      celebrate('cost', costThreshold);
      return;
    }
    const tokenThreshold = highestCrossed(prevTokens, tokens, TOKEN_BASE);
    if (tokenThreshold !== null) celebrate('tokens', tokenThreshold);
  }

  window.DashboardMilestones = { update, celebrate };
})();
