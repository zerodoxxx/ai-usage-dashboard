#!/usr/bin/env python3
"""Automated endpoint tests for the AI Tools Usage & Cost Visualizer."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
import uvicorn

from src.app import NO_STORE_HEADERS, _static_asset_version, version_static_assets


def test_version_static_assets_pins_mtime_and_replaces_stale_query() -> None:
    html = (
        '<script src="/static/js/charts.js"></script>'
        '<link rel="stylesheet" href="/static/css/dashboard.css?v=old">'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'
    )
    charts_v = _static_asset_version("/static/js/charts.js")
    css_v = _static_asset_version("/static/css/dashboard.css")
    out = version_static_assets(html)
    assert f'/static/js/charts.js?v={charts_v}' in out
    assert f'/static/css/dashboard.css?v={css_v}' in out
    assert "v=old" not in out
    assert 'src="https://cdn.jsdelivr.net/npm/chart.js"' in out


def test_index_html_cache_busts_charts_js() -> None:
    html = (Path(__file__).resolve().parent / "src" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    out = version_static_assets(html)
    charts_v = _static_asset_version("/static/js/charts.js")
    assert "Cost per 1M Tokens by Model" in out
    assert f"/static/js/charts.js?v={charts_v}" in out
    assert "Cost / 1K Tokens" not in out


def test_no_store_headers_are_complete() -> None:
    assert "no-store" in NO_STORE_HEADERS["Cache-Control"]
    assert NO_STORE_HEADERS["Pragma"] == "no-cache"
    assert NO_STORE_HEADERS["Expires"] == "0"


TEST_HOST = "127.0.0.1"
TEST_PORT = 8799
BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"


def run_server() -> None:
    config = uvicorn.Config(
        "src.app:app",
        host=TEST_HOST,
        port=TEST_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run()


def fetch_url(path: str) -> tuple[int, dict | str]:
    status, body, _headers = fetch_url_with_headers(path)
    return status, body


def fetch_url_with_headers(path: str) -> tuple[int, dict | str, dict[str, str]]:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            headers = {str(key).lower(): str(value) for key, value in resp.headers.items()}
            content_type = headers.get("content-type", "")
            raw = resp.read().decode("utf-8")
            if "application/json" in content_type:
                return resp.status, json.loads(raw), headers
            return resp.status, raw, headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        headers = {str(key).lower(): str(value) for key, value in e.headers.items()} if e.headers else {}
        try:
            return e.code, json.loads(raw), headers
        except Exception:
            return e.code, raw, headers


def main() -> None:
    print("\n--- Starting background test server ---")
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Wait for server to bind
    for _ in range(30):
        time.sleep(0.2)
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
    else:
        raise RuntimeError("Server failed to start within timeout.")

    print(f"✓ Test server active on {BASE_URL}")

    # 1. Test /api/health
    status, data = fetch_url("/api/health")
    assert status == 200, f"Expected 200, got {status}"
    assert data["status"] == "ok", f"Expected ok status, got {data}"
    assert "timestamp" in data, "Missing timestamp in health response"
    print("✓ GET /api/health passed")

    # 2. Test / (Dashboard HTML)
    status, html, headers = fetch_url_with_headers("/")
    assert status == 200, f"Expected 200, got {status}"
    assert "<!DOCTYPE html>" in html
    assert "AI Usage & Cost Visualizer" in html
    assert "odo-total-tokens" in html
    assert "chart-tokens" in html
    assert "models-table-body" in html
    assert "time-range-select" in html
    assert "Projected 30 Day Cost" in html
    assert "Projected Monthly Cost" not in html
    assert "/static/js/charts.js?v=" in html
    assert "no-store" in headers.get("cache-control", "")
    print("✓ GET / (Dashboard HTML) passed")

    # 3. Test Static Assets
    for asset in ["/static/css/dashboard.css", "/static/js/odometer.js", "/static/js/dashboard.js"]:
        st, content, headers = fetch_url_with_headers(asset)
        assert st == 200, f"Failed to fetch {asset}: {st}"
        assert len(content) > 100
        assert "no-store" in headers.get("cache-control", "")
        print(f"✓ GET {asset} passed ({len(content)} bytes)")

    # 4. Test /api/pricing
    status, pricing, headers = fetch_url_with_headers("/api/pricing")
    assert status == 200, f"Expected 200, got {status}"
    assert "gpt-6-astra" in pricing
    assert "Gemini 3.8 Flash (High)" in pricing
    assert pricing["gpt-6-astra"]["uncached_input"] == 10.0
    assert "no-store" in headers.get("cache-control", "")
    print(f"✓ GET /api/pricing passed ({len(pricing)} models)")

    # 5. Test /api/usage?tool=all
    status, usage_all, headers = fetch_url_with_headers("/api/usage?tool=all")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_all["tool"] == "all"
    assert "no-store" in headers.get("cache-control", "")
    assert "summary" in usage_all and "models" in usage_all and "timeline" in usage_all and "sessions" in usage_all
    assert "hourly_timeline" in usage_all and "weekday_hour" in usage_all
    assert len(usage_all["hourly_timeline"]) == 24
    assert len(usage_all["weekday_hour"]) == 168
    assert "analytics" in usage_all
    assert {
        "top_sessions",
        "daily_calls",
        "projected_30d_usd",
        "monthly_projection_usd",
        "comparison",
    }.issubset(usage_all["analytics"])
    assert usage_all["analytics"]["projected_30d_usd"] == usage_all["analytics"]["monthly_projection_usd"]
    assert all("usage_events" not in session for session in usage_all["sessions"] if isinstance(session, dict))
    s = usage_all["summary"]
    assert s["total_tokens"] > 0
    assert s["cost_cached_usd"] > 0
    print(f"✓ GET /api/usage?tool=all passed (Total tokens: {s['total_tokens']:,}, Cost: ${s['cost_cached_usd']:.4f})")

    # 6. Test /api/usage?tool=codex
    status, usage_codex = fetch_url("/api/usage?tool=codex")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_codex["tool"] == "codex"
    print(f"✓ GET /api/usage?tool=codex passed (Codex tokens: {usage_codex['summary']['total_tokens']:,})")

    # 7. Test time-range filtering
    status, usage_7d = fetch_url("/api/usage?tool=all&time_range=7d")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_7d["time_range"] == "7d"
    assert usage_7d["summary"]["session_count"] <= usage_all["summary"]["session_count"]
    assert usage_7d["analytics"]["comparison"]["label"] == "previous 7 days"
    print(f"✓ GET /api/usage?time_range=7d passed (Sessions: {usage_7d['summary']['session_count']})")

    # 8. Test /api/usage?tool=agy
    status, usage_agy = fetch_url("/api/usage?tool=agy")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_agy["tool"] == "antigravity"
    print(f"✓ GET /api/usage?tool=agy passed (AGY tokens: {usage_agy['summary']['total_tokens']:,})")

    # 9. Test invalid tool
    status, err = fetch_url("/api/usage?tool=invalid_tool")
    assert status == 400, f"Expected 400, got {status}: {err}"
    print("✓ GET /api/usage with invalid tool properly rejected with HTTP 400")

    # 10. Test invalid time range
    status, err = fetch_url("/api/usage?time_range=invalid_range")
    assert status == 400, f"Expected 400, got {status}: {err}"
    print("✓ GET /api/usage with invalid time range properly rejected with HTTP 400")

    print("\n========================================")
    print("  ALL API & DASHBOARD TESTS PASSED!     ")
    print("========================================\n")


if __name__ == "__main__":
    main()
