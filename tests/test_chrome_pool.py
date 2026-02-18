"""Tests for ChromePoolManager - shared Chrome with per-session browser contexts."""

from __future__ import annotations

import subprocess
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wsl_chrome_mcp.chrome_pool import ChromeInstance, ChromePoolManager


def _make_manager(**kwargs: object) -> ChromePoolManager:
    """Create a ChromePoolManager with orphan cleanup mocked out."""
    with patch.object(ChromePoolManager, "_cleanup_orphaned_temp_dirs"):
        return ChromePoolManager(**kwargs)  # type: ignore[arg-type]


# --- Fixtures ---


def make_mock_proxy(port: int = 9222) -> MagicMock:
    """Create a mock CDPProxyClient."""
    proxy = MagicMock()
    proxy.get_version = AsyncMock(return_value={"Browser": "Chrome/120.0"})
    proxy.get_browser_ws_url = AsyncMock(
        return_value=f"ws://localhost:{port}/devtools/browser/abc123"
    )
    proxy.list_targets = AsyncMock(
        return_value=[
            {
                "id": "T1",
                "type": "page",
                "title": "New Tab",
                "url": "about:blank",
                "webSocketDebuggerUrl": f"ws://localhost:{port}/devtools/page/T1",
            }
        ]
    )
    proxy.new_page = AsyncMock(
        return_value={
            "id": "T1",
            "webSocketDebuggerUrl": f"ws://localhost:{port}/devtools/page/T1",
        }
    )
    proxy.send_cdp_command = AsyncMock(return_value={"targetId": "T2"})
    proxy.close_page = AsyncMock(return_value=True)
    proxy.navigate = AsyncMock(return_value={"frameId": "123"})
    proxy.evaluate = AsyncMock(return_value=None)
    return proxy


def make_chrome_instance(
    session_id: str = "test_session",
    port: int = 9222,
    pid: int = 1234,
    browser_context_id: str | None = "ctx_test",
) -> ChromeInstance:
    """Create a ChromeInstance for testing."""
    proxy = make_mock_proxy(port)
    return ChromeInstance(
        session_id=session_id,
        port=port,
        pid=pid,
        proxy=proxy,
        user_data_dir="C:\\Temp\\chrome-test",
        created_at=datetime.now(),
        current_target_id="T1",
        targets=["T1"],
        browser_context_id=browser_context_id,
    )


def _make_mock_browser_cdp() -> MagicMock:
    """Create a mock browser-level CDP client."""
    cdp = MagicMock()
    cdp.is_connected = True
    cdp.send = AsyncMock()
    cdp.disconnect = AsyncMock()
    return cdp


# --- ChromeInstance Tests ---


class TestChromeInstance:
    """Tests for ChromeInstance dataclass."""

    def test_is_connected_returns_false_when_no_cdp(self) -> None:
        """Should return False when no CDP client."""
        instance = make_chrome_instance()
        assert instance.is_connected is False

    def test_is_connected_returns_true_when_cdp_connected(self) -> None:
        """Should return True when CDP client is connected."""
        instance = make_chrome_instance()
        mock_cdp = MagicMock()
        mock_cdp.is_connected = True
        instance.cdp = mock_cdp
        assert instance.is_connected is True

    def test_clear_page_state_clears_all(self) -> None:
        """Should clear page-specific state."""
        instance = make_chrome_instance()
        instance.console_messages.append(MagicMock())
        instance.network_requests["req1"] = MagicMock()
        instance.snapshot_cache["uid1"] = {}

        instance.clear_page_state()

        assert len(instance.console_messages) == 0
        assert len(instance.network_requests) == 0
        assert len(instance.snapshot_cache) == 0

    def test_browser_context_id_stored(self) -> None:
        """Should store browser_context_id."""
        instance = make_chrome_instance(browser_context_id="ctx_abc")
        assert instance.browser_context_id == "ctx_abc"

    def test_browser_context_id_defaults_none(self) -> None:
        """Should default to None when not provided."""
        instance = make_chrome_instance(browser_context_id=None)
        assert instance.browser_context_id is None


# --- ChromePoolManager Session Tests ---


class TestChromePoolManagerSessions:
    """Tests for session management with browser contexts."""

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self) -> None:
        """Should return cached instance on second call."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc", port=9222)
        manager._instances["ses_abc"] = instance

        result = await manager.get_or_create("ses_abc")

        assert result is instance

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self) -> None:
        """Should create browser context and target for new session."""
        manager = _make_manager()

        # Set up shared Chrome state
        mock_browser_cdp = _make_mock_browser_cdp()
        mock_browser_cdp.send = AsyncMock(
            side_effect=[
                # Target.createBrowserContext
                {"browserContextId": "ctx_new"},
                # Target.createTarget
                {"targetId": "T_new"},
            ]
        )
        manager._browser_cdp = mock_browser_cdp
        manager._shared_proxy = make_mock_proxy()
        manager._shared_pid = 9999

        with (
            patch.object(manager, "_ensure_shared_chrome", new_callable=AsyncMock),
            patch.object(manager, "_connect_cdp", new_callable=AsyncMock),
        ):
            result = await manager.get_or_create("ses_new")

        assert result.session_id == "ses_new"
        assert result.browser_context_id == "ctx_new"
        assert result.current_target_id == "T_new"
        assert "T_new" in result.targets
        assert "ses_new" in manager._instances

    @pytest.mark.asyncio
    async def test_destroy_removes_instance(self) -> None:
        """Should remove instance and dispose browser context."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc", port=9222, browser_context_id="ctx_abc")
        manager._instances["ses_abc"] = instance

        mock_browser_cdp = _make_mock_browser_cdp()
        manager._browser_cdp = mock_browser_cdp

        await manager.destroy("ses_abc")

        assert "ses_abc" not in manager._instances
        mock_browser_cdp.send.assert_called_once_with(
            "Target.disposeBrowserContext",
            {"browserContextId": "ctx_abc"},
        )

    @pytest.mark.asyncio
    async def test_destroy_unknown_raises(self) -> None:
        """Should raise KeyError for unknown session."""
        manager = _make_manager()

        with pytest.raises(KeyError):
            await manager.destroy("nonexistent")

    def test_list_sessions_returns_info(self) -> None:
        """Should return session info including browser_context_id."""
        manager = _make_manager()
        instance = make_chrome_instance(
            "ses_abc", port=9222, pid=1234, browser_context_id="ctx_abc"
        )
        manager._instances["ses_abc"] = instance

        sessions = manager.list_sessions()

        assert "ses_abc" in sessions
        assert sessions["ses_abc"]["port"] == 9222
        assert sessions["ses_abc"]["pid"] == 1234
        assert sessions["ses_abc"]["tab_count"] == 1
        assert sessions["ses_abc"]["connected"] is False
        assert sessions["ses_abc"]["browser_context_id"] == "ctx_abc"

    @pytest.mark.asyncio
    async def test_cleanup_all_destroys_all(self) -> None:
        """Should destroy all instances and kill shared Chrome."""
        manager = _make_manager()
        instance1 = make_chrome_instance("ses_1", port=9222, browser_context_id="ctx_1")
        instance2 = make_chrome_instance("ses_2", port=9222, browser_context_id="ctx_2")
        manager._instances = {"ses_1": instance1, "ses_2": instance2}

        mock_browser_cdp = _make_mock_browser_cdp()
        manager._browser_cdp = mock_browser_cdp

        with patch.object(manager, "_kill_shared_chrome", new_callable=AsyncMock):
            await manager.cleanup_all()

        assert len(manager._instances) == 0

    @pytest.mark.asyncio
    async def test_get_or_create_cleans_up_context_on_failure(self) -> None:
        """Should dispose browser context if target creation fails."""
        manager = _make_manager()

        mock_browser_cdp = _make_mock_browser_cdp()
        mock_browser_cdp.send = AsyncMock(
            side_effect=[
                # Target.createBrowserContext succeeds
                {"browserContextId": "ctx_fail"},
                # Target.createTarget fails
                RuntimeError("Target creation failed"),
            ]
        )
        manager._browser_cdp = mock_browser_cdp
        manager._shared_proxy = make_mock_proxy()
        manager._shared_pid = 9999

        with (
            patch.object(manager, "_ensure_shared_chrome", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="Target creation failed"),
        ):
            await manager.get_or_create("ses_fail")

        assert "ses_fail" not in manager._instances
        # Should have attempted to dispose the browser context
        assert mock_browser_cdp.send.call_count == 3  # create ctx + create target + dispose ctx


# --- Tab Operations Tests ---


class TestChromePoolManagerTabs:
    """Tests for tab management within sessions."""

    @pytest.mark.asyncio
    async def test_create_tab_adds_to_instance(self) -> None:
        """Should create tab via browser CDP and add to instance."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc", browser_context_id="ctx_abc")
        manager._instances["ses_abc"] = instance

        mock_browser_cdp = _make_mock_browser_cdp()
        mock_browser_cdp.send = AsyncMock(
            side_effect=[
                # Target.createTarget
                {"targetId": "T2"},
                # Target.activateTarget (from switch_tab)
                {},
            ]
        )
        manager._browser_cdp = mock_browser_cdp

        with patch.object(manager, "_connect_cdp", new_callable=AsyncMock):
            target_id = await manager.create_tab("ses_abc", "https://example.com")

        assert target_id == "T2"
        assert "T2" in instance.targets

    @pytest.mark.asyncio
    async def test_switch_tab_updates_current(self) -> None:
        """Should update current target."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc")
        instance.targets = ["T1", "T2"]
        manager._instances["ses_abc"] = instance

        mock_browser_cdp = _make_mock_browser_cdp()
        mock_browser_cdp.send = AsyncMock(return_value={})
        manager._browser_cdp = mock_browser_cdp

        with patch.object(manager, "_connect_cdp", new_callable=AsyncMock):
            await manager.switch_tab("ses_abc", "T2")

        assert instance.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_rejects_unknown(self) -> None:
        """Should reject switching to unknown target."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc")
        manager._instances["ses_abc"] = instance

        with pytest.raises(ValueError, match="does not belong"):
            await manager.switch_tab("ses_abc", "UNKNOWN")

    @pytest.mark.asyncio
    async def test_close_tab_removes_from_instance(self) -> None:
        """Should close tab and remove from tracking."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc")
        instance.targets = ["T1", "T2"]
        instance.current_target_id = "T2"
        manager._instances["ses_abc"] = instance

        with patch.object(manager, "_connect_cdp", new_callable=AsyncMock):
            await manager.close_tab("ses_abc", "T2")

        assert "T2" not in instance.targets
        assert instance.current_target_id == "T1"

    @pytest.mark.asyncio
    async def test_close_last_tab_raises(self) -> None:
        """Should not allow closing last tab."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc")
        manager._instances["ses_abc"] = instance

        with pytest.raises(ValueError, match="Cannot close the last tab"):
            await manager.close_tab("ses_abc", "T1")

    @pytest.mark.asyncio
    async def test_list_tabs_returns_session_tabs(self) -> None:
        """Should list tabs for session."""
        manager = _make_manager()
        instance = make_chrome_instance("ses_abc")
        instance.proxy.list_targets = AsyncMock(
            return_value=[
                {"id": "T1", "type": "page", "title": "Tab 1", "url": "https://example.com"},
                {"id": "T2", "type": "page", "title": "Other", "url": "https://other.com"},
            ]
        )
        manager._instances["ses_abc"] = instance

        tabs = await manager.list_tabs("ses_abc")

        assert len(tabs) == 1  # Only T1 is in instance.targets
        assert tabs[0]["id"] == "T1"
        assert tabs[0]["is_current"] is True


# --- Integration-style Tests ---


class TestChromePoolManagerIntegration:
    """Higher-level integration tests."""

    def test_multiple_sessions_share_port(self) -> None:
        """All sessions should share the same port."""
        manager = _make_manager()

        inst1 = make_chrome_instance("ses_1", port=9222, browser_context_id="ctx_1")
        inst2 = make_chrome_instance("ses_2", port=9222, browser_context_id="ctx_2")
        inst3 = make_chrome_instance("ses_3", port=9222, browser_context_id="ctx_3")

        manager._instances = {"ses_1": inst1, "ses_2": inst2, "ses_3": inst3}

        # All share port 9222
        ports = {inst.port for inst in manager._instances.values()}
        assert ports == {9222}

        # But have different browser contexts
        contexts = {inst.browser_context_id for inst in manager._instances.values()}
        assert len(contexts) == 3

    def test_session_isolation(self) -> None:
        """Sessions should have independent tab tracking."""
        manager = _make_manager()

        inst1 = make_chrome_instance("ses_1", port=9222, browser_context_id="ctx_1")
        inst1.targets = ["A1", "A2"]

        inst2 = make_chrome_instance("ses_2", port=9222, browser_context_id="ctx_2")
        inst2.targets = ["B1"]

        manager._instances = {"ses_1": inst1, "ses_2": inst2}

        # Each session has its own tabs
        assert inst1.targets == ["A1", "A2"]
        assert inst2.targets == ["B1"]

        # Modifying one doesn't affect the other
        inst1.targets.append("A3")
        assert "A3" not in inst2.targets


# --- Orphaned Temp Dir Cleanup Tests ---


class TestChromePoolManagerCleanup:
    """Tests for orphaned temp directory cleanup."""

    def _mock_run_windows(self, stdout: str = "", returncode: int = 0) -> MagicMock:
        """Create a mock for run_windows_command."""
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stdout = stdout
        result.returncode = returncode
        return result

    def test_cleanup_orphaned_dirs_runs_powershell(self) -> None:
        """Should execute PowerShell to clean old chrome-mcp-* dirs."""
        with patch("wsl_chrome_mcp.chrome_pool.run_windows_command") as mock_run:
            mock_run.return_value = self._mock_run_windows("chrome-mcp-old1\nchrome-mcp-old2")
            ChromePoolManager()

            assert mock_run.called
            call_args = mock_run.call_args_list[0][0][0]
            assert "chrome-mcp-*" in call_args
            assert "AddHours(-24)" in call_args

    def test_cleanup_orphaned_dirs_handles_errors(self) -> None:
        """Should not raise when cleanup fails."""
        with patch(
            "wsl_chrome_mcp.chrome_pool.run_windows_command",
            side_effect=Exception("PowerShell not available"),
        ):
            manager = ChromePoolManager()
            assert manager is not None
