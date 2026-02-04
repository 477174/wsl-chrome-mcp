"""CDP Proxy - Routes Chrome DevTools Protocol requests through PowerShell.

This module provides a workaround for WSL2 networking isolation by proxying
all HTTP and WebSocket CDP requests through PowerShell on Windows.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from .wsl import is_wsl, run_windows_command

logger = logging.getLogger(__name__)


class CDPProxyClient:
    """CDP client that proxies requests through PowerShell for WSL compatibility."""

    def __init__(self, port: int = 9222) -> None:
        """Initialize the proxy client.

        Args:
            port: Chrome debugging port on Windows localhost.
        """
        self.port = port
        self._ws_messages: dict[str, list[str]] = {}

    def _make_http_request(self, path: str, method: str = "GET") -> dict[str, Any] | list[Any] | None:
        """Make an HTTP request to Chrome via PowerShell.

        Args:
            path: URL path (e.g., "/json/version")
            method: HTTP method

        Returns:
            Parsed JSON response or None on error.
        """
        ps_cmd = f'''
        try {{
            $response = Invoke-WebRequest -Uri "http://localhost:{self.port}{path}" -Method {method} -UseBasicParsing -TimeoutSec 10
            Write-Output $response.Content
        }} catch {{
            Write-Error $_.Exception.Message
            exit 1
        }}
        '''

        try:
            result = run_windows_command(ps_cmd, timeout=15.0)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception as e:
            logger.error(f"HTTP request failed: {e}")

        return None

    async def get_version(self) -> dict[str, Any] | None:
        """Get Chrome version info."""
        return self._make_http_request("/json/version")

    async def list_targets(self) -> list[dict[str, Any]]:
        """List available debugging targets."""
        result = self._make_http_request("/json/list")
        return result if isinstance(result, list) else []

    async def new_page(self, url: str = "about:blank") -> dict[str, Any] | None:
        """Create a new page."""
        return self._make_http_request(f"/json/new?{url}", method="PUT")

    async def close_page(self, target_id: str) -> bool:
        """Close a page."""
        result = self._make_http_request(f"/json/close/{target_id}")
        return result is not None

    async def send_cdp_command(
        self,
        ws_url: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a CDP command via WebSocket through PowerShell.

        Args:
            ws_url: WebSocket URL for the target
            method: CDP method name
            params: Optional parameters
            timeout: Command timeout

        Returns:
            CDP response result
        """
        # Build the CDP message
        message = {"id": 1, "method": method}
        if params:
            message["params"] = params

        message_json = json.dumps(message).replace('"', '`"')

        # PowerShell script to send WebSocket message and get response
        ps_script = f'''
        $ws = New-Object System.Net.WebSockets.ClientWebSocket
        $uri = [System.Uri]::new("{ws_url}")
        $ct = [System.Threading.CancellationToken]::None

        try {{
            $null = $ws.ConnectAsync($uri, $ct).GetAwaiter().GetResult()

            # Send message
            $message = "{message_json}"
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($message)
            $segment = [System.ArraySegment[byte]]::new($bytes)
            $null = $ws.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).GetAwaiter().GetResult()

            # Receive response
            $buffer = New-Object byte[] 65536
            $result = ""
            do {{
                $segment = [System.ArraySegment[byte]]::new($buffer)
                $received = $ws.ReceiveAsync($segment, $ct).GetAwaiter().GetResult()
                $result += [System.Text.Encoding]::UTF8.GetString($buffer, 0, $received.Count)
            }} while (-not $received.EndOfMessage)

            Write-Output $result
        }} finally {{
            if ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {{
                $null = $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", $ct).GetAwaiter().GetResult()
            }}
            $ws.Dispose()
        }}
        '''

        try:
            result = run_windows_command(ps_script, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout.strip():
                response = json.loads(result.stdout.strip())
                if "error" in response:
                    raise RuntimeError(f"CDP error: {response['error']}")
                return response.get("result", {})
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse CDP response: {e}")
            raise RuntimeError(f"Invalid CDP response: {result.stdout[:200] if result else 'empty'}")
        except Exception as e:
            logger.error(f"CDP command failed: {e}")
            raise

        raise RuntimeError("CDP command failed with no response")

    async def navigate(self, ws_url: str, url: str) -> dict[str, Any]:
        """Navigate to a URL."""
        await self.send_cdp_command(ws_url, "Page.enable")
        return await self.send_cdp_command(ws_url, "Page.navigate", {"url": url})

    async def screenshot(
        self, ws_url: str, format: str = "png", full_page: bool = False
    ) -> bytes:
        """Take a screenshot."""
        params: dict[str, Any] = {"format": format}

        if full_page:
            layout = await self.send_cdp_command(ws_url, "Page.getLayoutMetrics")
            content_size = layout.get("contentSize", {})
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": content_size.get("width", 1920),
                "height": content_size.get("height", 1080),
                "scale": 1,
            }
            params["captureBeyondViewport"] = True

        result = await self.send_cdp_command(ws_url, "Page.captureScreenshot", params)
        return base64.b64decode(result["data"])

    async def evaluate(self, ws_url: str, expression: str) -> Any:
        """Evaluate JavaScript."""
        result = await self.send_cdp_command(
            ws_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    async def get_html(self, ws_url: str) -> str:
        """Get page HTML."""
        await self.send_cdp_command(ws_url, "DOM.enable")
        doc = await self.send_cdp_command(ws_url, "DOM.getDocument", {"depth": -1})
        root_id = doc["root"]["nodeId"]
        result = await self.send_cdp_command(ws_url, "DOM.getOuterHTML", {"nodeId": root_id})
        return result["outerHTML"]


def should_use_proxy() -> bool:
    """Check if we should use the CDP proxy (WSL with network isolation)."""
    if not is_wsl():
        return False

    # Test if we can reach localhost:9222 directly
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "--connect-timeout", "1", "http://localhost:9222/json/version"],
            capture_output=True,
            timeout=3,
        )
        return result.returncode != 0
    except Exception:
        return True
