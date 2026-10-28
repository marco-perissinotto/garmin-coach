"""MCP server (stdio). Phase 1 scope: boot + startup check + get_sync_status. Full toolset arrives in Phase 3.

The server must start even with an empty cache, a missing key or Garmin down (PLAN §9.1): the startup check
only enqueues, and errors surface through get_sync_status.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import App
from ..status import build_status
from ..sync.startup import startup_check

log = logging.getLogger(__name__)


def build_server(app: App) -> MCPServer:
    mcp = MCPServer("garmin-coach")

    @mcp.tool()
    def get_sync_status() -> dict[str, Any]:
        """State of the local Garmin cache: whether history is loaded, age of each data tier vs its TTL,
        the running sync job with progress (calls_done/calls_planned) and ETA, recent jobs, cooldowns and last
        error. Use it to answer "is my data up to date / loaded?". Never contacts Garmin."""
        conn = app.connect()
        try:
            return build_status(conn, app.settings, app.clock)
        finally:
            conn.close()

    return mcp


def serve(app: App) -> None:
    """stdio only for now; the localhost HTTP mode with a bearer token is Phase 3 (PLAN §9)."""
    try:
        job = startup_check(app.jobs, app.settings, trigger="startup")
        if job:
            log.info("mcp: startup check enqueued job #%d", job)
    except Exception:  # noqa: BLE001 - never crash at boot
        log.exception("startup check failed")
    build_server(app).run(transport="stdio")
