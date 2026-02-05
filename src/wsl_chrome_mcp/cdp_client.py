"""Chrome DevTools Protocol (CDP) client for communicating with Chrome."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


@dataclass
class CDPTarget:
    """Represents a CDP target (tab/page)."""

    id: str
    type: str
    title: str
    url: str
    websocket_url: str
    description: str = ""


@dataclass
class CDPSession:
    """A CDP session connected to a specific target."""

    websocket: ClientConnection
    target: CDPTarget
    _message_id: int = field(default=0, repr=False)
    _pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict, repr=False)
    _event_handlers: dict[str, list[Any]] = field(default_factory=dict, repr=False)
    _receive_task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def start(self) -> None:
        """Start receiving messages from the WebSocket."""
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def close(self) -> None:
        """Close this CDP session."""
        if self._receive_task:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
        await self.websocket.close()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a CDP command and wait for the result.

        Args:
            method: CDP method name (e.g., "Page.navigate").
            params: Optional parameters for the method.

        Returns:
            The result from Chrome.

        Raises:
            CDPError: If Chrome returns an error.
        """
        self._message_id += 1
        msg_id = self._message_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        # Create a future for the response
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self.websocket.send(json.dumps(message))
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError as err:
            self._pending.pop(msg_id, None)
            raise CDPError(f"Timeout waiting for response to {method}") from err
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    def on(self, event: str, handler: Any) -> None:
        """Register an event handler.

        Args:
            event: Event name (e.g., "Network.requestWillBeSent").
            handler: Async function to call when event occurs.
        """
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def _receive_loop(self) -> None:
        """Continuously receive messages from WebSocket."""
        try:
            async for message in self.websocket:
                data = json.loads(message)

                # Handle response to a command
                if "id" in data:
                    msg_id = data["id"]
                    if msg_id in self._pending:
                        future = self._pending.pop(msg_id)
                        if "error" in data:
                            future.set_exception(
                                CDPError(data["error"].get("message", "Unknown error"))
                            )
                        else:
                            future.set_result(data.get("result", {}))

                # Handle event
                elif "method" in data:
                    event = data["method"]
                    params = data.get("params", {})
                    handlers = self._event_handlers.get(event, [])
                    for handler in handlers:
                        try:
                            await handler(params)
                        except Exception as e:
                            logger.warning(f"Event handler error for {event}: {e}")

        except websockets.exceptions.ConnectionClosed:
            logger.debug("WebSocket connection closed")
        except asyncio.CancelledError:
            pass


class CDPError(Exception):
    """Error from Chrome DevTools Protocol."""

    pass


class CDPClient:
    """Client for interacting with Chrome via CDP."""

    def __init__(self, debugger_url: str) -> None:
        """Initialize the CDP client.

        Args:
            debugger_url: Chrome's debugging URL (e.g., http://localhost:9222).
        """
        self.debugger_url = debugger_url.rstrip("/")
        self._http_client: httpx.AsyncClient | None = None
        self._sessions: dict[str, CDPSession] = {}

    async def __aenter__(self) -> CDPClient:
        """Enter async context."""
        self._http_client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context and cleanup."""
        await self.close()

    async def close(self) -> None:
        """Close all sessions and connections."""
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def list_targets(self) -> list[CDPTarget]:
        """List all available CDP targets (tabs, pages, etc.).

        Returns:
            List of CDPTarget objects.
        """
        if not self._http_client:
            raise RuntimeError("CDPClient not initialized. Use async context manager.")

        response = await self._http_client.get(f"{self.debugger_url}/json/list")
        response.raise_for_status()

        targets = []
        for item in response.json():
            if "webSocketDebuggerUrl" in item:
                targets.append(
                    CDPTarget(
                        id=item["id"],
                        type=item.get("type", "page"),
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        websocket_url=item["webSocketDebuggerUrl"],
                        description=item.get("description", ""),
                    )
                )
        return targets

    async def get_version(self) -> dict[str, Any]:
        """Get Chrome version information.

        Returns:
            Dict with version info.
        """
        if not self._http_client:
            raise RuntimeError("CDPClient not initialized. Use async context manager.")

        response = await self._http_client.get(f"{self.debugger_url}/json/version")
        response.raise_for_status()
        return response.json()

    async def new_page(self, url: str = "about:blank") -> CDPTarget:
        """Create a new page/tab.

        Args:
            url: Initial URL to load.

        Returns:
            CDPTarget for the new page.
        """
        if not self._http_client:
            raise RuntimeError("CDPClient not initialized. Use async context manager.")

        response = await self._http_client.put(f"{self.debugger_url}/json/new?{url}")
        response.raise_for_status()
        item = response.json()

        return CDPTarget(
            id=item["id"],
            type=item.get("type", "page"),
            title=item.get("title", ""),
            url=item.get("url", url),
            websocket_url=item["webSocketDebuggerUrl"],
        )

    async def close_page(self, target_id: str) -> None:
        """Close a page/tab.

        Args:
            target_id: ID of the target to close.
        """
        if not self._http_client:
            raise RuntimeError("CDPClient not initialized. Use async context manager.")

        # Close session if exists
        if target_id in self._sessions:
            await self._sessions[target_id].close()
            del self._sessions[target_id]

        response = await self._http_client.get(f"{self.debugger_url}/json/close/{target_id}")
        response.raise_for_status()

    async def connect_to_target(self, target: CDPTarget) -> CDPSession:
        """Connect to a specific target via WebSocket.

        Args:
            target: The CDPTarget to connect to.

        Returns:
            CDPSession for interacting with the target.
        """
        if target.id in self._sessions:
            return self._sessions[target.id]

        websocket = await websockets.connect(target.websocket_url)
        session = CDPSession(websocket=websocket, target=target)
        await session.start()

        self._sessions[target.id] = session
        return session

    async def connect_to_browser(self) -> CDPSession:
        """Connect to the browser-level WebSocket endpoint.

        This session is used for browser-wide commands like Target.createTarget,
        Browser.getWindowForTarget, and Target.activateTarget. Unlike page sessions,
        this connects to the /json/version webSocketDebuggerUrl.

        Returns:
            CDPSession connected to the browser endpoint.
        """
        if "_browser" in self._sessions:
            return self._sessions["_browser"]

        version = await self.get_version()
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome did not return browser webSocketDebuggerUrl")

        websocket = await websockets.connect(ws_url)
        # Create a synthetic target for the browser session
        browser_target = CDPTarget(
            id="_browser",
            type="browser",
            title="Browser",
            url="",
            websocket_url=ws_url,
        )
        session = CDPSession(websocket=websocket, target=browser_target)
        await session.start()

        self._sessions["_browser"] = session
        return session

    async def get_or_create_page(self) -> tuple[CDPTarget, CDPSession]:
        """Get an existing page or create a new one.

        Returns:
            Tuple of (target, session) for a usable page.
        """
        targets = await self.list_targets()
        page_targets = [t for t in targets if t.type == "page"]

        if page_targets:
            target = page_targets[0]
        else:
            target = await self.new_page()

        session = await self.connect_to_target(target)
        return target, session


# High-level helper functions for common CDP operations


async def navigate(session: CDPSession, url: str, wait_until: str = "load") -> dict[str, Any]:
    """Navigate to a URL and wait for load.

    Args:
        session: CDP session.
        url: URL to navigate to.
        wait_until: Event to wait for ("load", "domcontentloaded", etc.).

    Returns:
        Navigation result.
    """
    # Enable Page events
    await session.send("Page.enable")

    # Create a future to wait for the load event
    load_future: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    event_name = "Page.loadEventFired" if wait_until == "load" else "Page.domContentEventFired"

    async def on_load(params: dict[str, Any]) -> None:
        if not load_future.done():
            load_future.set_result(None)

    session.on(event_name, on_load)

    # Navigate
    result = await session.send("Page.navigate", {"url": url})

    # Wait for load (with timeout)
    try:
        await asyncio.wait_for(load_future, timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning(f"Timeout waiting for {wait_until} event")

    return result


async def take_screenshot(
    session: CDPSession,
    format: str = "png",
    quality: int | None = None,
    full_page: bool = False,
) -> bytes:
    """Take a screenshot of the page.

    Args:
        session: CDP session.
        format: Image format ("png" or "jpeg").
        quality: Quality for jpeg (0-100).
        full_page: Whether to capture the full page.

    Returns:
        Screenshot as bytes.
    """
    params: dict[str, Any] = {"format": format}

    if quality is not None and format == "jpeg":
        params["quality"] = quality

    if full_page:
        # Get full page dimensions
        layout = await session.send("Page.getLayoutMetrics")
        content_size = layout.get("contentSize", {})
        params["clip"] = {
            "x": 0,
            "y": 0,
            "width": content_size.get("width", 1920),
            "height": content_size.get("height", 1080),
            "scale": 1,
        }
        params["captureBeyondViewport"] = True

    result = await session.send("Page.captureScreenshot", params)
    return base64.b64decode(result["data"])


async def get_document_html(session: CDPSession) -> str:
    """Get the HTML content of the page.

    Args:
        session: CDP session.

    Returns:
        HTML content as string.
    """
    result = await session.send("DOM.getDocument", {"depth": -1, "pierce": True})
    root_node_id = result["root"]["nodeId"]
    html_result = await session.send("DOM.getOuterHTML", {"nodeId": root_node_id})
    return html_result["outerHTML"]


async def evaluate_javascript(session: CDPSession, expression: str) -> Any:
    """Evaluate JavaScript in the page context.

    Args:
        session: CDP session.
        expression: JavaScript expression to evaluate.

    Returns:
        Result of the evaluation.
    """
    result = await session.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )

    if "exceptionDetails" in result:
        raise CDPError(f"JavaScript error: {result['exceptionDetails']}")

    return result.get("result", {}).get("value")


async def get_console_messages(session: CDPSession) -> list[dict[str, Any]]:
    """Enable and collect console messages.

    Args:
        session: CDP session.

    Returns:
        List of console message objects.
    """
    messages: list[dict[str, Any]] = []

    async def on_message(params: dict[str, Any]) -> None:
        messages.append(
            {
                "type": params.get("type", "log"),
                "text": params.get("args", [{}])[0].get("value", ""),
                "timestamp": params.get("timestamp"),
            }
        )

    session.on("Runtime.consoleAPICalled", on_message)
    await session.send("Runtime.enable")

    return messages


async def get_network_requests(session: CDPSession) -> list[dict[str, Any]]:
    """Enable network monitoring and return collected requests.

    Args:
        session: CDP session.

    Returns:
        List of network request objects.
    """
    requests: list[dict[str, Any]] = []

    async def on_request(params: dict[str, Any]) -> None:
        request = params.get("request", {})
        requests.append(
            {
                "request_id": params.get("requestId"),
                "url": request.get("url"),
                "method": request.get("method"),
                "headers": request.get("headers", {}),
                "timestamp": params.get("timestamp"),
            }
        )

    session.on("Network.requestWillBeSent", on_request)
    await session.send("Network.enable")

    return requests
