#!/usr/bin/env python3
"""Entry point CLI for launching the AI Tools Usage & Cost Visualizer."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

# Ensure project root is in Python sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn


def print_banner(host: str, port: int, reload: bool, auto_open: bool, display_host: str | None = None) -> None:
    """Print an attractive, informative startup banner to the console."""
    target_host = display_host or ("127.0.0.1" if host in ("0.0.0.0", "::") else host)
    url = f"http://{target_host}:{port}"
    cyan = "\033[96m"
    green = "\033[92m"
    yellow = "\033[93m"
    magenta = "\033[95m"
    bold = "\033[1m"
    dim = "\033[2m"
    reset = "\033[0m"

    banner = f"""
{cyan}{bold}╔═══════════════════════════════════════════════════════════════════╗
║                 AI Tools Usage & Cost Visualizer                  ║
║               Real-Time Codex & AGY Telemetry Server              ║
╚═══════════════════════════════════════════════════════════════════╝{reset}
  {green}●{reset} {bold}Dashboard UI:{reset}    {magenta}{bold}{url}{reset}
  {dim}├─ Host:{reset}          {host}
  {dim}├─ Port:{reset}          {port}
  {dim}├─ Hot Reload:{reset}    {green if reload else yellow}{'Enabled (uvicorn watcher)' if reload else 'Disabled'}{reset}
  {dim}└─ Auto-browser:{reset}  {'Enabled' if auto_open else 'Disabled'}

  {dim}Press Ctrl+C to stop the server.{reset}
"""
    print(banner)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Launch the AI Tools Usage & Cost Visualizer dashboard server."
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host IP interface to bind server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port number to listen on (default: 8765)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Automatically open dashboard in default web browser upon launch",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable uvicorn auto-reload",
    )
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error(f"Port must be between 1 and 65535 (received: {args.port})")
    return args


def main() -> None:
    """Run the server."""
    args = parse_args()
    reload_enabled = not args.no_reload
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    dashboard_url = f"http://{display_host}:{args.port}"

    if args.open:
        def _open_tab() -> None:
            try:
                webbrowser.open(dashboard_url)
            except Exception as exc:
                print(f"Warning: Failed to launch browser: {exc}")

        timer = threading.Timer(0.8, _open_tab)
        timer.daemon = True
        timer.start()

    print_banner(
        host=args.host,
        port=args.port,
        reload=reload_enabled,
        auto_open=args.open,
        display_host=display_host,
    )

    uvicorn.run(
        "src.app:app",
        host=args.host,
        port=args.port,
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
