"""Session manager for multi-session Chrome window isolation.

Each opencode chat session gets its own Chrome window so agents
can operate on their own tabs without interfering with each other.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .cdp_client import (
    CDPClient,
    CDPSession,
    evaluate_javascript,
    get_console_messages,
    get_network_requests,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """State for a single chat session's Chrome window.

    Each session owns one Chrome window and tracks its tabs,
    console messages, and network requests independently.
    """

    session_id: str
    window_id: int | None = None
    current_target_id: str | None = None
    targets: list[str] = field(default_factory=list)
    cdp_sessions: dict[str, CDPSession] = field(default_factory=dict)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current_session(self) -> CDPSession | None:
        """Get the CDP session for the current (active) tab."""
        if self.current_target_id and self.current_target_id in self.cdp_sessions:
            return self.cdp_sessions[self.current_target_id]
        return None


class SessionManager:
    """Manages multiple Chrome sessions, one window per opencode session.

    Routes tool calls to the correct Chrome window/tab based on session_id.
    Creates new windows on demand when a new session_id is first seen.

    Uses Target.createTarget(newWindow=true) as primary strategy.
    Falls back to window.open() via JavaScript if Chrome ignores newWindow.
    """

    def __init__(self, cdp_client: CDPClient) -> None:
        """Initialize the session manager.

        Args:
            cdp_client: Shared CDP client connected to Chrome.
        """
        self._cdp = cdp_client
        self._sessions: dict[str, SessionState] = {}
        self._browser_session: CDPSession | None = None

    async def _ensure_browser_session(self) -> CDPSession:
        """Get or create a browser-level CDP session.

        Returns:
            CDPSession connected to the browser endpoint.
        """
        if self._browser_session is not None:
            return self._browser_session

        self._browser_session = await self._cdp.connect_to_browser()
        logger.info("Connected to browser-level WebSocket")
        return self._browser_session

    def _get_existing_window_ids(self) -> set[int]:
        """Get window IDs already claimed by existing sessions."""
        return {s.window_id for s in self._sessions.values() if s.window_id is not None}

    def _get_any_existing_page_session(self) -> CDPSession | None:
        """Get any existing page CDPSession for running JS fallback."""
        for state in self._sessions.values():
            if state.current_session is not None:
                return state.current_session
        return None

    async def _get_window_id_for_target(self, target_id: str) -> int | None:
        """Query the window ID that a target belongs to.

        Args:
            target_id: The CDP target ID.

        Returns:
            The Chrome window ID, or None if the query fails.
        """
        browser = await self._ensure_browser_session()
        try:
            info = await browser.send(
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
        browser = await self._ensure_browser_session()

        result = await browser.send(
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

    async def _create_target_via_window_open(
        self, page_session: CDPSession
    ) -> tuple[str, int | None]:
        """Create a target in a new window using window.open() fallback.

        Executes window.open('about:blank', '_blank', 'popup') on an
        existing page. The 'popup' feature forces Chrome to open a new
        window rather than a tab.

        Args:
            page_session: An existing page CDPSession to run JS on.

        Returns:
            Tuple of (target_id, window_id).

        Raises:
            RuntimeError: If the new target cannot be detected.
        """
        logger.info("Fallback: creating window via window.open()")

        # Snapshot current targets
        before = {t.id for t in await self._cdp.list_targets()}
        logger.debug("Targets before window.open(): %s", before)

        # Open a popup window via JavaScript
        await evaluate_javascript(
            page_session,
            "window.open('about:blank', '_blank', 'popup')",
        )

        # Poll for the new target (it may take a moment to appear)
        new_target_id: str | None = None
        for attempt in range(20):
            await asyncio.sleep(0.25)
            after = {t.id for t in await self._cdp.list_targets()}
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

    async def _connect_and_setup_target(
        self, target_id: str
    ) -> tuple[CDPSession, list[dict[str, Any]], list[dict[str, Any]]]:
        """Connect to a target and enable DOM/console/network monitoring.

        Args:
            target_id: The target to connect to.

        Returns:
            Tuple of (cdp_session, console_messages, network_requests).
        """
        targets = await self._cdp.list_targets()
        target = next((t for t in targets if t.id == target_id), None)

        if target is None:
            raise RuntimeError(f"Target {target_id} not found in target list")

        cdp_session = await self._cdp.connect_to_target(target)
        await cdp_session.send("DOM.enable")
        console_msgs = await get_console_messages(cdp_session)
        network_reqs = await get_network_requests(cdp_session)

        return cdp_session, console_msgs, network_reqs

    async def get_or_create(self, session_id: str) -> SessionState:
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
            SessionState for the requested session.
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
            page_session = self._get_any_existing_page_session()
            if page_session is None:
                logger.warning(
                    "No existing page session for fallback. Proceeding without separate window."
                )
            else:
                # Close the wrongly-placed tab
                try:
                    await self._cdp.close_page(target_id)
                    logger.debug("Closed wrongly-placed target %s", target_id)
                except Exception as e:
                    logger.warning("Failed to close target %s: %s", target_id, e)

                # Step 3: Fallback via window.open()
                target_id, window_id = await self._create_target_via_window_open(page_session)

                # Verify the fallback actually got a different window
                if window_id is not None and window_id in existing_windows:
                    logger.warning(
                        "Fallback also produced duplicate "
                        "windowId=%s. Proceeding anyway - "
                        "session isolation will be tab-based only.",
                        window_id,
                    )

        # Step 4: Connect to the target and set up monitoring
        cdp_session, console_msgs, network_reqs = await self._connect_and_setup_target(target_id)

        state = SessionState(
            session_id=session_id,
            window_id=window_id,
            current_target_id=target_id,
            targets=[target_id],
            cdp_sessions={target_id: cdp_session},
            console_messages=console_msgs,
            network_requests=network_reqs,
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

        Note: Target.createTarget does not support a windowId param,
        so the new tab lands in whichever window Chrome decides.
        We track it in this session regardless.

        Args:
            session_id: The session to create the tab in.
            url: URL to open in the new tab.

        Returns:
            The target_id of the new tab.

        Raises:
            KeyError: If session_id is not found.
        """
        state = self._sessions[session_id]
        browser = await self._ensure_browser_session()

        result = await browser.send(
            "Target.createTarget",
            {"url": url},
        )
        target_id = result["targetId"]
        logger.info(
            "Session %s: new tab %s -> %s",
            session_id,
            target_id,
            url,
        )

        # Connect and set up
        cdp_session, console_msgs, network_reqs = await self._connect_and_setup_target(target_id)

        state.targets.append(target_id)
        state.cdp_sessions[target_id] = cdp_session
        state.current_target_id = target_id
        state.console_messages.extend(console_msgs)
        state.network_requests.extend(network_reqs)

        return target_id

    async def switch_tab_in_session(self, session_id: str, target_id: str) -> CDPSession:
        """Switch a session's active tab.

        Args:
            session_id: The session to switch tabs in.
            target_id: The target_id to switch to.

        Returns:
            The CDP session for the newly active tab.

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

        # Ensure we have a CDP session for this target
        if target_id not in state.cdp_sessions:
            cdp_session, _, _ = await self._connect_and_setup_target(target_id)
            state.cdp_sessions[target_id] = cdp_session

        # Activate the target visually in its window
        browser = await self._ensure_browser_session()
        await browser.send("Target.activateTarget", {"targetId": target_id})

        logger.info("Session %s: switched to tab %s", session_id, target_id)
        return state.cdp_sessions[target_id]

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

        # Close the CDP session for this target
        if target_id in state.cdp_sessions:
            await state.cdp_sessions[target_id].close()
            del state.cdp_sessions[target_id]

        # Close the target via CDP
        await self._cdp.close_page(target_id)

        # Remove from session state
        state.targets.remove(target_id)

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

        all_targets = await self._cdp.list_targets()
        session_targets = [t for t in all_targets if t.id in state.targets]

        tabs = []
        for target in session_targets:
            tabs.append(
                {
                    "id": target.id,
                    "title": target.title,
                    "url": target.url,
                    "is_current": (target.id == state.current_target_id),
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

        for cdp_session in state.cdp_sessions.values():
            try:
                await cdp_session.close()
            except Exception as e:
                logger.warning(
                    "Error closing CDP session in %s: %s",
                    session_id,
                    e,
                )

        for target_id in state.targets:
            try:
                await self._cdp.close_page(target_id)
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
        """Destroy all sessions. Called on server shutdown."""
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            try:
                await self.destroy(session_id)
            except Exception as e:
                logger.warning("Error destroying session %s: %s", session_id, e)

        if self._browser_session is not None:
            try:
                await self._browser_session.close()
            except Exception as e:
                logger.warning("Error closing browser session: %s", e)
            self._browser_session = None
