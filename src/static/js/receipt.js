(function () {
  'use strict';

  const MAX_MODELS = 6;
  const state = {
    initialized: false,
    queryKey: null,
    bill: null,
    timers: new Set(),
    swaps: new Map(),
    printing: false,
  };

  function ensureSection() {
    let receipt = document.getElementById('receipt');
    if (!receipt) return null;
    if (receipt.tagName.toLowerCase() !== 'section') {
      const section = document.createElement('section');
      section.id = 'receipt';
      section.className = 'receipt';
      receipt.replaceWith(section);
      receipt = section;
    }
    receipt.setAttribute('aria-labelledby', 'receipt-heading');
    if (!receipt.querySelector('.receipt__paper')) {
      receipt.innerHTML = `
        <div class="receipt__slot" aria-hidden="true"></div>
        <div class="receipt__paper">
          <h3 class="receipt__heading" id="receipt-heading">Top models by cost</h3>
          <div class="receipt__rule" aria-hidden="true"></div>
          <ol class="receipt__lines"></ol>
          <div class="receipt__summary"></div>
          <p class="receipt__empty" hidden>No usage in this period</p>
          <div class="receipt__tear" aria-hidden="true"></div>
        </div>
      `;
    }
    return receipt;
  }

  // Keep numeric parsing, cost/rate fields and sorting aligned with renderModelLedger.
  function finiteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function buildBill(data) {
    const utils = window.DashboardUtils;
    const models = (Array.isArray(data.models) ? data.models : [])
      .filter((m) => m && typeof m === 'object')
      .map((m) => {
        const tokens = Math.max(0, finiteNumber(m.total_tokens) ?? 0);
        const knownCost = finiteNumber(m.est_cost_cached_usd);
        const cost = knownCost ?? 0;
        const unpriced = m.unpriced === true || m.priced === false || m.cost_available === false;
        const rate = !unpriced && tokens > 0 && knownCost !== null ? (cost / tokens) * 1e6 : null;
        const tool = (m.tool && m.tool !== 'all' ? m.tool : '') || m.provider || '';
        return {
          key: String(m.key ?? JSON.stringify([m.provider, m.model, m.tool])),
          name: String(m.model || 'unknown'),
          tool,
          tokens,
          cost,
          unpriced,
          estimated: utils.isEstimatedRow(m),
          rate: finiteNumber(rate),
          calls: finiteNumber(m.call_count) ?? 0,
        };
      })
      .filter((row) => row.tokens > 0 || row.calls > 0 || row.cost > 0);
    models.sort((a, b) => {
      if (a.unpriced !== b.unpriced) return a.unpriced ? 1 : -1;
      return a.unpriced ? b.tokens - a.tokens : b.cost - a.cost;
    });
    return {
      items: models.slice(0, MAX_MODELS),
      otherCount: Math.max(0, models.length - MAX_MODELS),
      otherCost: models.slice(MAX_MODELS).reduce((sum, row) => sum + row.cost, 0),
      // This is the exact input updateMeter passes to the register.
      total: finiteNumber(data.summary?.cost_cached_usd) ?? 0,
    };
  }

  function costLabel(row) {
    if (row.unpriced) return '–';
    return `${row.estimated ? '~' : ''}${window.DashboardUtils.formatUsd(row.cost, 2)}`;
  }

  function detailLabel(row) {
    const utils = window.DashboardUtils;
    const tokens = `${utils.formatCompactSig(row.tokens)} tok`;
    return row.rate === null ? tokens : `${tokens} @ ${utils.formatRate(row.rate)}/1M`;
  }

  function itemTitle(row) {
    const cost = row.unpriced ? 'Cost unavailable' : `${row.estimated ? '~' : ''}$${row.cost}`;
    return `${row.name}, ${window.DashboardUtils.toolLabel(row.tool)}. ${row.tokens.toLocaleString('en-US', { maximumFractionDigits: 20 })} tokens. ${cost}.`;
  }

  function makeLine(row) {
    const line = document.createElement('li');
    line.className = 'receipt__line';
    line.dataset.modelKey = row.key;
    line.title = itemTitle(row);
    const content = document.createElement('div');
    content.className = 'receipt__line-content';
    const dotHolder = document.createElement('span');
    dotHolder.innerHTML = window.DashboardUtils.toolDotHtml(row.tool);
    const dot = dotHolder.firstElementChild;
    dot.classList.add('receipt__dot');
    const name = document.createElement('span');
    name.className = 'receipt__model';
    name.textContent = row.name;
    const cost = document.createElement('span');
    cost.className = 'receipt__cost';
    cost.textContent = costLabel(row);
    const detail = document.createElement('div');
    detail.className = 'receipt__detail';
    detail.textContent = detailLabel(row);
    content.append(dot, name, cost);
    line.append(content, detail);
    return line;
  }

  function makeSummaryRow(label, amount, className) {
    const row = document.createElement('div');
    row.className = `receipt__summary-row ${className}`;
    const name = document.createElement('span');
    name.textContent = label;
    const cost = document.createElement('span');
    cost.className = 'receipt__cost';
    cost.textContent = window.DashboardUtils.formatUsd(amount, 2);
    row.append(name, cost);
    return row;
  }

  function render(receipt, bill) {
    const list = receipt.querySelector('.receipt__lines');
    const summary = receipt.querySelector('.receipt__summary');
    summary.className = 'receipt__summary';
    const empty = bill.items.length === 0;
    receipt.classList.toggle('receipt--empty', empty);
    receipt.querySelector('.receipt__empty').hidden = !empty;
    list.replaceChildren(...bill.items.map(makeLine));
    summary.replaceChildren();
    summary.hidden = empty;
    if (empty) return;
    const rule = document.createElement('div');
    rule.className = 'receipt__rule';
    rule.setAttribute('aria-hidden', 'true');
    summary.append(rule);
    if (bill.otherCount > 0) {
      summary.append(makeSummaryRow(`${bill.otherCount} other models`, bill.otherCost, 'receipt__other'));
    }
    summary.append(makeSummaryRow('Total', bill.total, 'receipt__total'));
  }

  function later(callback, delay) {
    const timer = window.setTimeout(() => {
      state.timers.delete(timer);
      callback();
    }, delay);
    state.timers.add(timer);
    return timer;
  }

  function clearTimers() {
    state.timers.forEach((timer) => window.clearTimeout(timer));
    state.timers.clear();
    state.swaps.clear();
    state.printing = false;
  }

  function printLine(line) {
    line.classList.remove('receipt__line--pending');
    line.classList.add('receipt__line--feed', 'receipt__line--enter');
    later(() => line.classList.remove('receipt__line--feed', 'receipt__line--enter'), 220);
  }

  function reprint(receipt, bill, boot) {
    render(receipt, bill);
    if (!bill.items.length) return;
    state.printing = true;
    const items = Array.from(receipt.querySelector('.receipt__lines').children);
    const summary = receipt.querySelector('.receipt__summary');
    const summaryRows = Array.from(summary.querySelectorAll('.receipt__summary-row'));
    const lines = boot ? items : [...items, ...summaryRows];
    lines.forEach((line) => line.classList.add('receipt__line--pending'));
    summary.querySelector('.receipt__rule').classList.add('receipt__line--pending');
    if (boot) summary.classList.add('receipt__line--pending');
    lines.forEach((line, index) => {
      later(() => {
        printLine(line);
        if (boot && index === items.length - 1) {
          summary.querySelector('.receipt__rule').classList.remove('receipt__line--pending');
          printLine(summary);
        } else if (!boot && index === items.length) {
          summary.querySelector('.receipt__rule').classList.remove('receipt__line--pending');
        }
        if (index === lines.length - 1) state.printing = false;
      }, (boot ? 900 : 0) + index * (boot ? 80 : 45));
    });
  }

  function swapAmount(row, label) {
    const amount = row.querySelector('.receipt__cost');
    const previous = state.swaps.get(amount);
    if (previous) previous.forEach((timer) => {
      window.clearTimeout(timer);
      state.timers.delete(timer);
    });
    amount.classList.remove('receipt__cost--out', 'receipt__cost--in');
    row.classList.remove('receipt__line--changed');
    // Restart ink emphasis if another value arrives during an existing swap.
    row.getBoundingClientRect();
    amount.classList.add('receipt__cost--out');
    row.classList.add('receipt__line--changed');
    const timers = [];
    timers.push(later(() => {
      amount.textContent = label;
      amount.classList.remove('receipt__cost--out');
      amount.classList.add('receipt__cost--in');
      timers.push(later(() => amount.classList.remove('receipt__cost--in'), 120));
    }, 80));
    timers.push(later(() => {
      row.classList.remove('receipt__line--changed');
      state.swaps.delete(amount);
    }, 1200));
    state.swaps.set(amount, timers);
  }

  function refresh(receipt, bill, previous) {
    const lines = Array.from(receipt.querySelector('.receipt__lines').children);
    bill.items.forEach((row, index) => {
      const line = lines[index];
      const old = previous.items[index];
      const title = itemTitle(row);
      if (line.title !== title) line.title = title;
      const name = line.querySelector('.receipt__model');
      if (name.textContent !== row.name) name.textContent = row.name;
      const detail = line.querySelector('.receipt__detail');
      const label = detailLabel(row);
      if (detail.textContent !== label) detail.textContent = label;
      if (row.tool !== old.tool) {
        const holder = document.createElement('span');
        holder.innerHTML = window.DashboardUtils.toolDotHtml(row.tool);
        holder.firstElementChild.classList.add('receipt__dot');
        line.querySelector('.receipt__dot').replaceWith(holder.firstElementChild);
      }
      if (row.cost !== old.cost || row.unpriced !== old.unpriced || row.estimated !== old.estimated) {
        if (state.printing) line.querySelector('.receipt__cost').textContent = costLabel(row);
        else swapAmount(line, costLabel(row));
      }
    });
    const summary = receipt.querySelector('.receipt__summary');
    let other = summary.querySelector('.receipt__other');
    if (bill.otherCount === 0) {
      if (other) other.remove();
    } else if (!other) {
      other = makeSummaryRow(`${bill.otherCount} other models`, bill.otherCost, 'receipt__other');
      summary.insertBefore(other, summary.querySelector('.receipt__total'));
    } else {
      if (bill.otherCount !== previous.otherCount) {
        other.firstElementChild.textContent = `${bill.otherCount} other models`;
      }
      if (bill.otherCost !== previous.otherCost || bill.otherCount !== previous.otherCount) {
        if (state.printing) other.querySelector('.receipt__cost').textContent = window.DashboardUtils.formatUsd(bill.otherCost);
        else swapAmount(other, window.DashboardUtils.formatUsd(bill.otherCost));
      }
    }
    if (bill.total !== previous.total && bill.items.length) {
      const total = summary.querySelector('.receipt__total');
      if (state.printing) total.querySelector('.receipt__cost').textContent = window.DashboardUtils.formatUsd(bill.total);
      else swapAmount(total, window.DashboardUtils.formatUsd(bill.total));
    }
  }

  function update(data, ctx = {}) {
    const receipt = ensureSection();
    if (!receipt) return;
    receipt.hidden = false;
    const bill = buildBill(data || {});
    const queryKey = ctx.queryKey ?? JSON.stringify([ctx.tool || 'all', ctx.timeRange || 'all']);
    const reducedMotion = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const first = !state.initialized;
    const rankingChanged = !first && (bill.items.length !== state.bill.items.length
      || bill.items.some((row, index) => row.key !== state.bill.items[index].key));
    if (reducedMotion || first || queryKey !== state.queryKey || rankingChanged) {
      clearTimers();
      if (reducedMotion || (first && !ctx.boot)) render(receipt, bill);
      else reprint(receipt, bill, first && ctx.boot);
    } else {
      refresh(receipt, bill, state.bill);
    }
    state.initialized = true;
    state.queryKey = queryKey;
    state.bill = bill;
  }

  window.DashboardReceipt = { update };
})();
