"""FastAPI application for AI Tools Usage & Cost Visualizer."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from src.parsers.aggregator import get_tool_usage
from src.pricing import active_pricing_payload, refresh_openai_pricing

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
_STATIC_ASSET_RE = re.compile(
    r'(?P<attr>src|href)="(?P<path>/static/[^"?#]+)(?:\?[^"]*)?"'
)
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

app = FastAPI(
    title="AI Tools Usage & Cost Visualizer",
    version="1.0.0",
    description="Real-time usage telemetry, token auditing, and cost analysis dashboard for Codex, AGY, and Claude Code.",
)

# Ensure static directory exists before mounting
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _static_asset_version(url_path: str) -> str:
    """Return a filesystem mtime token for a /static/... URL."""
    relative = url_path.lstrip("/")
    candidate = (BASE_DIR / relative).resolve()
    try:
        candidate.relative_to(BASE_DIR.resolve())
    except ValueError:
        return "0"
    try:
        return str(candidate.stat().st_mtime_ns)
    except OSError:
        return "0"


def version_static_assets(html: str) -> str:
    """Pin local CSS/JS URLs to file mtime so browsers cannot keep a stale charts.js."""

    def replace(match: re.Match[str]) -> str:
        path = match.group("path")
        return f'{match.group("attr")}="{path}?v={_static_asset_version(path)}"'

    return _STATIC_ASSET_RE.sub(replace, html)


@app.middleware("http")
async def disable_browser_cache(request: Request, call_next) -> Response:
    """Never let the browser reuse HTML, JS, or API payloads across loads."""
    response = await call_next(request)
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value
    return response


@app.get("/", response_class=HTMLResponse)
def read_index() -> HTMLResponse:
    """Serve the primary single-page dashboard."""
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard index.html not found.")
    html = version_static_assets(index_file.read_text(encoding="utf-8"))
    return HTMLResponse(html, headers=dict(NO_STORE_HEADERS))


@app.get("/api/usage")
def api_usage(
    tool: str = Query(
        default="all",
        description="Filter metrics by tool: 'all', 'codex', 'agy', 'antigravity', or 'claude-code'.",
    ),
    time_range: str = Query(
        default="all",
        description="Filter usage by time: 'all', 'month', '30d', '7d', '24h', or 'custom' (requires start; end is optional, YYYY-MM-DD).",
    ),
    start: str | None = Query(
        default=None,
        description="Inclusive custom-range start date (YYYY-MM-DD, UTC). Required when time_range=custom.",
    ),
    end: str | None = Query(
        default=None,
        description="Inclusive custom-range end date (YYYY-MM-DD, UTC). Optional; defaults to the current instant when omitted.",
    ),
) -> dict[str, Any]:
    """Return real-time usage metrics, summaries, model breakdowns, timelines, and sessions."""
    try:
        # Refresh before parsing so provider adapters and their snapshots use
        # the same active rates as the pricing endpoint.
        refresh_openai_pricing()
        return get_tool_usage(tool, time_range=time_range, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error aggregating usage metrics: {e}")


@app.get("/api/pricing")
def api_pricing() -> dict[str, Any]:
    """Return active model rates ($/1M tokens) and provenance metadata."""
    return active_pricing_payload()


@app.get("/api/health")
def api_health() -> dict[str, str]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
