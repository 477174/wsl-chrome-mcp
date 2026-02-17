"""Persistent Chrome DevTools Protocol client with event support.

This module provides a CDP client that maintains a persistent WebSocket
connection, enabling real-time event handling for console messages,
network requests, dialogs, and performance traces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


@runtime_checkable
class CDPClientProtocol(Protocol):
    """Minimal interface shared by PersistentCDPClient and PowerShellCDPRelay."""

    @property
    def is_connected(self) -> bool: ...

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = ...,
        timeout: float | None = ...,
    ) -> dict[str, Any]: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    def on(self, event: str, handler: EventHandler) -> None: ...

    def off(self, event: str, handler: EventHandler | None = ...) -> None: ...


class CDPError(Exception):
    """Error from Chrome DevTools Protocol."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class CDPTarget:
    """Represents a CDP target (page, worker, etc.)."""

    id: str
    type: str
    title: str
    url: str
    websocket_url: str
    description: str = ""


class PersistentCDPClient:
    """CDP client with persistent WebSocket for event handling.

    Unlike the proxy-based client that opens/closes connections per command,
    this client maintains a persistent WebSocket connection, enabling:
    - Real-time event subscriptions (console, network, dialogs)
    - Lower latency for rapid command sequences
    - Proper async event handling

    Example:
        client = PersistentCDPClient("ws://localhost:9222/devtools/page/...")
        await client.connect()

        # Subscribe to events
        client.on("Runtime.consoleAPICalled", handle_console)
        await client.send("Runtime.enable")

        # Send commands
        result = await client.send("Page.navigate", {"url": "https://example.com"})

        await client.disconnect()
    """

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        """Initialize the CDP client.

        Args:
            ws_url: WebSocket URL for the target.
            timeout: Default timeout for commands in seconds.
        """
        self.ws_url = ws_url
        self.timeout = timeout

        self._ws: ClientConnection | None = None
        self._message_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._receive_task: asyncio.Task[None] | None = None
        self._connected = False
        self._reconnecting = False

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected and self._ws is not None

    async def connect(self) -> None:
        """Establish persistent WebSocket connection.

        Raises:
            RuntimeError: If already connected.
            ConnectionError: If connection fails.
        """
        if self._connected:
            logger.warning("Already connected to %s", self.ws_url)
            return

        logger.debug("Connecting to %s", self.ws_url)

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                max_size=100 * 1024 * 1024,  # 100MB for large traces
                close_timeout=5,
            )
            self._connected = True
            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info("Connected to CDP endpoint: %s", self.ws_url)

        except Exception as e:
            logger.error("Failed to connect to %s: %s", self.ws_url, e)
            raise ConnectionError(f"Failed to connect to CDP: {e}") from e

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if not self._connected:
            return

        logger.debug("Disconnecting from %s", self.ws_url)
        self._connected = False

        # Cancel receive task
        if self._receive_task:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        # Cancel pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection closed"))
        self._pending.clear()

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.warning("Error closing WebSocket: %s", e)
            self._ws = None

        logger.info("Disconnected from CDP endpoint")

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a CDP command and wait for response.

        Args:
            method: CDP method name (e.g., "Page.navigate").
            params: Optional parameters for the method.
            timeout: Override default timeout for this command.

        Returns:
            The result from Chrome.

        Raises:
            CDPError: If Chrome returns an error.
            ConnectionError: If not connected.
            asyncio.TimeoutError: If command times out.
        """
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected to CDP endpoint")

        self._message_id += 1
        msg_id = self._message_id

        message: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        # Create future for response
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(json.dumps(message))
            result = await asyncio.wait_for(future, timeout=timeout or self.timeout)
            return result

        except asyncio.TimeoutError as err:
            self._pending.pop(msg_id, None)
            raise asyncio.TimeoutError(f"Timeout waiting for {method}") from err

        except Exception:
            self._pending.pop(msg_id, None)
            raise

    def on(self, event: str, handler: EventHandler) -> None:
        """Subscribe to a CDP event.

        Args:
            event: Event name (e.g., "Runtime.consoleAPICalled").
            handler: Async or sync function to call when event occurs.
        """
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)
        logger.debug("Registered handler for event: %s", event)

    def off(self, event: str, handler: EventHandler | None = None) -> None:
        """Unsubscribe from a CDP event.

        Args:
            event: Event name to unsubscribe from.
            handler: Specific handler to remove, or None to remove all.
        """
        if event not in self._event_handlers:
            return

        if handler is None:
            del self._event_handlers[event]
        else:
            self._event_handlers[event] = [h for h in self._event_handlers[event] if h != handler]

    async def _receive_loop(self) -> None:
        """Process incoming WebSocket messages."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.warning("Invalid JSON from CDP: %s", e)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("WebSocket connection closed: %s", e)
            self._connected = False

        except asyncio.CancelledError:
            pass

        except Exception as e:
            logger.error("Error in receive loop: %s", e)
            self._connected = False

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Handle a single CDP message."""
        if "id" in data:
            # Response to a command
            msg_id = data["id"]
            if msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if "error" in data:
                    error = data["error"]
                    future.set_exception(
                        CDPError(error.get("message", "Unknown error"), error.get("code"))
                    )
                else:
                    future.set_result(data.get("result", {}))

        elif "method" in data:
            # Event from Chrome
            event = data["method"]
            params = data.get("params", {})
            await self._dispatch_event(event, params)

    async def _dispatch_event(self, event: str, params: dict[str, Any]) -> None:
        """Dispatch an event to registered handlers."""
        handlers = self._event_handlers.get(event, [])

        for handler in handlers:
            try:
                result = handler(params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("Event handler error for %s: %s", event, e)

    async def __aenter__(self) -> PersistentCDPClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()


class CDPConnection:
    """High-level CDP connection manager for a Chrome instance.

    Manages both page-level and browser-level CDP connections,
    providing a unified interface for commands and events.
    """

    def __init__(self, debugger_url: str) -> None:
        """Initialize the connection manager.

        Args:
            debugger_url: Chrome's HTTP debugging URL (e.g., http://localhost:9222).
        """
        self.debugger_url = debugger_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._browser_client: PersistentCDPClient | None = None
        self._page_clients: dict[str, PersistentCDPClient] = {}  # target_id -> client

    async def __aenter__(self) -> CDPConnection:
        """Async context manager entry."""
        self._http = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close all connections."""
        # Close page clients
        for client in list(self._page_clients.values()):
            await client.disconnect()
        self._page_clients.clear()

        # Close browser client
        if self._browser_client:
            await self._browser_client.disconnect()
            self._browser_client = None

        # Close HTTP client
        if self._http:
            await self._http.aclose()
            self._http = None

    async def get_version(self) -> dict[str, Any]:
        """Get Chrome version information."""
        if not self._http:
            raise RuntimeError("Connection not initialized")

        response = await self._http.get(f"{self.debugger_url}/json/version")
        response.raise_for_status()
        return response.json()

    async def list_targets(self) -> list[CDPTarget]:
        """List available debugging targets."""
        if not self._http:
            raise RuntimeError("Connection not initialized")

        response = await self._http.get(f"{self.debugger_url}/json/list")
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

    async def get_browser_client(self) -> PersistentCDPClient:
        """Get or create the browser-level CDP client.

        Used for Target.*, Browser.* domain commands.
        """
        if self._browser_client and self._browser_client.is_connected:
            return self._browser_client

        version = await self.get_version()
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome did not return browser WebSocket URL")

        self._browser_client = PersistentCDPClient(ws_url)
        await self._browser_client.connect()
        return self._browser_client

    async def get_page_client(self, target_id: str) -> PersistentCDPClient:
        """Get or create a page-level CDP client.

        Args:
            target_id: The target ID to connect to.

        Returns:
            Connected PersistentCDPClient for the page.
        """
        if target_id in self._page_clients:
            client = self._page_clients[target_id]
            if client.is_connected:
                return client

        # Find target
        targets = await self.list_targets()
        target = next((t for t in targets if t.id == target_id), None)

        if not target:
            raise ValueError(f"Target not found: {target_id}")

        client = PersistentCDPClient(target.websocket_url)
        await client.connect()
        self._page_clients[target_id] = client
        return client

    async def remove_page_client(self, target_id: str) -> None:
        """Remove and disconnect a page client.

        Args:
            target_id: The target ID to disconnect.
        """
        if target_id in self._page_clients:
            client = self._page_clients.pop(target_id)
            await client.disconnect()

    async def new_page(self, url: str = "about:blank") -> CDPTarget:
        """Create a new page.

        Args:
            url: Initial URL to load.

        Returns:
            CDPTarget for the new page.
        """
        if not self._http:
            raise RuntimeError("Connection not initialized")

        response = await self._http.put(f"{self.debugger_url}/json/new?{url}")
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
        """Close a page.

        Args:
            target_id: The target ID to close.
        """
        if not self._http:
            raise RuntimeError("Connection not initialized")

        # Disconnect client first
        await self.remove_page_client(target_id)

        response = await self._http.get(f"{self.debugger_url}/json/close/{target_id}")
        response.raise_for_status()


# Convenience functions for common CDP operations


async def enable_domains(
    client: CDPClientProtocol,
    domains: list[str] | None = None,
) -> None:
    """Enable common CDP domains for event collection.

    Args:
        client: The CDP client to configure.
        domains: List of domains to enable, or None for defaults.
    """
    if domains is None:
        domains = ["Page", "Runtime", "Network", "DOM"]

    for domain in domains:
        try:
            await client.send(f"{domain}.enable")
            logger.debug("Enabled CDP domain: %s", domain)
        except CDPError as e:
            logger.warning("Failed to enable %s domain: %s", domain, e)


async def navigate(
    client: PersistentCDPClient,
    url: str,
    wait_until: str = "load",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Navigate to a URL and wait for load.

    Args:
        client: The CDP client.
        url: URL to navigate to.
        wait_until: Event to wait for ("load" or "domcontentloaded").
        timeout: Maximum time to wait.

    Returns:
        Navigation result.
    """
    await client.send("Page.enable")

    load_event = asyncio.Event()

    event_name = "Page.loadEventFired" if wait_until == "load" else "Page.domContentEventFired"

    def on_load(params: dict[str, Any]) -> None:
        load_event.set()

    client.on(event_name, on_load)

    try:
        result = await client.send("Page.navigate", {"url": url})

        try:
            await asyncio.wait_for(load_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for %s event", wait_until)

        return result

    finally:
        client.off(event_name, on_load)
