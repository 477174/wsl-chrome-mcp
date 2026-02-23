"""WSL Chrome MCP Server - Chrome DevTools for coding agents in WSL.

Each session gets its own Chrome instance for complete isolation,
with persistent WebSocket connections for real-time event handling.

Tool naming follows ChromeDevTools conventions (navigate_page, click, fill, etc.)
with backwards-compatible aliases for old names (chrome_navigate, chrome_click, etc.)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)

from .chrome_pool import ChromeInstance, ChromePoolManager
from .config import load_config
from .logging_config import setup_logging
from .tools import get_all_tools, get_tool
from .tools.base import ContentResult
from .wsl import get_windows_host_ip, is_wsl

# Configure logging
setup_logging()
logger = logging.getLogger("wsl-chrome-mcp")

# session_id property shared across all tool schemas
SESSION_ID_PROPERTY = {
    "session_id": {
        "type": "string",
        "description": (
            "Session identifier for multi-session isolation (auto-injected by opencode plugin)"
        ),
    },
}

# Map old tool names to new names for backwards compatibility
TOOL_ALIASES = {
    "chrome_navigate": "navigate_page",
    "chrome_screenshot": "take_screenshot",
    "chrome_click": "click",
    "chrome_type": "fill",
    "chrome_get_html": "get_html",
    "chrome_evaluate": "evaluate",
    "chrome_console": "get_console",
    "chrome_network": "get_network",
    "chrome_wait": "wait_for",
    "chrome_scroll": "scroll",
    "chrome_tabs": "list_pages",
    "chrome_new_tab": "new_page",
    "chrome_close_tab": "close_page",
    "chrome_switch_tab": "select_page",
    "chrome_pdf": "generate_pdf",
}

# Tools that need special session handling (destroy needs to happen
# at server level, not inside tool handler)
SESSION_DESTROY_TOOL = "chrome_session_end"


class ToolContextImpl:
    """Implementation of ToolContext for tool handlers.

    Provides access to Chrome instance and pool manager,
    plus CDP and JS evaluation helpers.
    """

    def __init__(
        self,
        instance: ChromeInstance,
        pool: ChromePoolManager,
    ) -> None:
        self._instance = instance
        self._pool = pool

    @property
    def instance(self) -> ChromeInstance:
        """Current Chrome instance."""
        return self._instance

    @property
    def pool(self) -> ChromePoolManager:
        """Chrome pool manager."""
        return self._pool

    async def send_cdp(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a CDP command using persistent connection or proxy fallback."""
        if self._instance.cdp and self._instance.is_connected:
            try:
                return await self._instance.cdp.send(method, params)
            except Exception as e:
                logger.warning("Persistent CDP send failed, falling back to proxy: %s", e)

        if not self._instance.proxy:
            raise RuntimeError("No CDP connection available (no proxy)")

        all_targets = await self._instance.proxy.list_targets()
        page_targets = [t for t in all_targets if t.get("type") == "page"]

        exact_match = next(
            (t for t in page_targets if t.get("id") == self._instance.current_target_id),
            None,
        )

        if not exact_match and page_targets:
            exact_match = page_targets[0]
            old_id = self._instance.current_target_id
            self._instance.current_target_id = exact_match.get("id")
            self._instance.targets = [str(t["id"]) for t in page_targets if t.get("id")]
            logger.warning(
                "Target %s not found, falling back to %s (%s)",
                old_id,
                exact_match.get("id"),
                exact_match.get("url", "unknown"),
            )
        target = exact_match

        if not target:
            raise RuntimeError(
                f"No page targets available (found {len(all_targets)} non-page targets)"
            )

        ws_url = target.get("webSocketDebuggerUrl", "")
        if not ws_url:
            raise RuntimeError(f"Target {target.get('id')} has no webSocketDebuggerUrl")

        return await self._instance.proxy.send_cdp_command(ws_url, method, params)

    async def evaluate_js(self, expression: str) -> Any:
        """Evaluate JavaScript in the page context."""
        result = await self.send_cdp(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")


class ChromeMCPServer:
    """MCP Server providing Chrome DevTools capabilities.

    Each opencode chat session gets its own Chrome instance on a unique port,
    with persistent WebSocket connections for real-time event handling.
    All tools are registered in the modular tools/ package.
    """

    def __init__(self) -> None:
        """Initialize the Chrome MCP Server."""
        self.server = Server("wsl-chrome-mcp")
        self._pool: ChromePoolManager | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register all MCP handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available Chrome DevTools tools."""
            return [tool_def.to_mcp_tool(SESSION_ID_PROPERTY) for tool_def in get_all_tools()]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
            """Handle tool calls with session-aware routing."""
            try:
                session_id = arguments.pop("session_id", "default")
                logger.info("call_tool: %s session_id=%s", name, session_id)

                if self._pool is None:
                    cfg = load_config()
                    if cfg.chrome.profile_mode == "profile":
                        self._pool = ChromePoolManager(
                            port_min=cfg.chrome.debug_port,
                            headless=cfg.chrome.headless,
                            profile_mode=cfg.chrome.profile_mode,
                            profile_name=cfg.chrome.profile_name,
                        )
                    else:
                        self._pool = ChromePoolManager(
                            port_min=cfg.chrome.debug_port,
                            headless=cfg.chrome.headless,
                        )

                # Resolve tool name aliases
                resolved_name = TOOL_ALIASES.get(name, name)

                # Session destroy is special: destroys the instance
                if resolved_name == SESSION_DESTROY_TOOL:
                    return await self._session_end(session_id)

                # All other tools: get or create Chrome instance
                instance = await self._pool.get_or_create(session_id)

                if not instance.is_connected and not instance.proxy:
                    return [
                        TextContent(
                            type="text",
                            text=f"Error: No connection for session {session_id}",
                        )
                    ]

                # Look up tool in registry and dispatch
                tool_def = get_tool(resolved_name)
                if tool_def:
                    ctx = ToolContextImpl(instance, self._pool)
                    return await tool_def.handler(arguments, ctx)

                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            except Exception as e:
                logger.exception(f"Error in tool {name}")
                return [TextContent(type="text", text=f"Error: {e!s}")]

    async def _session_end(self, session_id: str) -> ContentResult:
        """End a session, killing its Chrome process."""
        assert self._pool is not None
        try:
            await self._pool.destroy(session_id)
            return [TextContent(type="text", text=f"Session ended: {session_id}")]
        except KeyError:
            return [TextContent(type="text", text=f"Session not found: {session_id}")]

    # --- Lifecycle ---

    async def run(self) -> None:
        """Run the MCP server."""
        logger.info("Starting WSL Chrome MCP Server...")

        if is_wsl():
            host_ip = get_windows_host_ip()
            logger.info(f"Running in WSL, Windows host IP: {host_ip}")
        else:
            logger.info("Running in native environment")

        async with stdio_server() as (read_stream, write_stream):
            try:
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
            finally:
                await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        if self._pool:
            await self._pool.cleanup_all()
            self._pool = None


def main() -> None:
    """Main entry point."""
    from dotenv import load_dotenv

    load_dotenv()
    server = ChromeMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
