"""Proxy session manager for multi-session Chrome window isolation in WSL.

Each opencode chat session gets its own Chrome window so agents
can operate on their own tabs without interfering with each other.

This module mirrors SessionManager but uses CDPProxyClient for all
CDP commands, enabling session isolation in WSL environments with
network isolation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .cdp_proxy import CDPProxyClient

logger = logging.getLogger(__name__)


@dataclass
class ProxySessionState:
    """State for a single chat session's Chrome window (proxy mode).

    Each session owns one Chrome window and tracks its tabs independently.
    Unlike SessionState, this uses WebSocket URLs instead of CDPSession objects
    since proxy mode doesn't maintain persistent WebSocket connections.
    """

    session_id: str
    window_id: int | None = None
    current_target_id: str | None = None
    targets: list[str] = field(default_factory=list)
    ws_urls: dict[str, str] = field(default_factory=dict)  # target_id -> WebSocket URL
    # Console/network not available in proxy mode (requires persistent connection)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current_ws_url(self) -> str | None:
        """Get the WebSocket URL for the current (active) tab."""
        if self.current_target_id and self.current_target_id in self.ws_urls:
            return self.ws_urls[self.current_target_id]
        return None


class ProxySessionManager:
    """Manages multiple Chrome sessions in proxy mode, one window per opencode session.

    Routes tool calls to the correct Chrome window/tab based on session_id.
    Creates new windows on demand when a new session_id is first seen.

    Uses Target.createTarget(newWindow=true) as primary strategy.
    Falls back to window.open() via JavaScript if Chrome ignores newWindow.

    All CDP commands are sent through CDPProxyClient (PowerShell WebSocket proxy).
    """

    def __init__(self, proxy: CDPProxyClient) -> None:
        """Initialize the proxy session manager.

        Args:
            proxy: CDPProxyClient for sending CDP commands via PowerShell.
        """
        self._proxy = proxy
        self._sessions: dict[str, ProxySessionState] = {}
        self._browser_ws_url: str | None = None

    async def _ensure_browser_ws_url(self) -> str:
        """Get or fetch the browser-level WebSocket URL.

        Returns:
            Browser WebSocket URL for Target/Browser domain commands.

        Raises:
            RuntimeError: If browser WebSocket URL cannot be obtained.
        """
        if self._browser_ws_url is not None:
            return self._browser_ws_url

        self._browser_ws_url = await self._proxy.get_browser_ws_url()
        if self._browser_ws_url is None:
            raise RuntimeError("Failed to get browser WebSocket URL from Chrome")

        logger.info("Browser WebSocket URL: %s", self._browser_ws_url)
        return self._browser_ws_url

    def _get_existing_window_ids(self) -> set[int]:
        """Get window IDs already claimed by existing sessions."""
        return {s.window_id for s in self._sessions.values() if s.window_id is not None}

    def _get_any_existing_page_ws_url(self) -> str | None:
        """Get any existing page WebSocket URL for running JS fallback."""
        for state in self._sessions.values():
            if state.current_ws_url is not None:
                return state.current_ws_url
        return None

    async def _get_window_id_for_target(self, target_id: str) -> int | None:
        """Query the window ID that a target belongs to.

        Args:
            target_id: The CDP target ID.

        Returns:
            The Chrome window ID, or None if the query fails.
        """
        browser_ws = await self._ensure_browser_ws_url()
        try:
            info = await self._proxy.send_cdp_command(
                browser_ws,
                "Browser.getWindowForTarget",
                {"targetId": target_id},
            )
            window_id = info.get("windowId")
            logger.debug(
                "Browser.getWindowForTarget(%s) -> windowId=%s",
                target_id,
                window_id,
            )
            return window_id
        except Exception as e:
            logger.warning(
                "Browser.getWindowForTarget failed for %s: %s",
                target_id,
                e,
            )
            return None

    async def _create_target_via_cdp(self) -> tuple[str, int | None]:
        """Try to create a target in a new window via Target.createTarget.

        Returns:
            Tuple of (target_id, window_id).
        """
        browser_ws = await self._ensure_browser_ws_url()

        result = await self._proxy.send_cdp_command(
            browser_ws,
            "Target.createTarget",
            {"url": "about:blank", "newWindow": True},
        )
        target_id = result["targetId"]
        logger.info(
            "Target.createTarget(newWindow=true) -> targetId=%s",
            target_id,
        )

        window_id = await self._get_window_id_for_target(target_id)
        logger.info("New target %s is in windowId=%s", target_id, window_id)

        return target_id, window_id

    async def _create_target_via_window_open(self, page_ws_url: str) -> tuple[str, int | None]:
        """Create a target in a new window using window.open() fallback.

        Executes window.open('about:blank', '_blank', 'popup') on an
        existing page. The 'popup' feature forces Chrome to open a new
        window rather than a tab.

        Args:
            page_ws_url: WebSocket URL of an existing page to run JS on.

        Returns:
            Tuple of (target_id, window_id).

        Raises:
            RuntimeError: If the new target cannot be detected.
        """
        logger.info("Fallback: creating window via window.open()")

        # Snapshot current targets
        before_targets = await self._proxy.list_targets()
        before = {t["id"] for t in before_targets if t.get("type") == "page"}
        logger.debug("Targets before window.open(): %s", before)

        # Open a popup window via JavaScript
        await self._proxy.evaluate(
            page_ws_url,
            "window.open('about:blank', '_blank', 'popup')",
        )

        # Poll for the new target (it may take a moment to appear)
        # Note: In proxy mode, each poll is a PowerShell HTTP request, so we
        # use longer intervals than direct mode
        new_target_id: str | None = None
        for attempt in range(15):
            await asyncio.sleep(0.5)
            after_targets = await self._proxy.list_targets()
            after = {t["id"] for t in after_targets if t.get("type") == "page"}
            new_ids = after - before
            if new_ids:
                new_target_id = new_ids.pop()
                logger.info(
                    "Fallback: detected new target %s (attempt %d)",
                    new_target_id,
                    attempt + 1,
                )
                break

        if new_target_id is None:
            raise RuntimeError(
                "Fallback failed: no new target appeared after "
                "window.open(). Check popup blocker settings."
            )

        window_id = await self._get_window_id_for_target(new_target_id)
        logger.info(
            "Fallback target %s is in windowId=%s",
            new_target_id,
            window_id,
        )

        return new_target_id, window_id

    async def _get_ws_url_for_target(self, target_id: str) -> str | None:
        """Get the WebSocket URL for a target from /json/list.

        Args:
            target_id: The target ID to look up.

        Returns:
            WebSocket debugger URL or None if not found.
        """
        targets = await self._proxy.list_targets()
        for t in targets:
            if t.get("id") == target_id:
                return t.get("webSocketDebuggerUrl")
        return None

    async def get_or_create(self, session_id: str) -> ProxySessionState:
        """Get an existing session or create a new one with its own window.

        Strategy:
        1. Try Target.createTarget(newWindow=true) via CDP.
        2. Check if the resulting windowId is unique (not already used).
        3. If duplicate (Chrome ignored newWindow), fall back to
           window.open('about:blank', '_blank', 'popup') via JS on
           an existing page, which reliably opens a popup window.
        4. First session has no other sessions to compare against,
           so it always accepts the result from step 1.

        Args:
            session_id: The opencode session identifier.

        Returns:
            ProxySessionState for the requested session.
        """
        if session_id in self._sessions:
            logger.debug("Returning cached session: %s", session_id)
            return self._sessions[session_id]

        logger.info("Creating new session: %s", session_id)

        existing_windows = self._get_existing_window_ids()
        logger.debug("Existing window IDs: %s", existing_windows)

        # Step 1: Try Target.createTarget(newWindow=true)
        target_id, window_id = await self._create_target_via_cdp()

        # Step 2: Check if we actually got a new window
        needs_fallback = (
            window_id is not None and len(existing_windows) > 0 and window_id in existing_windows
        )

        if needs_fallback:
            logger.warning(
                "Target.createTarget(newWindow=true) produced "
                "windowId=%s which is already in use. "
                "Chrome ignored newWindow parameter.",
                window_id,
            )

            # Close the tab that landed in the wrong window
            page_ws_url = self._get_any_existing_page_ws_url()
            if page_ws_url is None:
                logger.warning(
                    "No existing page session for fallback. Proceeding without separate window."
                )
            else:
                # Close the wrongly-placed tab
                try:
                    await self._proxy.close_page(target_id)
                    logger.debug("Closed wrongly-placed target %s", target_id)
                except Exception as e:
                    logger.warning("Failed to close target %s: %s", target_id, e)

                # Step 3: Fallback via window.open()
                target_id, window_id = await self._create_target_via_window_open(page_ws_url)

                # Verify the fallback actually got a different window
                if window_id is not None and window_id in existing_windows:
                    logger.warning(
                        "Fallback also produced duplicate "
                        "windowId=%s. Proceeding anyway - "
                        "session isolation will be tab-based only.",
                        window_id,
                    )

        # Step 4: Get WebSocket URL for the new target
        ws_url = await self._get_ws_url_for_target(target_id)
        if ws_url is None:
            raise RuntimeError(f"Failed to get WebSocket URL for target {target_id}")

        state = ProxySessionState(
            session_id=session_id,
            window_id=window_id,
            current_target_id=target_id,
            targets=[target_id],
            ws_urls={target_id: ws_url},
        )
        self._sessions[session_id] = state

        logger.info(
            "Session %s: created (window=%s, target=%s)",
            session_id,
            window_id,
            target_id,
        )
        return state

    async def create_tab_in_session(self, session_id: str, url: str = "about:blank") -> str:
        """Create a new tab in an existing session's window.

        Uses window.open() on the session's current tab to ensure the new
        tab opens in the same window (not a random window like Target.createTarget).

        Args:
            session_id: The session to create the tab in.
            url: URL to open in the new tab.

        Returns:
            The target_id of the new tab.

        Raises:
            KeyError: If session_id is not found.
            RuntimeError: If the session has no current tab or tab creation fails.
        """
        state = self._sessions[session_id]

        if state.current_ws_url is None:
            raise RuntimeError(f"Session {session_id} has no current tab to open from")

        # Snapshot current targets
        before_targets = await self._proxy.list_targets()
        before = {t["id"] for t in before_targets if t.get("type") == "page"}

        # Open new tab via window.open() on the current page
        # This ensures the tab opens in the SAME window as the opener
        import json

        await self._proxy.evaluate(
            state.current_ws_url,
            f"window.open({json.dumps(url)}, '_blank')",
        )

        # Poll for the new target (longer intervals for proxy mode)
        new_target_id: str | None = None
        for attempt in range(15):
            await asyncio.sleep(0.5)
            after_targets = await self._proxy.list_targets()
            after = {t["id"] for t in after_targets if t.get("type") == "page"}
            new_ids = after - before
            if new_ids:
                new_target_id = new_ids.pop()
                logger.info(
                    "Session %s: new tab %s -> %s (attempt %d)",
                    session_id,
                    new_target_id,
                    url,
                    attempt + 1,
                )
                break

        if new_target_id is None:
            raise RuntimeError(
                f"Failed to create new tab in session {session_id}: "
                "no new target appeared after window.open()"
            )

        # Get WebSocket URL for the new target
        ws_url = await self._get_ws_url_for_target(new_target_id)
        if ws_url is None:
            raise RuntimeError(f"Failed to get WebSocket URL for new target {new_target_id}")

        state.targets.append(new_target_id)
        state.ws_urls[new_target_id] = ws_url
        state.current_target_id = new_target_id

        return new_target_id

    async def switch_tab_in_session(self, session_id: str, target_id: str) -> str:
        """Switch a session's active tab.

        Args:
            session_id: The session to switch tabs in.
            target_id: The target_id to switch to.

        Returns:
            The WebSocket URL for the newly active tab.

        Raises:
            KeyError: If session_id is not found.
            ValueError: If target_id is not in this session.
        """
        state = self._sessions[session_id]

        if target_id not in state.targets:
            raise ValueError(
                f"Target {target_id} does not belong to "
                f"session {session_id}. "
                f"Available targets: {state.targets}"
            )

        state.current_target_id = target_id

        # Ensure we have a WebSocket URL for this target
        if target_id not in state.ws_urls:
            ws_url = await self._get_ws_url_for_target(target_id)
            if ws_url is None:
                raise RuntimeError(f"Failed to get WebSocket URL for target {target_id}")
            state.ws_urls[target_id] = ws_url

        # Activate the target visually in its window
        browser_ws = await self._ensure_browser_ws_url()
        await self._proxy.send_cdp_command(
            browser_ws,
            "Target.activateTarget",
            {"targetId": target_id},
        )

        logger.info("Session %s: switched to tab %s", session_id, target_id)
        return state.ws_urls[target_id]

    async def close_tab_in_session(self, session_id: str, target_id: str) -> None:
        """Close a tab within a session.

        If closing the current tab, switches to another tab.

        Args:
            session_id: The session that owns the tab.
            target_id: The target_id of the tab to close.

        Raises:
            KeyError: If session_id is not found.
            ValueError: If target_id not in session or is the last tab.
        """
        state = self._sessions[session_id]

        if target_id not in state.targets:
            raise ValueError(f"Target {target_id} does not belong to session {session_id}")

        if len(state.targets) <= 1:
            raise ValueError(
                f"Cannot close the last tab in session {session_id}. "
                f"Use destroy() to close the entire session."
            )

        # Close the target via CDP
        await self._proxy.close_page(target_id)

        # Remove from session state
        state.targets.remove(target_id)
        if target_id in state.ws_urls:
            del state.ws_urls[target_id]

        # If we closed the current tab, switch to another
        if state.current_target_id == target_id:
            state.current_target_id = state.targets[0]
            logger.info(
                "Session %s: auto-switched to tab %s",
                session_id,
                state.current_target_id,
            )

        logger.info("Session %s: closed tab %s", session_id, target_id)

    async def list_tabs_in_session(self, session_id: str) -> list[dict[str, Any]]:
        """List all tabs in a session's window.

        Args:
            session_id: The session to list tabs for.

        Returns:
            List of tab info dicts with id, title, url, is_current.

        Raises:
            KeyError: If session_id is not found.
        """
        state = self._sessions[session_id]

        all_targets = await self._proxy.list_targets()
        session_targets = [t for t in all_targets if t.get("id") in state.targets]

        tabs = []
        for target in session_targets:
            tabs.append(
                {
                    "id": target.get("id"),
                    "title": target.get("title", ""),
                    "url": target.get("url", ""),
                    "is_current": (target.get("id") == state.current_target_id),
                }
            )

        return tabs

    def list_sessions(self) -> dict[str, dict[str, Any]]:
        """List all active sessions.

        Returns:
            Dict mapping session_id to session info.
        """
        result = {}
        for session_id, state in self._sessions.items():
            result[session_id] = {
                "session_id": session_id,
                "window_id": state.window_id,
                "tab_count": len(state.targets),
                "current_target_id": state.current_target_id,
            }
        return result

    async def destroy(self, session_id: str) -> None:
        """Destroy a session, closing its Chrome window and all tabs.

        Args:
            session_id: The session to destroy.

        Raises:
            KeyError: If session_id is not found.
        """
        state = self._sessions.pop(session_id)

        for target_id in state.targets:
            try:
                await self._proxy.close_page(target_id)
            except Exception as e:
                logger.warning(
                    "Error closing target %s in %s: %s",
                    target_id,
                    session_id,
                    e,
                )

        logger.info(
            "Destroyed session %s (window %s)",
            session_id,
            state.window_id,
        )

    async def cleanup(self) -> None:
        """Destroy all sessions. Called on server shutdown.

        Respects CHROME_MCP_CLEANUP_ON_EXIT environment variable.
        If set to "false" (default), windows are left open.
        """
        cleanup_on_exit = os.environ.get("CHROME_MCP_CLEANUP_ON_EXIT", "false").lower()
        if cleanup_on_exit not in ("true", "1", "yes"):
            logger.info(
                "CHROME_MCP_CLEANUP_ON_EXIT=%s, leaving %d session(s) open",
                cleanup_on_exit,
                len(self._sessions),
            )
            self._sessions.clear()
            return

        logger.info("Cleaning up %d session(s)", len(self._sessions))
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            try:
                await self.destroy(session_id)
            except Exception as e:
                logger.warning("Error destroying session %s: %s", session_id, e)
