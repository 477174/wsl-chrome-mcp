"""WSL Chrome MCP Server - Chrome DevTools for coding agents in WSL."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
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

from .cdp_client import (
    CDPClient,
    CDPSession,
    evaluate_javascript,
    get_document_html,
    navigate,
    take_screenshot,
)
from .cdp_proxy import CDPProxyClient, should_use_proxy
from .chrome_launcher import ChromeLauncher
from .logging_config import setup_logging
from .proxy_session_manager import ProxySessionManager, ProxySessionState
from .session_manager import SessionManager, SessionState
from .wsl import get_windows_host_ip, is_wsl

# Configure logging to file (logs/{Y}/{M}/{D}/wsl-chrome-mcp.log) + stderr
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

    Supports multi-session isolation: each opencode chat session gets its own
    Chrome window with independent tabs, console messages, and network requests.
    Session routing is handled via the session_id parameter (auto-injected by
    the opencode plugin).
    """

    def __init__(self) -> None:
        """Initialize the Chrome MCP Server."""
        self.server = Server("wsl-chrome-mcp")
        self.launcher: ChromeLauncher | None = None
        self.cdp: CDPClient | None = None
        self.session_manager: SessionManager | None = None

        # Proxy mode for WSL with network isolation
        self._use_proxy = False
        self._proxy: CDPProxyClient | None = None
        self._proxy_session_manager: ProxySessionManager | None = None

        # Register handlers
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
                    description="Take a screenshot of the current page. Returns the image.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "full_page": {
                                    "type": "boolean",
                                    "description": "Capture the full page (default: false)",
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
                    description="Click on an element in the page using a CSS selector.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector of the element to click",
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
                                    "description": "CSS selector of the input element",
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Text to type into the element",
                                },
                                "clear_first": {
                                    "type": "boolean",
                                    "description": "Clear the input before typing (default: true)",
                                    "default": True,
                                },
                            }
                        ),
                        "required": ["selector", "text"],
                    },
                ),
                Tool(
                    name="chrome_get_html",
                    description="Get the HTML content of the current page or a specific element.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector for element (optional)",
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_evaluate",
                    description="Execute JavaScript code in the browser and return the result.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "expression": {
                                    "type": "string",
                                    "description": "JavaScript expression to evaluate",
                                },
                            }
                        ),
                        "required": ["expression"],
                    },
                ),
                Tool(
                    name="chrome_console",
                    description="Get console messages (logs, warnings, errors) from the browser.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "clear": {
                                    "type": "boolean",
                                    "description": "Clear after returning (default: false)",
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
                                    "description": "Clear after returning (default: false)",
                                    "default": False,
                                },
                            }
                        ),
                    },
                ),
                Tool(
                    name="chrome_wait",
                    description="Wait for an element to appear on the page.",
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
                                    "description": "Maximum time to wait in seconds (default: 10)",
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
                                    "description": "Scroll amount in pixels (default: 500)",
                                    "default": 500,
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "Element selector (optional, defaults to page)",
                                },
                            }
                        ),
                        "required": ["direction"],
                    },
                ),
                Tool(
                    name="chrome_tabs",
                    description="List all open browser tabs in this session's window.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id({}),
                    },
                ),
                Tool(
                    name="chrome_new_tab",
                    description="Open a new browser tab in this session's window.",
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
                    description="Close a browser tab.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "tab_id": {
                                    "type": "string",
                                    "description": "ID of the tab to close",
                                },
                            }
                        ),
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="chrome_switch_tab",
                    description="Switch to a different browser tab.",
                    inputSchema={
                        "type": "object",
                        "properties": _with_session_id(
                            {
                                "tab_id": {
                                    "type": "string",
                                    "description": "ID of the tab to switch to",
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
                                    "description": "Use landscape orientation (default: false)",
                                    "default": False,
                                },
                                "print_background": {
                                    "type": "boolean",
                                    "description": "Print background graphics (default: true)",
                                    "default": True,
                                },
                            }
                        ),
                    },
                ),
                # Session management tools
                Tool(
                    name="chrome_session_start",
                    description=(
                        "Create a new Chrome session with its own window."
                        " Sessions are auto-created on first tool call."
                    ),
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
                    description="List all active Chrome sessions and their windows.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name="chrome_session_end",
                    description="End a Chrome session, closing its window and all tabs.",
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
                # Extract session_id from arguments (auto-injected by plugin)
                session_id = arguments.pop("session_id", "default")
                logger.info("call_tool: %s session_id=%s", name, session_id)

                # Ensure we have a connection to Chrome
                await self._ensure_connected()

                # Session management tools (work in both modes)
                if name == "chrome_session_start":
                    return await self._session_start(
                        session_id, arguments.get("url", "about:blank")
                    )
                elif name == "chrome_session_list":
                    return await self._session_list()
                elif name == "chrome_session_end":
                    return await self._session_end(session_id)

                # Proxy mode with multi-session support
                if self._use_proxy:
                    assert self._proxy_session_manager is not None
                    state = await self._proxy_session_manager.get_or_create(session_id)
                    return await self._handle_proxy_tool(name, arguments, state)

                # Multi-session mode: get or create session, then route
                assert self.session_manager is not None
                state = await self.session_manager.get_or_create(session_id)
                session = state.current_session

                if session is None:
                    return [
                        TextContent(
                            type="text",
                            text=f"Error: No active tab in session {session_id}",
                        )
                    ]

                if name == "chrome_navigate":
                    return await self._navigate(session, arguments["url"])
                elif name == "chrome_screenshot":
                    return await self._screenshot(
                        session,
                        arguments.get("full_page", False),
                        arguments.get("format", "png"),
                    )
                elif name == "chrome_click":
                    return await self._click(session, arguments["selector"])
                elif name == "chrome_type":
                    return await self._type(
                        session,
                        arguments["selector"],
                        arguments["text"],
                        arguments.get("clear_first", True),
                    )
                elif name == "chrome_get_html":
                    return await self._get_html(session, arguments.get("selector"))
                elif name == "chrome_evaluate":
                    return await self._evaluate(session, arguments["expression"])
                elif name == "chrome_console":
                    return await self._get_console(state, arguments.get("clear", False))
                elif name == "chrome_network":
                    return await self._get_network(state, arguments.get("clear", False))
                elif name == "chrome_wait":
                    return await self._wait_for(
                        session,
                        arguments["selector"],
                        arguments.get("timeout", 10),
                    )
                elif name == "chrome_scroll":
                    return await self._scroll(
                        session,
                        arguments["direction"],
                        arguments.get("amount", 500),
                        arguments.get("selector"),
                    )
                elif name == "chrome_tabs":
                    return await self._list_tabs(state)
                elif name == "chrome_new_tab":
                    return await self._new_tab(state, arguments.get("url", "about:blank"))
                elif name == "chrome_close_tab":
                    return await self._close_tab(state, arguments["tab_id"])
                elif name == "chrome_switch_tab":
                    return await self._switch_tab(state, arguments["tab_id"])
                elif name == "chrome_pdf":
                    return await self._generate_pdf(
                        session,
                        arguments.get("landscape", False),
                        arguments.get("print_background", True),
                    )
                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]

            except Exception as e:
                logger.exception(f"Error in tool {name}")
                return [TextContent(type="text", text=f"Error: {str(e)}")]

    async def _ensure_connected(self) -> None:
        """Ensure we have a connected Chrome instance."""
        if self._use_proxy and self._proxy_session_manager is not None:
            return
        if self.session_manager is not None:
            return

        port = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
        headless = False  # Always use headed mode (visible browser)
        user_data_dir = os.environ.get("CHROME_USER_DATA_DIR")

        # Check if we need proxy mode (WSL with network isolation)
        if is_wsl() and should_use_proxy():
            logger.info("WSL network isolation detected, using CDP proxy mode")
            await self._ensure_connected_proxy(port, headless, user_data_dir)
            return

        # Direct connection with multi-session support
        await self._ensure_connected_direct(port, headless, user_data_dir)

    async def _ensure_connected_proxy(
        self, port: int, headless: bool, user_data_dir: str | None
    ) -> None:
        """Connect using CDP proxy for WSL network isolation."""
        from .wsl import run_windows_command

        self._use_proxy = True
        self._proxy = CDPProxyClient(port)

        # Check if Chrome is already running on Windows
        version = await self._proxy.get_version()
        if not version:
            # Launch Chrome on Windows
            logger.info("Launching Chrome on Windows...")

            # Create temp dir on Windows
            create_temp = (
                '$temp = Join-Path $env:TEMP ("wsl-chrome-mcp-" + '
                "[System.IO.Path]::GetRandomFileName()); "
                "New-Item -ItemType Directory -Path $temp -Force | Out-Null; "
                "Write-Output $temp"
            )
            result = run_windows_command(create_temp, timeout=10.0)
            temp_dir = result.stdout.strip() if result.returncode == 0 else None

            if not temp_dir:
                raise RuntimeError("Failed to create temp directory on Windows")

            # Build Chrome args
            args = [
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=0.0.0.0",
                f"--user-data-dir={temp_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if headless:
                args.append("--headless=new")

            args_str = '","'.join(args)

            # Find and launch Chrome
            find_chrome = """
            $paths = @(
                "$env:PROGRAMFILES\\Google\\Chrome\\Application\\chrome.exe",
                "${env:PROGRAMFILES(x86)}\\Google\\Chrome\\Application\\chrome.exe",
                "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
            )
            foreach ($p in $paths) { if (Test-Path $p) { Write-Output $p; break } }
            """
            result = run_windows_command(find_chrome, timeout=10.0)
            chrome_path = result.stdout.strip() if result.returncode == 0 else None

            if not chrome_path:
                raise RuntimeError("Chrome not found on Windows")

            launch_cmd = f'Start-Process -FilePath "{chrome_path}" -ArgumentList "{args_str}"'
            run_windows_command(launch_cmd, timeout=10.0)

            # Wait for Chrome to be ready
            import time

            for _ in range(30):
                time.sleep(1)
                version = await self._proxy.get_version()
                if version:
                    break
            else:
                raise RuntimeError("Chrome did not start within 30 seconds")

        logger.info(f"Connected to Chrome via proxy: {version.get('Browser', 'unknown')}")

        # Create proxy session manager for multi-session support
        self._proxy_session_manager = ProxySessionManager(self._proxy)
        logger.info("Proxy session manager initialized")

    async def _ensure_connected_direct(
        self, port: int, headless: bool, user_data_dir: str | None
    ) -> None:
        """Connect directly to Chrome with multi-session support."""
        if self.launcher is None:
            self.launcher = ChromeLauncher(
                port=port,
                headless=headless,
                user_data_dir=user_data_dir,
            )

        # Connect or launch Chrome
        chrome = await self.launcher.connect_or_launch()
        logger.info(f"Connected to Chrome at {chrome.debugger_url}")

        # Create CDP client and session manager
        self.cdp = CDPClient(chrome.debugger_url)
        await self.cdp.__aenter__()
        self.session_manager = SessionManager(self.cdp)

    # --- Session management tools (work in both modes) ---

    async def _session_start(self, session_id: str, url: str) -> ContentResult:
        """Create a new session with its own Chrome window."""
        if self._use_proxy:
            assert self._proxy_session_manager is not None
            state = await self._proxy_session_manager.get_or_create(session_id)

            # Navigate to the requested URL if not about:blank
            if url != "about:blank" and state.current_ws_url is not None:
                assert self._proxy is not None
                await self._proxy.navigate(state.current_ws_url, url)

            return [
                TextContent(
                    type="text",
                    text=(
                        f"Session started: {session_id}\n"
                        f"Window ID: {state.window_id}\n"
                        f"Tab: {state.current_target_id}"
                    ),
                )
            ]

        assert self.session_manager is not None
        state = await self.session_manager.get_or_create(session_id)

        # Navigate to the requested URL if not about:blank
        if url != "about:blank" and state.current_session is not None:
            await navigate(state.current_session, url)

        return [
            TextContent(
                type="text",
                text=(
                    f"Session started: {session_id}\n"
                    f"Window ID: {state.window_id}\n"
                    f"Tab: {state.current_target_id}"
                ),
            )
        ]

    async def _session_list(self) -> ContentResult:
        """List all active sessions."""
        if self._use_proxy:
            assert self._proxy_session_manager is not None
            sessions = self._proxy_session_manager.list_sessions()
        else:
            assert self.session_manager is not None
            sessions = self.session_manager.list_sessions()

        if not sessions:
            return [TextContent(type="text", text="No active sessions.")]

        lines = ["Active sessions:"]
        for sid, info in sessions.items():
            lines.append(
                f"  - {sid}: window={info['window_id']}, "
                f"tabs={info['tab_count']}, "
                f"current={info['current_target_id']}"
            )

        return [TextContent(type="text", text="\n".join(lines))]

    async def _session_end(self, session_id: str) -> ContentResult:
        """End a session, closing its window and all tabs."""
        if self._use_proxy:
            assert self._proxy_session_manager is not None
            try:
                await self._proxy_session_manager.destroy(session_id)
                return [TextContent(type="text", text=f"Session ended: {session_id}")]
            except KeyError:
                return [TextContent(type="text", text=f"Session not found: {session_id}")]

        assert self.session_manager is not None
        try:
            await self.session_manager.destroy(session_id)
            return [TextContent(type="text", text=f"Session ended: {session_id}")]
        except KeyError:
            return [TextContent(type="text", text=f"Session not found: {session_id}")]

    # --- Proxy mode handlers (multi-session, accept ProxySessionState) ---

    async def _handle_proxy_tool(
        self, name: str, arguments: dict[str, Any], state: ProxySessionState
    ) -> ContentResult:
        """Handle tool calls in proxy mode with multi-session support."""
        ws_url = state.current_ws_url
        if ws_url is None:
            return [
                TextContent(
                    type="text",
                    text=f"Error: No active tab in session {state.session_id}",
                )
            ]

        if name == "chrome_navigate":
            return await self._navigate_proxy(ws_url, arguments["url"])
        elif name == "chrome_screenshot":
            return await self._screenshot_proxy(
                ws_url,
                arguments.get("full_page", False),
                arguments.get("format", "png"),
            )
        elif name == "chrome_click":
            return await self._click_proxy(ws_url, arguments["selector"])
        elif name == "chrome_type":
            return await self._type_proxy(
                ws_url,
                arguments["selector"],
                arguments["text"],
                arguments.get("clear_first", True),
            )
        elif name == "chrome_get_html":
            return await self._get_html_proxy(ws_url, arguments.get("selector"))
        elif name == "chrome_evaluate":
            return await self._evaluate_proxy(ws_url, arguments["expression"])
        elif name == "chrome_console":
            return [
                TextContent(type="text", text="Console monitoring not available in proxy mode.")
            ]
        elif name == "chrome_network":
            return [
                TextContent(type="text", text="Network monitoring not available in proxy mode.")
            ]
        elif name == "chrome_wait":
            return await self._wait_for_proxy(
                ws_url,
                arguments["selector"],
                arguments.get("timeout", 10),
            )
        elif name == "chrome_scroll":
            return await self._scroll_proxy(
                ws_url,
                arguments["direction"],
                arguments.get("amount", 500),
                arguments.get("selector"),
            )
        elif name == "chrome_tabs":
            return await self._list_tabs_proxy(state)
        elif name == "chrome_new_tab":
            return await self._new_tab_proxy(state, arguments.get("url", "about:blank"))
        elif name == "chrome_close_tab":
            return await self._close_tab_proxy(state, arguments["tab_id"])
        elif name == "chrome_switch_tab":
            return await self._switch_tab_proxy(state, arguments["tab_id"])
        elif name == "chrome_pdf":
            return await self._generate_pdf_proxy(
                ws_url,
                arguments.get("landscape", False),
                arguments.get("print_background", True),
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    # --- Direct mode handlers (multi-session, accept CDPSession/SessionState) ---

    async def _navigate(self, session: CDPSession, url: str) -> ContentResult:
        """Navigate to a URL."""
        result = await navigate(session, url)
        title = await evaluate_javascript(session, "document.title")
        frame_id = result.get("frameId", "unknown")
        return [
            TextContent(
                type="text",
                text=f"Navigated to: {url}\nTitle: {title}\nFrame ID: {frame_id}",
            )
        ]

    async def _screenshot(self, session: CDPSession, full_page: bool, format: str) -> ContentResult:
        """Take a screenshot."""
        image_data = await take_screenshot(session, format=format, full_page=full_page)
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(image_data).decode("utf-8"),
                mimeType=f"image/{format}",
            )
        ]

    async def _click(self, session: CDPSession, selector: str) -> ContentResult:
        """Click on an element."""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            el.click();
            return {{ success: true, tagName: el.tagName }};
        }})()
        """
        result = await evaluate_javascript(session, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        tag = result.get("tagName", "unknown") if isinstance(result, dict) else "unknown"
        return [TextContent(type="text", text=f"Clicked on {selector} ({tag})")]

    async def _type(
        self, session: CDPSession, selector: str, text: str, clear_first: bool
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
        result = await evaluate_javascript(session, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        return [TextContent(type="text", text=f"Typed into {selector}")]

    async def _get_html(self, session: CDPSession, selector: str | None) -> ContentResult:
        """Get HTML content."""
        if selector:
            js = f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'Element not found: {selector}' }};
                return {{ html: el.outerHTML }};
            }})()
            """
            result = await evaluate_javascript(session, js)

            if isinstance(result, dict) and result.get("error"):
                return [TextContent(type="text", text=f"Error: {result['error']}")]
            html = result.get("html", "") if isinstance(result, dict) else ""
        else:
            html = await get_document_html(session)

        return [TextContent(type="text", text=html)]

    async def _evaluate(self, session: CDPSession, expression: str) -> ContentResult:
        """Evaluate JavaScript."""
        result = await evaluate_javascript(session, expression)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async def _get_console(self, state: SessionState, clear: bool) -> ContentResult:
        """Get console messages for a session."""
        messages = list(state.console_messages)
        if clear:
            state.console_messages.clear()

        if not messages:
            return [TextContent(type="text", text="No console messages collected.")]

        formatted = []
        for msg in messages:
            formatted.append(f"[{msg.get('type', 'log').upper()}] {msg.get('text', '')}")

        return [TextContent(type="text", text="\n".join(formatted))]

    async def _get_network(self, state: SessionState, clear: bool) -> ContentResult:
        """Get network requests for a session."""
        requests = list(state.network_requests)
        if clear:
            state.network_requests.clear()

        if not requests:
            return [TextContent(type="text", text="No network requests collected.")]

        formatted = []
        for req in requests:
            formatted.append(f"{req.get('method', 'GET')} {req.get('url', 'unknown')}")

        return [TextContent(type="text", text="\n".join(formatted))]

    async def _wait_for(self, session: CDPSession, selector: str, timeout: float) -> ContentResult:
        """Wait for an element to appear."""
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 0.3

        while asyncio.get_event_loop().time() < deadline:
            js = f"document.querySelector({json.dumps(selector)}) !== null"
            found = await evaluate_javascript(session, js)
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
        self, session: CDPSession, direction: str, amount: int, selector: str | None
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
        await evaluate_javascript(session, js)

        return [TextContent(type="text", text=f"Scrolled {direction}")]

    async def _list_tabs(self, state: SessionState) -> ContentResult:
        """List tabs in this session's window only."""
        assert self.session_manager is not None
        tabs = await self.session_manager.list_tabs_in_session(state.session_id)

        if not tabs:
            return [TextContent(type="text", text="No tabs open.")]

        lines = ["Open tabs:"]
        for tab in tabs:
            current = " (current)" if tab["is_current"] else ""
            lines.append(f"  - [{tab['id']}] {tab['title'] or 'Untitled'}: {tab['url']}{current}")

        return [TextContent(type="text", text="\n".join(lines))]

    async def _new_tab(self, state: SessionState, url: str) -> ContentResult:
        """Open a new tab in this session's window."""
        assert self.session_manager is not None
        target_id = await self.session_manager.create_tab_in_session(state.session_id, url)
        return [TextContent(type="text", text=f"Opened new tab: [{target_id}] {url}")]

    async def _close_tab(self, state: SessionState, tab_id: str) -> ContentResult:
        """Close a tab in this session."""
        assert self.session_manager is not None
        try:
            await self.session_manager.close_tab_in_session(state.session_id, tab_id)
            return [TextContent(type="text", text=f"Closed tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _switch_tab(self, state: SessionState, tab_id: str) -> ContentResult:
        """Switch to a different tab in this session."""
        assert self.session_manager is not None
        try:
            await self.session_manager.switch_tab_in_session(state.session_id, tab_id)
            return [TextContent(type="text", text=f"Switched to tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _generate_pdf(
        self, session: CDPSession, landscape: bool, print_background: bool
    ) -> ContentResult:
        """Generate a PDF of the page."""
        result = await session.send(
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

    # --- Proxy mode handlers (multi-session, accept ws_url/state) ---

    async def _navigate_proxy(self, ws_url: str, url: str) -> ContentResult:
        """Navigate in proxy mode."""
        assert self._proxy is not None
        result = await self._proxy.navigate(ws_url, url)
        title = await self._proxy.evaluate(ws_url, "document.title")
        frame_id = result.get("frameId", "unknown")
        return [
            TextContent(
                type="text",
                text=f"Navigated to: {url}\nTitle: {title}\nFrame ID: {frame_id}",
            )
        ]

    async def _screenshot_proxy(self, ws_url: str, full_page: bool, format: str) -> ContentResult:
        """Screenshot in proxy mode."""
        assert self._proxy is not None
        image_data = await self._proxy.screenshot(ws_url, format=format, full_page=full_page)
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(image_data).decode("utf-8"),
                mimeType=f"image/{format}",
            )
        ]

    async def _click_proxy(self, ws_url: str, selector: str) -> ContentResult:
        """Click in proxy mode."""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            el.click();
            return {{ success: true, tagName: el.tagName }};
        }})()
        """
        assert self._proxy is not None
        result = await self._proxy.evaluate(ws_url, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        tag = result.get("tagName", "unknown") if isinstance(result, dict) else "unknown"
        return [TextContent(type="text", text=f"Clicked on {selector} ({tag})")]

    async def _type_proxy(
        self, ws_url: str, selector: str, text: str, clear_first: bool
    ) -> ContentResult:
        """Type in proxy mode."""
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
        assert self._proxy is not None
        result = await self._proxy.evaluate(ws_url, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        return [TextContent(type="text", text=f"Typed into {selector}")]

    async def _get_html_proxy(self, ws_url: str, selector: str | None) -> ContentResult:
        """Get HTML in proxy mode."""
        assert self._proxy is not None
        if selector:
            js = f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'Element not found: {selector}' }};
                return {{ html: el.outerHTML }};
            }})()
            """
            result = await self._proxy.evaluate(ws_url, js)

            if isinstance(result, dict) and result.get("error"):
                return [TextContent(type="text", text=f"Error: {result['error']}")]
            html = result.get("html", "") if isinstance(result, dict) else ""
        else:
            html = await self._proxy.get_html(ws_url)

        return [TextContent(type="text", text=html)]

    async def _evaluate_proxy(self, ws_url: str, expression: str) -> ContentResult:
        """Evaluate JS in proxy mode."""
        assert self._proxy is not None
        result = await self._proxy.evaluate(ws_url, expression)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async def _wait_for_proxy(self, ws_url: str, selector: str, timeout: float) -> ContentResult:
        """Wait for element in proxy mode."""
        assert self._proxy is not None
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            js = f"document.querySelector({json.dumps(selector)}) !== null"
            found = await self._proxy.evaluate(ws_url, js)
            if found:
                return [TextContent(type="text", text=f"Element found: {selector}")]
            await asyncio.sleep(poll_interval)

        return [
            TextContent(
                type="text",
                text=f"Timeout: Element not found after {timeout}s: {selector}",
            )
        ]

    async def _scroll_proxy(
        self, ws_url: str, direction: str, amount: int, selector: str | None
    ) -> ContentResult:
        """Scroll in proxy mode."""
        assert self._proxy is not None
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
        await self._proxy.evaluate(ws_url, js)

        return [TextContent(type="text", text=f"Scrolled {direction}")]

    async def _list_tabs_proxy(self, state: ProxySessionState) -> ContentResult:
        """List tabs in this session's window only."""
        assert self._proxy_session_manager is not None
        tabs = await self._proxy_session_manager.list_tabs_in_session(state.session_id)

        if not tabs:
            return [TextContent(type="text", text="No tabs open.")]

        lines = ["Open tabs:"]
        for tab in tabs:
            current = " (current)" if tab["is_current"] else ""
            lines.append(f"  - [{tab['id']}] {tab['title'] or 'Untitled'}: {tab['url']}{current}")
        return [TextContent(type="text", text="\n".join(lines))]

    async def _new_tab_proxy(self, state: ProxySessionState, url: str) -> ContentResult:
        """New tab in this session's window."""
        assert self._proxy_session_manager is not None
        target_id = await self._proxy_session_manager.create_tab_in_session(state.session_id, url)
        return [TextContent(type="text", text=f"Opened new tab: [{target_id}] {url}")]

    async def _close_tab_proxy(self, state: ProxySessionState, tab_id: str) -> ContentResult:
        """Close tab in this session."""
        assert self._proxy_session_manager is not None
        try:
            await self._proxy_session_manager.close_tab_in_session(state.session_id, tab_id)
            return [TextContent(type="text", text=f"Closed tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _switch_tab_proxy(self, state: ProxySessionState, tab_id: str) -> ContentResult:
        """Switch tab in this session."""
        assert self._proxy_session_manager is not None
        try:
            await self._proxy_session_manager.switch_tab_in_session(state.session_id, tab_id)
            return [TextContent(type="text", text=f"Switched to tab: {tab_id}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _generate_pdf_proxy(
        self, ws_url: str, landscape: bool, print_background: bool
    ) -> ContentResult:
        """Generate PDF in proxy mode."""
        assert self._proxy is not None
        result = await self._proxy.send_cdp_command(
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
        if self._proxy_session_manager:
            await self._proxy_session_manager.cleanup()
            self._proxy_session_manager = None

        if self.session_manager:
            await self.session_manager.cleanup()
            self.session_manager = None

        if self.cdp:
            await self.cdp.close()
            self.cdp = None

        if self.launcher:
            await self.launcher.close()
            self.launcher = None


def main() -> None:
    """Main entry point."""
    server = ChromeMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
