"""FastAPI application for AI Tools Usage & Cost Visualizer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.parsers.aggregator import get_tool_usage
from src.pricing import MODEL_PRICING

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(
    title="AI Tools Usage & Cost Visualizer",
    version="1.0.0",
    description="Real-time usage telemetry, token auditing, and cost analysis dashboard for Codex and AGY.",
)

# Ensure static directory exists before mounting
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=FileResponse)
def read_index() -> FileResponse:
    """Serve the primary single-page dashboard."""
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard index.html not found.")
    return FileResponse(index_file)


@app.get("/api/usage")
def api_usage(
    tool: str = Query(
        default="all",
        description="Filter metrics by tool: 'all', 'codex', 'agy', or 'antigravity'.",
    ),
    time_range: str = Query(
        default="all",
        description="Filter usage by time: 'all', 'month', '30d', '7d', or '24h'.",
    ),
) -> dict[str, Any]:
    """Return real-time usage metrics, summaries, model breakdowns, timelines, and sessions."""
    try:
        return get_tool_usage(tool, time_range=time_range)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error aggregating usage metrics: {e}")


@app.get("/api/pricing")
def api_pricing() -> dict[str, dict[str, float]]:
    """Return reference model pricing rates ($/1M tokens)."""
    return MODEL_PRICING


@app.get("/api/health")
def api_health() -> dict[str, str]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
