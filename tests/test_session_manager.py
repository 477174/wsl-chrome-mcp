"""Tests for SessionManager - multi-session Chrome window isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wsl_chrome_mcp.cdp_client import CDPSession, CDPTarget
from wsl_chrome_mcp.session_manager import SessionManager, SessionState

# --- Fixtures ---


def make_target(target_id: str, url: str = "about:blank") -> CDPTarget:
    """Create a CDPTarget for testing."""
    return CDPTarget(
        id=target_id,
        type="page",
        title=f"Tab {target_id}",
        url=url,
        websocket_url=f"ws://localhost:9222/devtools/page/{target_id}",
    )


def make_cdp_session(target_id: str) -> CDPSession:
    """Create a mock CDPSession."""
    target = make_target(target_id)
    session = MagicMock(spec=CDPSession)
    session.target = target
    session.send = AsyncMock(return_value={})
    session.close = AsyncMock()
    return session


def make_browser_session() -> CDPSession:
    """Create a mock browser-level CDP session."""
    session = MagicMock(spec=CDPSession)
    session.send = AsyncMock()
    session.close = AsyncMock()
    return session


def make_cdp_client() -> MagicMock:
    """Create a mock CDPClient."""
    client = MagicMock()
    client.list_targets = AsyncMock(return_value=[])
    client.connect_to_target = AsyncMock()
    client.connect_to_browser = AsyncMock()
    client.close_page = AsyncMock()
    return client


def setup_single_session_mocks(
    client: MagicMock,
    browser: CDPSession,
    target_id: str = "T1",
    window_id: int = 42,
) -> CDPSession:
    """Set up mocks for creating one session.

    Returns the page CDPSession mock.
    """
    cdp_session = make_cdp_session(target_id)
    target = make_target(target_id)

    client.connect_to_browser = AsyncMock(return_value=browser)
    browser.send = AsyncMock(
        side_effect=[
            # Target.createTarget
            {"targetId": target_id},
            # Browser.getWindowForTarget
            {"windowId": window_id, "bounds": {}},
        ]
    )
    client.list_targets = AsyncMock(return_value=[target])
    client.connect_to_target = AsyncMock(return_value=cdp_session)

    return cdp_session


# --- SessionState Tests ---


class TestSessionState:
    """Tests for SessionState dataclass."""

    def test_current_session_returns_none_when_no_target(self) -> None:
        """Should return None when no current target is set."""
        state = SessionState(session_id="test")
        assert state.current_session is None

    def test_current_session_returns_none_when_target_not_in_sessions(
        self,
    ) -> None:
        """Should return None when current target has no CDP session."""
        state = SessionState(session_id="test", current_target_id="T1")
        assert state.current_session is None

    def test_current_session_returns_cdp_session(self) -> None:
        """Should return the CDP session for the current target."""
        session = make_cdp_session("T1")
        state = SessionState(
            session_id="test",
            current_target_id="T1",
            cdp_sessions={"T1": session},
        )
        assert state.current_session is session


# --- SessionManager.get_or_create Tests ---


class TestSessionManagerGetOrCreate:
    """Tests for SessionManager.get_or_create()."""

    @pytest.mark.asyncio
    async def test_creates_new_window_for_new_session(self) -> None:
        """First session: Target.createTarget(newWindow=true) works."""
        client = make_cdp_client()
        browser = make_browser_session()
        setup_single_session_mocks(client, browser)

        manager = SessionManager(client)
        state = await manager.get_or_create("ses_abc")

        assert state.session_id == "ses_abc"
        assert state.window_id == 42
        assert state.current_target_id == "T1"
        assert "T1" in state.targets
        assert "T1" in state.cdp_sessions

        # Verify Target.createTarget was called with newWindow=True
        create_call = browser.send.call_args_list[0]
        assert create_call[0][0] == "Target.createTarget"
        assert create_call[0][1]["newWindow"] is True

    @pytest.mark.asyncio
    async def test_returns_existing_session(self) -> None:
        """Should return cached state on second call with same ID."""
        client = make_cdp_client()
        browser = make_browser_session()
        setup_single_session_mocks(client, browser)

        manager = SessionManager(client)
        state1 = await manager.get_or_create("ses_abc")
        state2 = await manager.get_or_create("ses_abc")

        assert state1 is state2
        client.connect_to_browser.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_sessions_different_windows(self) -> None:
        """Two sessions with unique windowIds - no fallback needed."""
        client = make_cdp_client()
        browser = make_browser_session()

        s1 = make_cdp_session("T1")
        s2 = make_cdp_session("T2")
        t1 = make_target("T1")
        t2 = make_target("T2")

        client.connect_to_browser = AsyncMock(return_value=browser)
        browser.send = AsyncMock(
            side_effect=[
                # Session 1: createTarget + getWindow
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
                # Session 2: createTarget + getWindow
                {"targetId": "T2"},
                {"windowId": 20, "bounds": {}},
            ]
        )
        client.list_targets = AsyncMock(side_effect=[[t1], [t2]])
        client.connect_to_target = AsyncMock(side_effect=[s1, s2])

        manager = SessionManager(client)
        state1 = await manager.get_or_create("ses_abc")
        state2 = await manager.get_or_create("ses_xyz")

        assert state1.window_id == 10
        assert state2.window_id == 20
        assert state1.current_target_id == "T1"
        assert state2.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_first_session_skips_uniqueness_check(self) -> None:
        """First session has no others to compare - always accepts."""
        client = make_cdp_client()
        browser = make_browser_session()

        setup_single_session_mocks(client, browser, target_id="T1", window_id=99)

        manager = SessionManager(client)
        state = await manager.get_or_create("first")

        assert state.window_id == 99
        assert state.session_id == "first"
        # Only 2 browser.send calls: createTarget + getWindow
        assert browser.send.call_count == 2


class TestSessionManagerFallback:
    """Tests for the window.open() fallback path."""

    @pytest.mark.asyncio
    async def test_fallback_when_new_window_ignored(self) -> None:
        """When newWindow produces same windowId, fall back to window.open()."""
        client = make_cdp_client()
        browser = make_browser_session()

        # --- Session 1 (normal) ---
        s1 = make_cdp_session("T1")
        t1 = make_target("T1")

        client.connect_to_browser = AsyncMock(return_value=browser)
        browser.send = AsyncMock(
            side_effect=[
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
            ]
        )
        client.list_targets = AsyncMock(return_value=[t1])
        client.connect_to_target = AsyncMock(return_value=s1)

        manager = SessionManager(client)
        state1 = await manager.get_or_create("ses_1")
        assert state1.window_id == 10

        # --- Session 2 (newWindow ignored -> fallback) ---
        s2_good = make_cdp_session("T2_good")
        t2_good = make_target("T2_good")

        browser.send = AsyncMock(
            side_effect=[
                # createTarget -> lands in window 10 (duplicate!)
                {"targetId": "T2_wrong"},
                # getWindowForTarget -> same window as session 1
                {"windowId": 10, "bounds": {}},
                # getWindowForTarget on fallback target T2_good
                {"windowId": 20, "bounds": {}},
            ]
        )

        # list_targets sequence:
        # 1. before snapshot (for window.open diff)
        # 2. after poll (T2_good appeared)
        # 3. connect_and_setup_target
        client.list_targets = AsyncMock(
            side_effect=[
                [t1],
                [t1, t2_good],
                [t1, t2_good],
            ]
        )
        client.connect_to_target = AsyncMock(return_value=s2_good)
        client.close_page = AsyncMock()

        state2 = await manager.get_or_create("ses_2")

        assert state2.window_id == 20
        assert state2.current_target_id == "T2_good"
        client.close_page.assert_called_with("T2_wrong")

    @pytest.mark.asyncio
    async def test_fallback_no_existing_session_proceeds(self) -> None:
        """If no existing page for fallback, proceed without window."""
        client = make_cdp_client()
        browser = make_browser_session()

        # This shouldn't happen in practice (first session always
        # skips uniqueness check), but test the safety path.
        manager = SessionManager(client)

        # Manually inject a session with no working CDPSession
        dummy = SessionState(
            session_id="broken",
            window_id=10,
            current_target_id=None,
        )
        manager._sessions["broken"] = dummy

        s2 = make_cdp_session("T2")
        t2 = make_target("T2")

        client.connect_to_browser = AsyncMock(return_value=browser)
        browser.send = AsyncMock(
            side_effect=[
                {"targetId": "T2"},
                # Same window as "broken" session
                {"windowId": 10, "bounds": {}},
            ]
        )
        client.list_targets = AsyncMock(return_value=[t2])
        client.connect_to_target = AsyncMock(return_value=s2)

        # Should proceed without fallback (no page session available)
        state = await manager.get_or_create("ses_new")

        assert state.window_id == 10
        assert state.current_target_id == "T2"


# --- Tab Operations Tests ---


class TestSessionManagerTabOperations:
    """Tests for tab management within sessions."""

    async def _setup_manager(
        self,
    ) -> tuple[SessionManager, SessionState]:
        """Helper to set up a manager with one session."""
        client = make_cdp_client()
        browser = make_browser_session()
        setup_single_session_mocks(client, browser)

        manager = SessionManager(client)
        state = await manager.get_or_create("ses_test")

        # Reset mocks for subsequent calls
        browser.send = AsyncMock()
        client.list_targets = AsyncMock()
        client.connect_to_target = AsyncMock()

        return manager, state

    @pytest.mark.asyncio
    async def test_create_tab_adds_to_session(self) -> None:
        """Should create a new tab and add it to session state."""
        manager, state = await self._setup_manager()
        browser = await manager._ensure_browser_session()
        target2 = make_target("T2", "https://example.com")
        session2 = make_cdp_session("T2")

        browser.send = AsyncMock(return_value={"targetId": "T2"})
        manager._cdp.list_targets = AsyncMock(return_value=[target2])
        manager._cdp.connect_to_target = AsyncMock(return_value=session2)

        target_id = await manager.create_tab_in_session("ses_test", "https://example.com")

        assert target_id == "T2"
        assert "T2" in state.targets
        assert state.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_updates_current(self) -> None:
        """Should update current_target_id when switching tabs."""
        manager, state = await self._setup_manager()

        session2 = make_cdp_session("T2")
        state.targets.append("T2")
        state.cdp_sessions["T2"] = session2

        browser = await manager._ensure_browser_session()
        browser.send = AsyncMock()

        await manager.switch_tab_in_session("ses_test", "T2")

        assert state.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_rejects_foreign_target(self) -> None:
        """Should reject switching to a target not in this session."""
        manager, _state = await self._setup_manager()

        with pytest.raises(ValueError, match="does not belong to session"):
            await manager.switch_tab_in_session("ses_test", "T_OTHER")

    @pytest.mark.asyncio
    async def test_close_tab_removes_from_session(self) -> None:
        """Should close the tab and remove it from session state."""
        manager, state = await self._setup_manager()

        session2 = make_cdp_session("T2")
        state.targets.append("T2")
        state.cdp_sessions["T2"] = session2

        await manager.close_tab_in_session("ses_test", "T2")

        assert "T2" not in state.targets
        assert "T2" not in state.cdp_sessions

    @pytest.mark.asyncio
    async def test_close_last_tab_raises(self) -> None:
        """Should not allow closing the last tab in a session."""
        manager, _state = await self._setup_manager()

        with pytest.raises(ValueError, match="Cannot close the last tab"):
            await manager.close_tab_in_session("ses_test", "T1")

    @pytest.mark.asyncio
    async def test_close_current_tab_auto_switches(self) -> None:
        """Should auto-switch to another tab when closing current."""
        manager, state = await self._setup_manager()

        session2 = make_cdp_session("T2")
        state.targets.append("T2")
        state.cdp_sessions["T2"] = session2
        state.current_target_id = "T2"

        await manager.close_tab_in_session("ses_test", "T2")

        assert state.current_target_id == "T1"

    @pytest.mark.asyncio
    async def test_list_tabs_only_shows_session_tabs(self) -> None:
        """Should only list tabs belonging to the session."""
        manager, _state = await self._setup_manager()

        all_targets = [
            make_target("T1"),
            make_target("T_OTHER"),
        ]
        manager._cdp.list_targets = AsyncMock(return_value=all_targets)

        tabs = await manager.list_tabs_in_session("ses_test")

        assert len(tabs) == 1
        assert tabs[0]["id"] == "T1"
        assert tabs[0]["is_current"] is True


# --- Lifecycle Tests ---


class TestSessionManagerLifecycle:
    """Tests for session destruction and cleanup."""

    @pytest.mark.asyncio
    async def test_destroy_closes_sessions_and_pages(self) -> None:
        """Should close all CDP sessions and targets."""
        client = make_cdp_client()
        browser = make_browser_session()
        cdp_session = setup_single_session_mocks(client, browser)

        manager = SessionManager(client)
        await manager.get_or_create("ses_abc")

        await manager.destroy("ses_abc")

        cdp_session.close.assert_called_once()
        client.close_page.assert_called_once_with("T1")
        assert "ses_abc" not in manager._sessions

    @pytest.mark.asyncio
    async def test_destroy_unknown_session_raises(self) -> None:
        """Should raise KeyError when destroying unknown session."""
        client = make_cdp_client()
        manager = SessionManager(client)

        with pytest.raises(KeyError):
            await manager.destroy("nonexistent")

    @pytest.mark.asyncio
    async def test_cleanup_destroys_all_sessions(self) -> None:
        """Should destroy all sessions during cleanup."""
        client = make_cdp_client()
        browser = make_browser_session()

        s1 = make_cdp_session("T1")
        s2 = make_cdp_session("T2")
        t1 = make_target("T1")
        t2 = make_target("T2")

        client.connect_to_browser = AsyncMock(return_value=browser)
        browser.send = AsyncMock(
            side_effect=[
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
                {"targetId": "T2"},
                {"windowId": 20, "bounds": {}},
            ]
        )
        client.list_targets = AsyncMock(side_effect=[[t1], [t2]])
        client.connect_to_target = AsyncMock(side_effect=[s1, s2])

        manager = SessionManager(client)
        await manager.get_or_create("ses_1")
        await manager.get_or_create("ses_2")

        assert len(manager._sessions) == 2

        await manager.cleanup()

        assert len(manager._sessions) == 0
        browser.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_sessions_returns_info(self) -> None:
        """Should return session info for all active sessions."""
        client = make_cdp_client()
        browser = make_browser_session()
        setup_single_session_mocks(client, browser)

        manager = SessionManager(client)
        await manager.get_or_create("ses_abc")

        sessions = manager.list_sessions()

        assert "ses_abc" in sessions
        assert sessions["ses_abc"]["window_id"] == 42
        assert sessions["ses_abc"]["tab_count"] == 1
        assert sessions["ses_abc"]["current_target_id"] == "T1"


# --- Helper Tests ---


class TestSessionIdExtraction:
    """Tests for session_id extraction in tool calls."""

    def test_with_session_id_helper(self) -> None:
        """Should merge session_id property into tool properties."""
        from wsl_chrome_mcp.server import _with_session_id

        props = _with_session_id({"url": {"type": "string"}})
        assert "url" in props
        assert "session_id" in props
        assert props["session_id"]["type"] == "string"

    def test_with_session_id_does_not_overwrite(self) -> None:
        """Should not overwrite existing properties."""
        from wsl_chrome_mcp.server import _with_session_id

        props = _with_session_id({"url": {"type": "string"}})
        assert props["url"] == {"type": "string"}
