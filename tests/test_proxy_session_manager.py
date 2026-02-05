"""Tests for ProxySessionManager - multi-session Chrome window isolation in proxy mode."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wsl_chrome_mcp.proxy_session_manager import ProxySessionManager, ProxySessionState


# --- Fixtures ---


def make_proxy_client() -> MagicMock:
    """Create a mock CDPProxyClient."""
    proxy = MagicMock()
    proxy.get_browser_ws_url = AsyncMock(return_value="ws://localhost:9222/devtools/browser/abc123")
    proxy.list_targets = AsyncMock(return_value=[])
    proxy.send_cdp_command = AsyncMock(return_value={})
    proxy.close_page = AsyncMock(return_value=True)
    proxy.evaluate = AsyncMock(return_value=None)
    proxy.navigate = AsyncMock(return_value={"frameId": "123"})
    return proxy


def make_page_target(target_id: str, url: str = "about:blank") -> dict:
    """Create a page target dict like /json/list returns."""
    return {
        "id": target_id,
        "type": "page",
        "title": f"Tab {target_id}",
        "url": url,
        "webSocketDebuggerUrl": f"ws://localhost:9222/devtools/page/{target_id}",
    }


def setup_single_session_mocks(
    proxy: MagicMock,
    target_id: str = "T1",
    window_id: int = 42,
) -> None:
    """Set up mocks for creating one session."""
    target = make_page_target(target_id)

    proxy.send_cdp_command = AsyncMock(
        side_effect=[
            # Target.createTarget
            {"targetId": target_id},
            # Browser.getWindowForTarget
            {"windowId": window_id, "bounds": {}},
        ]
    )
    proxy.list_targets = AsyncMock(return_value=[target])


# --- ProxySessionState Tests ---


class TestProxySessionState:
    """Tests for ProxySessionState dataclass."""

    def test_current_ws_url_returns_none_when_no_target(self) -> None:
        """Should return None when no current target is set."""
        state = ProxySessionState(session_id="test")
        assert state.current_ws_url is None

    def test_current_ws_url_returns_none_when_target_not_in_ws_urls(self) -> None:
        """Should return None when current target has no WebSocket URL."""
        state = ProxySessionState(session_id="test", current_target_id="T1")
        assert state.current_ws_url is None

    def test_current_ws_url_returns_url(self) -> None:
        """Should return the WebSocket URL for the current target."""
        state = ProxySessionState(
            session_id="test",
            current_target_id="T1",
            ws_urls={"T1": "ws://localhost:9222/devtools/page/T1"},
        )
        assert state.current_ws_url == "ws://localhost:9222/devtools/page/T1"


# --- ProxySessionManager.get_or_create Tests ---


class TestProxySessionManagerGetOrCreate:
    """Tests for ProxySessionManager.get_or_create()."""

    @pytest.mark.asyncio
    async def test_creates_new_window_for_new_session(self) -> None:
        """First session: Target.createTarget(newWindow=true) works."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        state = await manager.get_or_create("ses_abc")

        assert state.session_id == "ses_abc"
        assert state.window_id == 42
        assert state.current_target_id == "T1"
        assert "T1" in state.targets
        assert "T1" in state.ws_urls

        # Verify Target.createTarget was called with newWindow=True
        calls = proxy.send_cdp_command.call_args_list
        assert calls[0][0][1] == "Target.createTarget"
        assert calls[0][0][2]["newWindow"] is True

    @pytest.mark.asyncio
    async def test_returns_existing_session(self) -> None:
        """Should return cached state on second call with same ID."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        state1 = await manager.get_or_create("ses_abc")
        state2 = await manager.get_or_create("ses_abc")

        assert state1 is state2
        proxy.get_browser_ws_url.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_sessions_different_windows(self) -> None:
        """Two sessions with unique windowIds - no fallback needed."""
        proxy = make_proxy_client()
        t1 = make_page_target("T1")
        t2 = make_page_target("T2")

        proxy.send_cdp_command = AsyncMock(
            side_effect=[
                # Session 1: createTarget + getWindow
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
                # Session 2: createTarget + getWindow
                {"targetId": "T2"},
                {"windowId": 20, "bounds": {}},
            ]
        )
        proxy.list_targets = AsyncMock(side_effect=[[t1], [t2]])

        manager = ProxySessionManager(proxy)
        state1 = await manager.get_or_create("ses_abc")
        state2 = await manager.get_or_create("ses_xyz")

        assert state1.window_id == 10
        assert state2.window_id == 20
        assert state1.current_target_id == "T1"
        assert state2.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_first_session_skips_uniqueness_check(self) -> None:
        """First session has no others to compare - always accepts."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy, target_id="T1", window_id=99)

        manager = ProxySessionManager(proxy)
        state = await manager.get_or_create("first")

        assert state.window_id == 99
        assert state.session_id == "first"
        # Only 2 send_cdp_command calls: createTarget + getWindow
        assert proxy.send_cdp_command.call_count == 2


class TestProxySessionManagerFallback:
    """Tests for the window.open() fallback path."""

    @pytest.mark.asyncio
    async def test_fallback_when_new_window_ignored(self) -> None:
        """When newWindow produces same windowId, fall back to window.open()."""
        proxy = make_proxy_client()

        # --- Session 1 (normal) ---
        t1 = make_page_target("T1")
        proxy.send_cdp_command = AsyncMock(
            side_effect=[
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
            ]
        )
        proxy.list_targets = AsyncMock(return_value=[t1])

        manager = ProxySessionManager(proxy)
        state1 = await manager.get_or_create("ses_1")
        assert state1.window_id == 10

        # --- Session 2 (newWindow ignored -> fallback) ---
        t2_good = make_page_target("T2_good")

        proxy.send_cdp_command = AsyncMock(
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
        # 2-15. poll attempts - T2_good appears on attempt 1
        # 16. final get_ws_url lookup
        proxy.list_targets = AsyncMock(
            side_effect=[
                [t1],  # before snapshot
                [t1, t2_good],  # after poll (T2_good appeared)
                [t1, t2_good],  # get_ws_url lookup
            ]
        )
        proxy.close_page = AsyncMock()
        proxy.evaluate = AsyncMock()

        state2 = await manager.get_or_create("ses_2")

        assert state2.window_id == 20
        assert state2.current_target_id == "T2_good"
        proxy.close_page.assert_called_with("T2_wrong")

    @pytest.mark.asyncio
    async def test_fallback_no_existing_session_proceeds(self) -> None:
        """If no existing page for fallback, proceed without window."""
        proxy = make_proxy_client()
        manager = ProxySessionManager(proxy)

        # Manually inject a session with no working WebSocket URL
        dummy = ProxySessionState(
            session_id="broken",
            window_id=10,
            current_target_id=None,
        )
        manager._sessions["broken"] = dummy

        t2 = make_page_target("T2")
        proxy.send_cdp_command = AsyncMock(
            side_effect=[
                {"targetId": "T2"},
                # Same window as "broken" session
                {"windowId": 10, "bounds": {}},
            ]
        )
        proxy.list_targets = AsyncMock(return_value=[t2])

        # Should proceed without fallback (no page session available)
        state = await manager.get_or_create("ses_new")

        assert state.window_id == 10
        assert state.current_target_id == "T2"


# --- Tab Operations Tests ---


class TestProxySessionManagerTabOperations:
    """Tests for tab management within sessions."""

    async def _setup_manager(self) -> tuple[ProxySessionManager, ProxySessionState]:
        """Helper to set up a manager with one session."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        state = await manager.get_or_create("ses_test")

        # Reset mocks for subsequent calls
        proxy.send_cdp_command = AsyncMock()
        proxy.list_targets = AsyncMock()

        return manager, state

    @pytest.mark.asyncio
    async def test_create_tab_adds_to_session(self) -> None:
        """Should create a new tab and add it to session state."""
        manager, state = await self._setup_manager()
        target1 = make_page_target("T1")
        target2 = make_page_target("T2", "https://example.com")

        # Mock evaluate (for window.open call)
        manager._proxy.evaluate = AsyncMock(return_value=None)

        # Mock list_targets: first call returns only T1, second call returns T1+T2
        manager._proxy.list_targets = AsyncMock(
            side_effect=[[target1], [target1, target2], [target1, target2]]
        )

        target_id = await manager.create_tab_in_session("ses_test", "https://example.com")

        assert target_id == "T2"
        assert "T2" in state.targets
        assert state.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_updates_current(self) -> None:
        """Should update current_target_id when switching tabs."""
        manager, state = await self._setup_manager()

        state.targets.append("T2")
        state.ws_urls["T2"] = "ws://localhost:9222/devtools/page/T2"

        manager._proxy.send_cdp_command = AsyncMock()

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

        state.targets.append("T2")
        state.ws_urls["T2"] = "ws://localhost:9222/devtools/page/T2"

        await manager.close_tab_in_session("ses_test", "T2")

        assert "T2" not in state.targets
        assert "T2" not in state.ws_urls

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

        state.targets.append("T2")
        state.ws_urls["T2"] = "ws://localhost:9222/devtools/page/T2"
        state.current_target_id = "T2"

        await manager.close_tab_in_session("ses_test", "T2")

        assert state.current_target_id == "T1"

    @pytest.mark.asyncio
    async def test_list_tabs_only_shows_session_tabs(self) -> None:
        """Should only list tabs belonging to the session."""
        manager, _state = await self._setup_manager()

        all_targets = [
            make_page_target("T1"),
            make_page_target("T_OTHER"),
        ]
        manager._proxy.list_targets = AsyncMock(return_value=all_targets)

        tabs = await manager.list_tabs_in_session("ses_test")

        assert len(tabs) == 1
        assert tabs[0]["id"] == "T1"
        assert tabs[0]["is_current"] is True


# --- Lifecycle Tests ---


class TestProxySessionManagerLifecycle:
    """Tests for session destruction and cleanup."""

    @pytest.mark.asyncio
    async def test_destroy_closes_pages(self) -> None:
        """Should close all targets in the session."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        await manager.get_or_create("ses_abc")

        await manager.destroy("ses_abc")

        proxy.close_page.assert_called_once_with("T1")
        assert "ses_abc" not in manager._sessions

    @pytest.mark.asyncio
    async def test_destroy_unknown_session_raises(self) -> None:
        """Should raise KeyError when destroying unknown session."""
        proxy = make_proxy_client()
        manager = ProxySessionManager(proxy)

        with pytest.raises(KeyError):
            await manager.destroy("nonexistent")

    @pytest.mark.asyncio
    async def test_cleanup_skips_destroy_by_default(self) -> None:
        """Should not destroy sessions when CHROME_MCP_CLEANUP_ON_EXIT is false."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        await manager.get_or_create("ses_1")

        with patch.dict(os.environ, {"CHROME_MCP_CLEANUP_ON_EXIT": "false"}):
            await manager.cleanup()

        # Sessions cleared but close_page not called
        assert len(manager._sessions) == 0
        # close_page should NOT have been called during cleanup
        # (only the initial setup call)
        assert proxy.close_page.call_count == 0

    @pytest.mark.asyncio
    async def test_cleanup_destroys_when_enabled(self) -> None:
        """Should destroy sessions when CHROME_MCP_CLEANUP_ON_EXIT is true."""
        proxy = make_proxy_client()
        t1 = make_page_target("T1")
        t2 = make_page_target("T2")

        proxy.send_cdp_command = AsyncMock(
            side_effect=[
                {"targetId": "T1"},
                {"windowId": 10, "bounds": {}},
                {"targetId": "T2"},
                {"windowId": 20, "bounds": {}},
            ]
        )
        proxy.list_targets = AsyncMock(side_effect=[[t1], [t2]])

        manager = ProxySessionManager(proxy)
        await manager.get_or_create("ses_1")
        await manager.get_or_create("ses_2")

        assert len(manager._sessions) == 2

        with patch.dict(os.environ, {"CHROME_MCP_CLEANUP_ON_EXIT": "true"}):
            await manager.cleanup()

        assert len(manager._sessions) == 0
        # Both sessions' targets should be closed
        assert proxy.close_page.call_count == 2

    @pytest.mark.asyncio
    async def test_list_sessions_returns_info(self) -> None:
        """Should return session info for all active sessions."""
        proxy = make_proxy_client()
        setup_single_session_mocks(proxy)

        manager = ProxySessionManager(proxy)
        await manager.get_or_create("ses_abc")

        sessions = manager.list_sessions()

        assert "ses_abc" in sessions
        assert sessions["ses_abc"]["window_id"] == 42
        assert sessions["ses_abc"]["tab_count"] == 1
        assert sessions["ses_abc"]["current_target_id"] == "T1"
