"""WSL Chrome MCP Server - Chrome DevTools for coding agents in WSL."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
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
    get_console_messages,
    get_document_html,
    get_network_requests,
    navigate,
    take_screenshot,
)
from .cdp_proxy import CDPProxyClient, should_use_proxy
from .chrome_launcher import ChromeLauncher
from .wsl import is_wsl, get_windows_host_ip

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("wsl-chrome-mcp")


class ChromeMCPServer:
    """MCP Server providing Chrome DevTools capabilities."""

    def __init__(self) -> None:
        """Initialize the Chrome MCP Server."""
        self.server = Server("wsl-chrome-mcp")
        self.launcher: ChromeLauncher | None = None
        self.cdp: CDPClient | None = None
        self.session: CDPSession | None = None
        self._console_messages: list[dict[str, Any]] = []
        self._network_requests: list[dict[str, Any]] = []

        # Proxy mode for WSL with network isolation
        self._use_proxy = False
        self._proxy: CDPProxyClient | None = None
        self._proxy_ws_url: str | None = None

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
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The URL to navigate to",
                            },
                        },
                        "required": ["url"],
                    },
                ),
                Tool(
                    name="chrome_screenshot",
                    description="Take a screenshot of the current page. Returns the image.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "full_page": {
                                "type": "boolean",
                                "description": "Capture the full scrollable page (default: false)",
                                "default": False,
                            },
                            "format": {
                                "type": "string",
                                "enum": ["png", "jpeg"],
                                "description": "Image format (default: png)",
                                "default": "png",
                            },
                        },
                    },
                ),
                Tool(
                    name="chrome_click",
                    description="Click on an element in the page using a CSS selector.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector of the element to click",
                            },
                        },
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="chrome_type",
                    description="Type text into an input element.",
                    inputSchema={
                        "type": "object",
                        "properties": {
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
                        },
                        "required": ["selector", "text"],
                    },
                ),
                Tool(
                    name="chrome_get_html",
                    description="Get the HTML content of the current page or a specific element.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector for element (optional)",
                            },
                        },
                    },
                ),
                Tool(
                    name="chrome_evaluate",
                    description="Execute JavaScript code in the browser and return the result.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "description": "JavaScript expression to evaluate",
                            },
                        },
                        "required": ["expression"],
                    },
                ),
                Tool(
                    name="chrome_console",
                    description="Get console messages (logs, warnings, errors) from the browser.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "clear": {
                                "type": "boolean",
                                "description": "Clear messages after returning (default: false)",
                                "default": False,
                            },
                        },
                    },
                ),
                Tool(
                    name="chrome_network",
                    description="Get network requests made by the page.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "clear": {
                                "type": "boolean",
                                "description": "Clear requests after returning (default: false)",
                                "default": False,
                            },
                        },
                    },
                ),
                Tool(
                    name="chrome_wait",
                    description="Wait for an element to appear on the page.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector to wait for",
                            },
                            "timeout": {
                                "type": "number",
                                "description": "Maximum time to wait in seconds (default: 10)",
                                "default": 10,
                            },
                        },
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="chrome_scroll",
                    description="Scroll the page or an element.",
                    inputSchema={
                        "type": "object",
                        "properties": {
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
                        },
                        "required": ["direction"],
                    },
                ),
                Tool(
                    name="chrome_tabs",
                    description="List all open browser tabs.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name="chrome_new_tab",
                    description="Open a new browser tab.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "URL to open in the new tab (default: about:blank)",
                                "default": "about:blank",
                            },
                        },
                    },
                ),
                Tool(
                    name="chrome_close_tab",
                    description="Close a browser tab.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tab_id": {
                                "type": "string",
                                "description": "ID of the tab to close",
                            },
                        },
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="chrome_switch_tab",
                    description="Switch to a different browser tab.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tab_id": {
                                "type": "string",
                                "description": "ID of the tab to switch to",
                            },
                        },
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="chrome_pdf",
                    description="Generate a PDF of the current page.",
                    inputSchema={
                        "type": "object",
                        "properties": {
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
                        },
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[TextContent | ImageContent | EmbeddedResource]:
            """Handle tool calls."""
            try:
                # Ensure we have a connection
                await self._ensure_connected()

                if name == "chrome_navigate":
                    return await self._navigate(arguments["url"])
                elif name == "chrome_screenshot":
                    return await self._screenshot(
                        arguments.get("full_page", False),
                        arguments.get("format", "png"),
                    )
                elif name == "chrome_click":
                    return await self._click(arguments["selector"])
                elif name == "chrome_type":
                    return await self._type(
                        arguments["selector"],
                        arguments["text"],
                        arguments.get("clear_first", True),
                    )
                elif name == "chrome_get_html":
                    return await self._get_html(arguments.get("selector"))
                elif name == "chrome_evaluate":
                    return await self._evaluate(arguments["expression"])
                elif name == "chrome_console":
                    return await self._get_console(arguments.get("clear", False))
                elif name == "chrome_network":
                    return await self._get_network(arguments.get("clear", False))
                elif name == "chrome_wait":
                    return await self._wait_for(
                        arguments["selector"],
                        arguments.get("timeout", 10),
                    )
                elif name == "chrome_scroll":
                    return await self._scroll(
                        arguments["direction"],
                        arguments.get("amount", 500),
                        arguments.get("selector"),
                    )
                elif name == "chrome_tabs":
                    return await self._list_tabs()
                elif name == "chrome_new_tab":
                    return await self._new_tab(arguments.get("url", "about:blank"))
                elif name == "chrome_close_tab":
                    return await self._close_tab(arguments["tab_id"])
                elif name == "chrome_switch_tab":
                    return await self._switch_tab(arguments["tab_id"])
                elif name == "chrome_pdf":
                    return await self._generate_pdf(
                        arguments.get("landscape", False),
                        arguments.get("print_background", True),
                    )
                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]

            except Exception as e:
                logger.exception(f"Error in tool {name}")
                return [TextContent(type="text", text=f"Error: {str(e)}")]

    async def _ensure_connected(self) -> None:
        """Ensure we have a connected Chrome instance and CDP session."""
        if self._use_proxy and self._proxy is not None:
            return
        if self.session is not None:
            return

        port = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
        headless = False  # Always use headed mode (visible browser)
        user_data_dir = os.environ.get("CHROME_USER_DATA_DIR")

        # Check if we need proxy mode (WSL with network isolation)
        if is_wsl() and should_use_proxy():
            logger.info("WSL network isolation detected, using CDP proxy mode")
            await self._ensure_connected_proxy(port, headless, user_data_dir)
            return

        # Try direct connection
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
                '[System.IO.Path]::GetRandomFileName()); '
                'New-Item -ItemType Directory -Path $temp -Force | Out-Null; '
                'Write-Output $temp'
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
            find_chrome = '''
            $paths = @(
                "$env:PROGRAMFILES\\Google\\Chrome\\Application\\chrome.exe",
                "${env:PROGRAMFILES(x86)}\\Google\\Chrome\\Application\\chrome.exe",
                "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
            )
            foreach ($p in $paths) { if (Test-Path $p) { Write-Output $p; break } }
            '''
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

        # Get a page to work with
        targets = await self._proxy.list_targets()
        page_targets = [t for t in targets if t.get("type") == "page"]

        if page_targets:
            self._proxy_ws_url = page_targets[0].get("webSocketDebuggerUrl")
        else:
            new_page = await self._proxy.new_page()
            if new_page:
                self._proxy_ws_url = new_page.get("webSocketDebuggerUrl")

        if not self._proxy_ws_url:
            raise RuntimeError("Failed to get WebSocket URL for Chrome page")

        logger.info(f"Proxy WebSocket URL: {self._proxy_ws_url}")

    async def _ensure_connected_direct(
        self, port: int, headless: bool, user_data_dir: str | None
    ) -> None:
        """Connect directly to Chrome (non-WSL or WSL with working network)."""
        if self.launcher is None:
            self.launcher = ChromeLauncher(
                port=port,
                headless=headless,
                user_data_dir=user_data_dir,
            )

        # Connect or launch Chrome
        chrome = await self.launcher.connect_or_launch()
        logger.info(f"Connected to Chrome at {chrome.debugger_url}")

        # Create CDP client and session
        self.cdp = CDPClient(chrome.debugger_url)
        await self.cdp.__aenter__()

        _, self.session = await self.cdp.get_or_create_page()

        # Enable monitoring
        self._console_messages = await get_console_messages(self.session)
        self._network_requests = await get_network_requests(self.session)

        # Enable DOM
        await self.session.send("DOM.enable")

    async def _navigate(self, url: str) -> list[TextContent]:
        """Navigate to a URL."""
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            result = await self._proxy.navigate(self._proxy_ws_url, url)
            title = await self._proxy.evaluate(self._proxy_ws_url, "document.title")
        else:
            assert self.session is not None
            result = await navigate(self.session, url)
            title = await evaluate_javascript(self.session, "document.title")

        frame_id = result.get('frameId', 'unknown')
        return [
            TextContent(
                type="text",
                text=f"Navigated to: {url}\nTitle: {title}\nFrame ID: {frame_id}",
            )
        ]

    async def _screenshot(self, full_page: bool, format: str) -> list[ImageContent]:
        """Take a screenshot."""
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            image_data = await self._proxy.screenshot(
                self._proxy_ws_url, format=format, full_page=full_page
            )
        else:
            assert self.session is not None
            image_data = await take_screenshot(self.session, format=format, full_page=full_page)

        return [
            ImageContent(
                type="image",
                data=base64.b64encode(image_data).decode("utf-8"),
                mimeType=f"image/{format}",
            )
        ]

    async def _click(self, selector: str) -> list[TextContent]:
        """Click on an element."""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            el.click();
            return {{ success: true, tagName: el.tagName }};
        }})()
        """

        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            result = await self._proxy.evaluate(self._proxy_ws_url, js)
        else:
            assert self.session is not None
            result = await evaluate_javascript(self.session, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        tag = result.get('tagName', 'unknown') if isinstance(result, dict) else 'unknown'
        return [TextContent(type="text", text=f"Clicked on {selector} ({tag})")]

    async def _type(self, selector: str, text: str, clear_first: bool) -> list[TextContent]:
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
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            result = await self._proxy.evaluate(self._proxy_ws_url, js)
        else:
            assert self.session is not None
            result = await evaluate_javascript(self.session, js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        return [TextContent(type="text", text=f"Typed into {selector}")]

    async def _get_html(self, selector: str | None) -> list[TextContent]:
        """Get HTML content."""
        if selector:
            js = f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ error: 'Element not found: {selector}' }};
                return {{ html: el.outerHTML }};
            }})()
            """
            if self._use_proxy:
                assert self._proxy is not None and self._proxy_ws_url is not None
                result = await self._proxy.evaluate(self._proxy_ws_url, js)
            else:
                assert self.session is not None
                result = await evaluate_javascript(self.session, js)

            if isinstance(result, dict) and result.get("error"):
                return [TextContent(type="text", text=f"Error: {result['error']}")]
            html = result.get("html", "") if isinstance(result, dict) else ""
        else:
            if self._use_proxy:
                assert self._proxy is not None and self._proxy_ws_url is not None
                html = await self._proxy.get_html(self._proxy_ws_url)
            else:
                assert self.session is not None
                html = await get_document_html(self.session)

        return [TextContent(type="text", text=html)]

    async def _evaluate(self, expression: str) -> list[TextContent]:
        """Evaluate JavaScript."""
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            result = await self._proxy.evaluate(self._proxy_ws_url, expression)
        else:
            assert self.session is not None
            result = await evaluate_javascript(self.session, expression)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async def _get_console(self, clear: bool) -> list[TextContent]:
        """Get console messages."""
        # Note: Console monitoring not available in proxy mode
        messages = list(self._console_messages)
        if clear:
            self._console_messages.clear()

        if not messages:
            msg = "No console messages collected."
            if self._use_proxy:
                msg += " (Limited in proxy mode)"
            return [TextContent(type="text", text=msg)]

        formatted = []
        for msg in messages:
            formatted.append(f"[{msg.get('type', 'log').upper()}] {msg.get('text', '')}")

        return [TextContent(type="text", text="\n".join(formatted))]

    async def _get_network(self, clear: bool) -> list[TextContent]:
        """Get network requests."""
        # Note: Network monitoring not available in proxy mode
        requests = list(self._network_requests)
        if clear:
            self._network_requests.clear()

        if not requests:
            msg = "No network requests collected."
            if self._use_proxy:
                msg += " (Limited in proxy mode)"
            return [TextContent(type="text", text=msg)]

        formatted = []
        for req in requests:
            formatted.append(f"{req.get('method', 'GET')} {req.get('url', 'unknown')}")

        return [TextContent(type="text", text="\n".join(formatted))]

    async def _wait_for(self, selector: str, timeout: float) -> list[TextContent]:
        """Wait for an element to appear."""
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 0.5  # Longer interval for proxy mode

        while asyncio.get_event_loop().time() < deadline:
            js = f"document.querySelector({json.dumps(selector)}) !== null"
            if self._use_proxy:
                assert self._proxy is not None and self._proxy_ws_url is not None
                found = await self._proxy.evaluate(self._proxy_ws_url, js)
            else:
                assert self.session is not None
                found = await evaluate_javascript(self.session, js)
            if found:
                return [TextContent(type="text", text=f"Element found: {selector}")]
            await asyncio.sleep(poll_interval)

        msg = f"Timeout: Element not found after {timeout}s: {selector}"
        return [TextContent(type="text", text=msg)]

    async def _scroll(
        self, direction: str, amount: int, selector: str | None
    ) -> list[TextContent]:
        """Scroll the page or element."""
        target = (
            f"document.querySelector({json.dumps(selector)})" if selector else "window"
        )

        scroll_code = {
            "up": f"{target}.scrollBy(0, -{amount})",
            "down": f"{target}.scrollBy(0, {amount})",
            "left": f"{target}.scrollBy(-{amount}, 0)",
            "right": f"{target}.scrollBy({amount}, 0)",
            "top": f"{target}.scrollTo(0, 0)",
            "bottom": f"{target}.scrollTo(0, document.body.scrollHeight)",
        }

        js = scroll_code.get(direction, f"{target}.scrollBy(0, {amount})")
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            await self._proxy.evaluate(self._proxy_ws_url, js)
        else:
            assert self.session is not None
            await evaluate_javascript(self.session, js)

        return [TextContent(type="text", text=f"Scrolled {direction}")]

    async def _list_tabs(self) -> list[TextContent]:
        """List all tabs."""
        if self._use_proxy:
            assert self._proxy is not None
            targets = await self._proxy.list_targets()
            page_targets = [t for t in targets if t.get("type") == "page"]

            if not page_targets:
                return [TextContent(type="text", text="No tabs open.")]

            lines = ["Open tabs:"]
            for t in page_targets:
                ws_url = t.get("webSocketDebuggerUrl", "")
                is_current = ws_url and self._proxy_ws_url and ws_url in self._proxy_ws_url
                current = " (current)" if is_current else ""
                lines.append(
                    f"  - [{t.get('id', 'unknown')}] "
                    f"{t.get('title', 'Untitled')}: {t.get('url', '')}{current}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        assert self.cdp is not None
        targets = await self.cdp.list_targets()
        page_targets = [t for t in targets if t.type == "page"]

        if not page_targets:
            return [TextContent(type="text", text="No tabs open.")]

        lines = ["Open tabs:"]
        for t in page_targets:
            current = " (current)" if self.session and t.id == self.session.target.id else ""
            lines.append(f"  - [{t.id}] {t.title or 'Untitled'}: {t.url}{current}")

        return [TextContent(type="text", text="\n".join(lines))]

    async def _new_tab(self, url: str) -> list[TextContent]:
        """Open a new tab."""
        if self._use_proxy:
            assert self._proxy is not None
            target = await self._proxy.new_page(url)
            if target:
                return [TextContent(
                    type="text",
                    text=f"Opened new tab: [{target.get('id', 'unknown')}] {url}"
                )]
            return [TextContent(type="text", text="Failed to open new tab")]

        assert self.cdp is not None
        target = await self.cdp.new_page(url)
        return [TextContent(type="text", text=f"Opened new tab: [{target.id}] {url}")]

    async def _close_tab(self, tab_id: str) -> list[TextContent]:
        """Close a tab."""
        if self._use_proxy:
            assert self._proxy is not None
            success = await self._proxy.close_page(tab_id)
            if success:
                return [TextContent(type="text", text=f"Closed tab: {tab_id}")]
            return [TextContent(type="text", text=f"Failed to close tab: {tab_id}")]

        assert self.cdp is not None
        await self.cdp.close_page(tab_id)
        return [TextContent(type="text", text=f"Closed tab: {tab_id}")]

    async def _switch_tab(self, tab_id: str) -> list[TextContent]:
        """Switch to a different tab."""
        if self._use_proxy:
            assert self._proxy is not None
            targets = await self._proxy.list_targets()
            target = next((t for t in targets if t.get("id") == tab_id), None)

            if not target:
                return [TextContent(type="text", text=f"Tab not found: {tab_id}")]

            self._proxy_ws_url = target.get("webSocketDebuggerUrl")
            return [TextContent(
                type="text",
                text=f"Switched to tab: {target.get('title', tab_id)}"
            )]

        assert self.cdp is not None
        targets = await self.cdp.list_targets()
        target = next((t for t in targets if t.id == tab_id), None)

        if not target:
            return [TextContent(type="text", text=f"Tab not found: {tab_id}")]

        self.session = await self.cdp.connect_to_target(target)

        # Re-enable monitoring for new session
        self._console_messages = await get_console_messages(self.session)
        self._network_requests = await get_network_requests(self.session)
        await self.session.send("DOM.enable")

        return [TextContent(type="text", text=f"Switched to tab: {target.title or tab_id}")]

    async def _generate_pdf(
        self, landscape: bool, print_background: bool
    ) -> list[EmbeddedResource]:
        """Generate a PDF of the page."""
        if self._use_proxy:
            assert self._proxy is not None and self._proxy_ws_url is not None
            result = await self._proxy.send_cdp_command(
                self._proxy_ws_url,
                "Page.printToPDF",
                {
                    "landscape": landscape,
                    "printBackground": print_background,
                    "preferCSSPageSize": True,
                },
            )
        else:
            assert self.session is not None
            result = await self.session.send(
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
        if self.cdp:
            await self.cdp.close()
            self.cdp = None

        if self.launcher:
            await self.launcher.close()
            self.launcher = None

        self.session = None
        self.chrome = None


def main() -> None:
    """Main entry point."""
    server = ChromeMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
