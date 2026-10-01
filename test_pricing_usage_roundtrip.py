"""Regression coverage for returning usage and its matching pricing catalog together."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import app as app_module


def test_usage_response_contains_pricing_applied_during_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    active_catalog = {
        "gpt-6-sol": {"uncached_input": 2.0, "cached_input": 0.2, "output": 10.0},
        "__meta__": {"source": "litellm", "models": {}},
    }

    def refresh_pricing() -> None:
        events.append("refresh")

    def get_tool_usage(*args: object, **kwargs: object) -> dict[str, object]:
        events.append("aggregate")
        # Model discovery happens inside aggregation and updates pricing metadata.
        active_catalog["__meta__"]["models"] = {"gpt-6-sol": "discovered"}
        return {"tool": "all", "summary": {"cost_cached_usd": 2.0}}

    def active_pricing_payload(*, refresh: bool = True) -> dict[str, object]:
        events.append(("payload", refresh))
        return active_catalog

    monkeypatch.setattr(app_module, "refresh_pricing", refresh_pricing)
    monkeypatch.setattr(app_module, "get_tool_usage", get_tool_usage)
    monkeypatch.setattr(app_module, "active_pricing_payload", active_pricing_payload)

    response = app_module.api_usage(tool="all", time_range="all", start=None, end=None)

    assert events == ["refresh", "aggregate", ("payload", False)]
    assert response["pricing"] == active_catalog
    assert response["pricing"]["__meta__"]["models"] == {"gpt-6-sol": "discovered"}


NODE_DASHBOARD_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function usage(pricing, marker) {
  const data = {
    summary: {}, analytics: {}, models: [], sessions: [], timezone: 'UTC', marker,
  };
  if (pricing !== undefined) data.pricing = pricing;
  return data;
}

function makeHarness(requestUsage, pricingFallback) {
  const listeners = {};
  const button = {
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelector() { return null; },
  };
  const pricingUpdates = [];
  const renderedPricing = [];
  const renderedUsage = [];
  let fetchPricingCalls = 0;
  let ready;
  const document = {
    readyState: 'loading',
    documentElement: { getAttribute() { return null; }, setAttribute() {} },
    addEventListener(name, callback) {
      if (name === 'DOMContentLoaded') ready = callback;
    },
    getElementById(id) { return id === 'refresh-btn' ? button : null; },
    querySelectorAll() { return []; },
    dispatchEvent() {},
  };
  const window = {
    DashboardApi: {
      requestUsage,
      fetchPricing: async () => {
        fetchPricingCalls += 1;
        return pricingFallback;
      },
      updatePricingStatus(_badge, meta) { pricingUpdates.push(meta); },
    },
    DashboardUtils: {
      formatUsd: String,
      formatRate: String,
      formatInt: String,
      formatCompactSig: String,
      formatDateTime: () => 'date 12:34',
      showToast() {},
    },
    DashboardAnalytics: { updateAnalytics() {} },
    DashboardTables: {
      renderModelLedger() {},
      renderModelTable(_models, options) { renderedPricing.push(options.pricingData); },
      renderSessionsTable() {},
    },
    DashboardCharts: { updateCharts(_data, ctx) { renderedUsage.push(ctx.queryKey); } },
  };
  const sandbox = {
    window, document, console,
    Date, Promise, URLSearchParams,
    setInterval, clearInterval,
  };
  vm.runInNewContext(source, sandbox, { filename: 'dashboard.js' });
  return {
    async start() { ready(); await flush(); },
    clickRefresh() { listeners.click(); },
    pricingUpdates,
    renderedPricing,
    renderedUsage,
    get fetchPricingCalls() { return fetchPricingCalls; },
  };
}

async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

(async () => {
  const latestPricing = {
    'gpt-6-sol': { uncached_input: 2 },
    __meta__: { source: 'litellm', revision: 'latest' },
  };
  const obsoletePricing = {
    'gpt-6-sol': { uncached_input: 99 },
    __meta__: { source: 'litellm', revision: 'obsolete' },
  };
  const first = deferred();
  let requests = 0;
  const race = makeHarness(async () => {
    requests += 1;
    return requests === 1
      ? first.promise
      : { status: 'ok', data: usage(latestPricing, 'latest') };
  }, null);
  await race.start();
  race.clickRefresh();
  await flush();
  first.resolve({ status: 'ok', data: usage(obsoletePricing, 'obsolete') });
  await flush();

  const fallbackPricing = {
    'gpt-6-sol': { uncached_input: 2 },
    __meta__: { source: 'litellm', revision: 'fallback' },
  };
  const fallback = makeHarness(
    async () => ({ status: 'ok', data: usage(undefined, 'legacy-server') }),
    fallbackPricing,
  );
  await fallback.start();

  process.stdout.write(JSON.stringify({
    raceRequestCount: requests,
    racePricingRevisions: race.pricingUpdates.map((meta) => meta.revision),
    raceRenderedRates: race.renderedPricing.map((pricing) => pricing && pricing['gpt-6-sol'].uncached_input),
    racePricingFetches: race.fetchPricingCalls,
    legacyPricingRevisions: fallback.pricingUpdates.map((meta) => meta.revision),
    legacyRenderedRates: fallback.renderedPricing.map((pricing) => pricing && pricing['gpt-6-sol'].uncached_input),
    legacyPricingFetches: fallback.fetchPricingCalls,
  }));
})();
"""


def test_dashboard_uses_embedded_pricing_with_generation_protection_and_legacy_fallback() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the dashboard integration harness")

    dashboard_path = Path(__file__).resolve().parent / "src" / "static" / "js" / "dashboard.js"
    result = subprocess.run(
        [node, "-e", NODE_DASHBOARD_HARNESS, str(dashboard_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)

    assert observed["raceRequestCount"] == 2
    assert observed["racePricingRevisions"] == ["latest"]
    assert observed["raceRenderedRates"] == [2]
    assert observed["racePricingFetches"] == 0
    assert observed["legacyPricingRevisions"] == ["fallback"]
    assert observed["legacyRenderedRates"] == [2]
    assert observed["legacyPricingFetches"] == 1
