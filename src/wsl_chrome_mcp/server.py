"""WSL Chrome MCP Server - Chrome DevTools for coding agents in WSL.

Each session gets its own Chrome instance for complete isolation.
"""

from __future__ import annotations

import asyncio
import base64
import json
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
from pydantic import AnyUrl

from .cdp_proxy import CDPProxyClient
from .chrome_pool import ChromeInstance, ChromePoolManager
from .logging_config import setup_logging
from .wsl import get_windows_host_ip, is_wsl

# Configure logging
setup_logging()
logger = logging.getLogger("wsl-chrome-mcp")

# Type alias for tool return values
ContentResult = Sequence[TextContent | ImageContent | EmbeddedResource]

# session_id property shared across all tool schemas
SESSION_ID_PROPERTY = {
    "session_id": {
        "type": "string",
        "description": (
            "Session identifier for multi-session isolation (auto-injected by opencode plugin)"
        ),
    },
}


def _with_session_id(properties: dict[str, Any]) -> dict[str, Any]:
    """Add session_id property to a tool's properties dict."""
    return {**properties, **SESSION_ID_PROPERTY}


class ChromeMCPServer:
    """MCP Server providing Chrome DevTools capabilities.

    Each opencode chat session gets its own Chrome instance on a unique port.
    This provides complete isolation - no window confusion, no race conditions.
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
            return [
                Tool(
                    name="chrome_navigate",
                    description="Navigate to a URL in Chrome. Waits for page load.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "url": {
                                    "type": "string",
                                    "description": "The URL to navigate to",
                                },
                            }
                        ),
                        "required": ["url"],
                    },
                ),
                Tool(
                    name="chrome_screenshot",
                    description="Take a screenshot of the current page.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "full_page": {
                                    "type": "boolean",
                                    "description": "Capture full page (default: false)",
                                    "default": False,
                                },
                                "format": {
                                    "type": "string",
                                    "enum": ["png", "jpeg"],
                                    "description": "Image format (default: png)",
                                    "default": "png",
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_click",
                    description="Click on an element using a CSS selector.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector of element to click",
                                },
                            }
                        ),
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="chrome_type",
                    description="Type text into an input element.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector of input element",
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Text to type",
                                },
                                "clear_first": {
                                    "type": "boolean",
                                    "description": "Clear input first (default: true)",
                                    "default": True,
                                },
                            }
                        ),
                        "required": ["selector", "text"],
                    },
                ),
                Tool(
                    name="chrome_get_html",
                    description="Get HTML content of page or element.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector (optional)",
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_evaluate",
                    description="Execute JavaScript and return result.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "expression": {
                                    "type": "string",
                                    "description": "JavaScript to evaluate",
                                },
                            }
                        ),
                        "required": ["expression"],
                    },
                ),
                Tool(
                    name="chrome_console",
                    description="Get console messages from the browser.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "clear": {
                                    "type": "boolean",
                                    "description": "Clear after returning",
                                    "default": False,
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_network",
                    description="Get network requests made by the page.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "clear": {
                                    "type": "boolean",
                                    "description": "Clear after returning",
                                    "default": False,
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_wait",
                    description="Wait for an element to appear.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector to wait for",
                                },
                                "timeout": {
                                    "type": "number",
                                    "description": "Max wait in seconds (default: 10)",
                                    "default": 10,
                                },
                            }
                        ),
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="chrome_scroll",
                    description="Scroll the page or an element.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "direction": {
                                    "type": "string",
                                    "enum": ["up", "down", "left", "right", "top", "bottom"],
                                    "description": "Direction to scroll",
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Pixels to scroll (default: 500)",
                                    "default": 500,
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "Element selector (optional)",
                                },
                            }
                        ),
                        "required": ["direction"],
                    },
                ),
                Tool(
                    name="chrome_tabs",
                    description="List all open tabs in this session.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id({}),
                    },
                ),
                Tool(
                    name="chrome_new_tab",
                    description="Open a new tab in this session.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "url": {
                                    "type": "string",
                                    "description": "URL to open (default: about:blank)",
                                    "default": "about:blank",
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_close_tab",
                    description="Close a tab.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "tab_id": {
                                    "type": "string",
                                    "description": "ID of tab to close",
                                },
                            }
                        ),
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="chrome_switch_tab",
                    description="Switch to a different tab.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "tab_id": {
                                    "type": "string",
                                    "description": "ID of tab to switch to",
                                },
                            }
                        ),
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="chrome_pdf",
                    description="Generate a PDF of the current page.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "landscape": {
                                    "type": "boolean",
                                    "description": "Landscape orientation",
                                    "default": False,
                                },
                                "print_background": {
                                    "type": "boolean",
                                    "description": "Print background graphics",
                                    "default": True,
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_session_start",
                    description="Start a Chrome session. Auto-created on first tool call.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "url": {
                                    "type": "string",
                                    "description": "URL to open (default: about:blank)",
                                    "default": "about:blank",
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_session_list",
                    description="List all active Chrome sessions.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name="chrome_session_end",
                    description="End a session, killing its Chrome process.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id({}),
                        "required": ["session_id"],
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
            """Handle tool calls with session-aware routing."""
            try:
                session_id = arguments.pop("session_id", "default")
                logger.info("call_tool: %s session_id=%s", name, session_id)

                # Ensure pool is initialized
                if self._pool is None:
                    self._pool = ChromePoolManager()

                # Session management tools
                if name == "chrome_session_start":
                    return await self._session_start(
                        session_id, arguments.get("url", "about:blank")
                    )
                elif name == "chrome_session_list":
                    return await self._session_list()
                elif name == "chrome_session_end":
                    return await self._session_end(session_id)

                # Get or create Chrome instance for this session
                instance = await self._pool.get_or_create(session_id)

                # Route to appropriate handler
                return await self._handle_tool(name, arguments, instance)

            except Exception as e:
                logger.exception(f"Error in tool {name}")
                return [TextContent(type="text", text=f"Error: {e!s}")]

    async def _handle_tool(
        self, name: str, arguments: dict[str, Any], instance: ChromeInstance
    ) -> ContentResult:
        """Handle a tool call for a specific Chrome instance."""
        ws_url = instance.current_ws_url
        if ws_url is None:
            return [
                TextContent(
                    type="text",
                    text=f"Error: No active tab in session {instance.session_id}",
                )
            ]

        proxy = instance.proxy

        if name == "chrome_navigate":
            return await self._navigate(proxy, ws_url, arguments["url"])
        elif name == "chrome_screenshot":
            return await self._screenshot(
                proxy,
                ws_url,
                arguments.get("full_page", False),
                arguments.get("format", "png"),
            )
        elif name == "chrome_click":
            return await self._click(proxy, ws_url, arguments["selector"])
        elif name == "chrome_type":
            return await self._type(
                proxy,
                ws_url,
                arguments["selector"],
                arguments["text"],
                arguments.get("clear_first", True),
            )
        elif name == "chrome_get_html":
            return await self._get_html(proxy, ws_url, arguments.get("selector"))
        elif name == "chrome_evaluate":
            return await self._evaluate(proxy, ws_url, arguments["expression"])
        elif name == "chrome_console":
            return [
                TextContent(
                    type="text",
                    text="Console monitoring not available in this mode.",
                )
            ]
        elif name == "chrome_network":
            return [
                TextContent(
                    type="text",
                    text="Network monitoring not available in this mode.",
                )
            ]
        elif name == "chrome_wait":
            return await self._wait_for(
                proxy,
                ws_url,
                arguments["selector"],
                arguments.get("timeout", 10),
            )
        elif name == "chrome_scroll":
            return await self._scroll(
                proxy,
                ws_url,
                arguments["direction"],
                arguments.get("amount", 500),
                arguments.get("selector"),
            )
        elif name == "chrome_tabs":
            return await self._list_tabs(instance)
        elif name == "chrome_new_tab":
            return await self._new_tab(instance, arguments.get("url", "about:blank"))
        elif name == "chrome_close_tab":
            return await self._close_tab(instance, arguments["tab_id"])
        elif name == "chrome_switch_tab":
            return await self._switch_tab(instance, arguments["tab_id"])
        elif name == "chrome_pdf":
            return await self._generate_pdf(
                proxy,
                ws_url,
                arguments.get("landscape", False),
                arguments.get("print_background", True),
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    # --- Session management ---

    async def _session_start(self, session_id: str, url: str) -> ContentResult:
        """Start a new session with its own Chrome instance."""
        assert self._pool is not None
        instance = await self._pool.get_or_create(session_id)

        if url != "about:blank" and instance.current_ws_url:
            await instance.proxy.navigate(instance.current_ws_url, url)

        return [
            TextContent(
                type="text",
                text=(
                    f"Session started: {session_id}\n"
                    f"Port: {instance.port}\n"
                    f"Tab: {instance.current_target_id}"
                ),
            )
        ]

    async def _session_list(self) -> ContentResult:
        """List all active sessions."""
        assert self._pool is not None
        sessions = self._pool.list_sessions()

        if not sessions:
            return [TextContent(type="text", text="No active sessions.")]

        lines = ["Active sessions:"]
        for sid, info in sessions.items():
            lines.append(
                f"  - {sid}: port={info['port']}, pid={info['pid']}, tabs={info['tab_count']}"
            )

        return [TextContent(type="text", text="\n".join(lines))]

    async def _session_end(self, session_id: str) -> ContentResult:
        """End a session, killing its Chrome process."""
        assert self._pool is not None
        try:
            await self._pool.destroy(session_id)
            return [TextContent(type="text", text=f"Session ended: {session_id}")]
        except KeyError:
            return [TextContent(type="text", text=f"Session not found: {session_id}")]

    # --- Tool handlers ---

    async def _navigate(self, proxy: CDPProxyClient, ws_url: str, url: str) -> ContentResult:
        """Navigate to a URL."""
        result = await proxy.navigate(ws_url, url)
        title = await proxy.evaluate(ws_url, "document.title")
        frame_id = result.get("frameId", "unknown")
        return [
            TextContent(
                type="text",
                text=f"Navigated to: {url}\nTitle: {title}\nFrame ID: {frame_id}",
            )
        ]

    async def _screenshot(
        self, proxy: CDPProxyClient, ws_url: str, full_page: bool, format: str
    ) -> ContentResult:
        """Take a screenshot."""
        image_data = await proxy.screenshot(ws_url, format=format, full_page=full_page)
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(image_data).decode("utf-8"),
                mimeType=f"image/{format}",
            )
        ]

    async def _click(self, proxy: CDPProxyClient, ws_url: str, selector: str) -> ContentResult:
        """Click on an element."""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            el.click();
            return {{ success: true, tagName: el.tagName }};
        }})()
        """
        result = await proxy.evaluate(ws_url, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        tag = result.get("tagName", "unknown") if isinstance(result, dict) else "unknown"
        return [TextContent(type="text", text=f"Clicked on {selector} ({tag})")]

    async def _type(
        self,
        proxy: CDPProxyClient,
        ws_url: str,
        selector: str,
        text: str,
        clear_first: bool,
    ) -> ContentResult:
        """Type text into an input."""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            el.focus();
            if ({str(clear_first).lower()}) {{
                el.value = '';
            }}
            el.value = {json.dumps(text)};
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return {{ success: true }};
        }})()
        """
        result = await proxy.evaluate(ws_url, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        return [TextContent(type="text", text=f"Typed into {selector}")]

    async def _get_html(
        self, proxy: CDPProxyClient, ws_url: str, selector: str | None
    ) -> ContentResult:
        """Get HTML content."""
        if selector:
            js = f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'Element not found: {selector}' }};
                return {{ html: el.outerHTML }};
            }})()
            """
            result = await proxy.evaluate(ws_url, js)

            if isinstance(result, dict) and result.get("error"):
                return [TextContent(type="text", text=f"Error: {result['error']}")]
            html = result.get("html", "") if isinstance(result, dict) else ""
        else:
            html = await proxy.get_html(ws_url)

        return [TextContent(type="text", text=html)]

    async def _evaluate(self, proxy: CDPProxyClient, ws_url: str, expression: str) -> ContentResult:
        """Evaluate JavaScript."""
        result = await proxy.evaluate(ws_url, expression)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async def _wait_for(
        self, proxy: CDPProxyClient, ws_url: str, selector: str, timeout: float
    ) -> ContentResult:
        """Wait for an element to appear."""
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            js = f"document.querySelector({json.dumps(selector)}) !== null"
            found = await proxy.evaluate(ws_url, js)
            if found:
                return [TextContent(type="text", text=f"Element found: {selector}")]
            await asyncio.sleep(poll_interval)

        return [
            TextContent(
                type="text",
                text=f"Timeout: Element not found after {timeout}s: {selector}",
            )
        ]

    async def _scroll(
        self,
        proxy: CDPProxyClient,
        ws_url: str,
        direction: str,
        amount: int,
        selector: str | None,
    ) -> ContentResult:
        """Scroll the page or element."""
        target = f"document.querySelector({json.dumps(selector)})" if selector else "window"

        scroll_code = {
            "up": f"{target}.scrollBy(0, -{amount})",
            "down": f"{target}.scrollBy(0, {amount})",
            "left": f"{target}.scrollBy(-{amount}, 0)",
            "right": f"{target}.scrollBy({amount}, 0)",
            "top": f"{target}.scrollTo(0, 0)",
            "bottom": f"{target}.scrollTo(0, document.body.scrollHeight)",
        }

        js = scroll_code.get(direction, f"{target}.scrollBy(0, {amount})")
        await proxy.evaluate(ws_url, js)

        return [TextContent(type="text", text=f"Scrolled {direction}")]

    async def _list_tabs(self, instance: ChromeInstance) -> ContentResult:
        """List tabs in this session."""
        assert self._pool is not None
        tabs = await self._pool.list_tabs(instance.session_id)

        if not tabs:
            return [TextContent(type="text", text="No tabs open.")]

        lines = ["Open tabs:"]
        for tab in tabs:
            current = " (current)" if tab["is_current"] else ""
            lines.append(f"  - [{tab['id']}] {tab['title'] or 'Untitled'}: {tab['url']}{current}")

        return [TextContent(type="text", text="\n".join(lines))]

    async def _new_tab(self, instance: ChromeInstance, url: str) -> ContentResult:
        """Open a new tab."""
        assert self._pool is not None
        target_id = await self._pool.create_tab(instance.session_id, url)
        return [TextContent(type="text", text=f"Opened new tab: [{target_id}] {url}")]

    async def _close_tab(self, instance: ChromeInstance, tab_id: str) -> ContentResult:
        """Close a tab."""
        assert self._pool is not None
        try:
            await self._pool.close_tab(instance.session_id, tab_id)
            return [TextContent(type="text", text=f"Closed tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _switch_tab(self, instance: ChromeInstance, tab_id: str) -> ContentResult:
        """Switch to a different tab."""
        assert self._pool is not None
        try:
            await self._pool.switch_tab(instance.session_id, tab_id)
            return [TextContent(type="text", text=f"Switched to tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _generate_pdf(
        self, proxy: CDPProxyClient, ws_url: str, landscape: bool, print_background: bool
    ) -> ContentResult:
        """Generate a PDF."""
        result = await proxy.send_cdp_command(
            ws_url,
            "Page.printToPDF",
            {
                "landscape": landscape,
                "printBackground": print_background,
                "preferCSSPageSize": True,
            },
        )
        pdf_data = result["data"]

        return [
            EmbeddedResource(
                type="resource",
                resource={
                    "uri": AnyUrl("data:application/pdf;base64"),
                    "mimeType": "application/pdf",
                    "blob": pdf_data,
                },
            )
        ]

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
    server = ChromeMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
