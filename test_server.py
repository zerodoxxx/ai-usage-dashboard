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
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
            if "application/json" in content_type:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


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
    status, html = fetch_url("/")
    assert status == 200, f"Expected 200, got {status}"
    assert "<!DOCTYPE html>" in html
    assert "AI Usage & Cost Visualizer" in html
    assert "odo-total-tokens" in html
    assert "chart-tokens" in html
    assert "models-table-body" in html
    print("✓ GET / (Dashboard HTML) passed")

    # 3. Test Static Assets
    for asset in ["/static/css/dashboard.css", "/static/js/odometer.js", "/static/js/dashboard.js"]:
        st, content = fetch_url(asset)
        assert st == 200, f"Failed to fetch {asset}: {st}"
        assert len(content) > 100
        print(f"✓ GET {asset} passed ({len(content)} bytes)")

    # 4. Test /api/pricing
    status, pricing = fetch_url("/api/pricing")
    assert status == 200, f"Expected 200, got {status}"
    assert "gpt-6-astra" in pricing
    assert "Gemini 3.8 Flash (High)" in pricing
    assert pricing["gpt-6-astra"]["uncached_input"] == 10.0
    print(f"✓ GET /api/pricing passed ({len(pricing)} models)")

    # 5. Test /api/usage?tool=all
    status, usage_all = fetch_url("/api/usage?tool=all")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_all["tool"] == "all"
    assert "summary" in usage_all and "models" in usage_all and "timeline" in usage_all and "sessions" in usage_all
    s = usage_all["summary"]
    assert s["total_tokens"] > 0
    assert s["cost_cached_usd"] > 0
    print(f"✓ GET /api/usage?tool=all passed (Total tokens: {s['total_tokens']:,}, Cost: ${s['cost_cached_usd']:.4f})")

    # 6. Test /api/usage?tool=codex
    status, usage_codex = fetch_url("/api/usage?tool=codex")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_codex["tool"] == "codex"
    print(f"✓ GET /api/usage?tool=codex passed (Codex tokens: {usage_codex['summary']['total_tokens']:,})")

    # 7. Test /api/usage?tool=agy
    status, usage_agy = fetch_url("/api/usage?tool=agy")
    assert status == 200, f"Expected 200, got {status}"
    assert usage_agy["tool"] == "antigravity"
    print(f"✓ GET /api/usage?tool=agy passed (AGY tokens: {usage_agy['summary']['total_tokens']:,})")

    # 8. Test invalid tool
    status, err = fetch_url("/api/usage?tool=invalid_tool")
    assert status == 400, f"Expected 400, got {status}: {err}"
    print("✓ GET /api/usage with invalid tool properly rejected with HTTP 400")

    print("\n========================================")
    print("  ALL API & DASHBOARD TESTS PASSED!     ")
    print("========================================\n")


if __name__ == "__main__":
    main()
