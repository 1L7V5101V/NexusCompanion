#!/usr/bin/env python3
"""VitalCast MCP Server — Apple Watch health data bridge.

Single-process dual-protocol server:

  * **stdio JSON-RPC** (MCP) — connects to the Akashic Agent
  * **HTTP server** (thread, stdlib) — receives health data from VitalCast iOS app

Usage::

    python vitalcast_mcp.py
    python vitalcast_mcp.py --port 8321 --data-dir ~/.vitalcast

Register with the Akashic Agent::

    /mcp_add vitalcast python /path/to/vitalcast/vitalcast_mcp.py --port 8321
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from http.server import HTTPServer
from typing import Any

# MCP SDK uses asyncio internally.  The alternative (anyio) does not
# expose the stdin/stdout transport the SDK expects.
import asyncio  # noqa: ANYIO_OK

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool as MCPTool

from server.config import VitalCastConfig
from server.http_handler import make_handler
from server.storage import VitalsStore, AlertsStore

logger = logging.getLogger("vitalcast")

# ── MCP server factory ──────────────────────────────────────────


def _build_mcp_server(vs: VitalsStore, a_s: AlertsStore) -> Server:
    """Create the MCP server with tool handlers wired to the given stores."""

    server = Server("vitalcast")

    @server.list_tools()
    async def list_tools() -> list[MCPTool]:
        return [
            MCPTool(
                name="get_vitals",
                description="Get the latest health vitals snapshot from VitalCast",
                inputSchema={"type": "object", "properties": {}},
            ),
            MCPTool(
                name="get_alerts",
                description="Get pending health alerts (e.g. high heart rate), "
                "then clear them from the queue",
                inputSchema={"type": "object", "properties": {}},
            ),
            MCPTool(
                name="get_vital",
                description="Get a single vital metric by type",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Metric type: heartRate, steps, sleep, "
                            "bodyWeight, activeEnergy",
                        },
                    },
                    "required": ["type"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        match name:
            case "get_vitals":
                data = vs.get_all()
                return _json_result(data)

            case "get_alerts":
                alerts = a_s.pop_all()
                return _json_result(alerts)

            case "get_vital":
                metric_type = arguments.get("type", "")
                if not isinstance(metric_type, str) or not metric_type:
                    return _json_result({"error": "Missing 'type' argument"})
                entry = vs.get(metric_type)
                return _json_result(entry)

            case _:
                return _json_result({"error": f"Unknown tool: {name}"})

    return server


def _json_result(data: Any) -> list[TextContent]:
    """Wrap a JSON-serialisable value in a ``TextContent`` result list."""
    text = json.dumps(data, ensure_ascii=False, indent=2) if data is not None else "null"
    return [TextContent(type="text", text=text)]


# ── Logging ─────────────────────────────────────────────────────


def _setup_logging() -> None:
    """Configure structured logging to stderr (HTTP request logs go to stderr too)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


# ── HTTP server lifecycle ───────────────────────────────────────


def _start_http_server(
    config: VitalCastConfig,
    vs: VitalsStore,
    a_s: AlertsStore,
) -> HTTPServer:
    """Start the background HTTP server in a daemon thread and return the server ref."""
    handler_cls = make_handler(vs, a_s)
    httpd = HTTPServer((config.host, config.port), handler_cls)  # noqa: SOCKET_SERVER_OK
    http_thread = threading.Thread(
        target=httpd.serve_forever,
        daemon=True,
        name="vitalcast-http",
    )
    http_thread.start()
    logger.info("HTTP server listening on %s:%d", config.host, config.port)
    return httpd


# ── Main entry point ────────────────────────────────────────────


async def main() -> None:
    """Wire up storage, start the HTTP thread, and enter the MCP stdio loop."""
    _setup_logging()
    config = VitalCastConfig.from_env_and_args()
    config.data_dir.mkdir(parents=True, exist_ok=True)

    vs = VitalsStore(config.data_dir / "vitals.json")
    a_s = AlertsStore(config.data_dir / "alerts.json", config.max_alerts)

    httpd = _start_http_server(config, vs, a_s)
    mcp_server = _build_mcp_server(vs, a_s)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream=read_stream,
                write_stream=write_stream,
                initialization_options=mcp_server.create_initialization_options(),
            )
    finally:
        logger.info("Shutting down HTTP server...")
        httpd.shutdown()
        logger.info("VitalCast MCP server stopped.")


# ── Signal handling ────────────────────────────────────────────


def _signal_handler(sig: int, _frame: Any) -> None:  # noqa: BROAD_EXCEPT_OK
    """Handle termination signals by stopping the asyncio event loop."""
    logger.info("Received signal %d, initiating shutdown...", sig)
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.stop()


# ── Entry guard ─────────────────────────────────────────────────

if __name__ == "__main__":
    # Register termination signal handlers (best-effort; signal.signal
    # may be restricted in threaded contexts or on Windows).
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
