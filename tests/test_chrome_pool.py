"""Tests for ChromePoolManager - per-session Chrome instance management."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wsl_chrome_mcp.chrome_pool import ChromeInstance, ChromePoolManager

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
        ws_urls={"T1": f"ws://localhost:{port}/devtools/page/T1"},
    )


# --- ChromeInstance Tests ---


class TestChromeInstance:
    """Tests for ChromeInstance dataclass."""

    def test_current_ws_url_returns_url(self) -> None:
        """Should return WebSocket URL for current target."""
        instance = make_chrome_instance()
        assert instance.current_ws_url == "ws://localhost:9222/devtools/page/T1"

    def test_current_ws_url_returns_none_when_no_target(self) -> None:
        """Should return None when no current target."""
        instance = make_chrome_instance()
        instance.current_target_id = None
        assert instance.current_ws_url is None

    def test_current_ws_url_returns_none_when_target_not_found(self) -> None:
        """Should return None when target not in ws_urls."""
        instance = make_chrome_instance()
        instance.current_target_id = "UNKNOWN"
        assert instance.current_ws_url is None


# --- ChromePoolManager Port Allocation Tests ---


class TestChromePoolManagerPorts:
    """Tests for port allocation."""

    def test_is_port_in_use_returns_true_when_responding(self) -> None:
        """Should return True when port responds to HTTP request."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)

        with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client._make_http_request.return_value = {"Browser": "Chrome"}
            mock_client_class.return_value = mock_client

            assert manager._is_port_in_use(9222) is True
            mock_client._make_http_request.assert_called_once_with("/json/version")

    def test_is_port_in_use_returns_false_when_not_responding(self) -> None:
        """Should return False when port doesn't respond."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)

        with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client._make_http_request.return_value = None
            mock_client_class.return_value = mock_client

            assert manager._is_port_in_use(9222) is False

    def test_is_port_in_use_returns_false_on_exception(self) -> None:
        """Should return False when request raises exception."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)

        with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client._make_http_request.side_effect = Exception("Connection refused")
            mock_client_class.return_value = mock_client

            assert manager._is_port_in_use(9222) is False

    def test_allocate_port_returns_first_available(self) -> None:
        """Should return first port in range."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)

        with patch.object(manager, "_is_port_in_use", return_value=False):
            port = manager._allocate_port()
            assert port == 9222

    def test_allocate_port_skips_used_ports(self) -> None:
        """Should skip already used ports."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)
        manager._used_ports.add(9222)
        manager._used_ports.add(9223)

        with patch.object(manager, "_is_port_in_use", return_value=False):
            port = manager._allocate_port()
            assert port == 9224

    def test_allocate_port_skips_externally_used_ports(self) -> None:
        """Should skip ports that are externally in use (orphaned Chrome)."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)

        # Mock: 9222 and 9223 are externally in use, 9224 is free
        def mock_is_port_in_use(port: int) -> bool:
            return port in (9222, 9223)

        with patch.object(manager, "_is_port_in_use", side_effect=mock_is_port_in_use):
            port = manager._allocate_port()
            assert port == 9224
            # Externally used ports should be added to _used_ports
            assert 9222 in manager._used_ports
            assert 9223 in manager._used_ports

    def test_allocate_port_raises_when_exhausted(self) -> None:
        """Should raise when no ports available."""
        manager = ChromePoolManager(port_min=9222, port_max=9224)
        manager._used_ports = {9222, 9223}
        with pytest.raises(RuntimeError, match="No available ports"):
            manager._allocate_port()

    def test_release_port_makes_port_available(self) -> None:
        """Should return port to available pool."""
        manager = ChromePoolManager(port_min=9222, port_max=9225)
        manager._used_ports.add(9222)
        manager._release_port(9222)
        assert 9222 not in manager._used_ports


# --- ChromePoolManager Session Tests ---


class TestChromePoolManagerSessions:
    """Tests for session management."""

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self) -> None:
        """Should return cached instance on second call."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc", port=9222)
        manager._instances["ses_abc"] = instance
        manager._used_ports.add(9222)

        result = await manager.get_or_create("ses_abc")

        assert result is instance

    @pytest.mark.asyncio
    async def test_destroy_removes_instance(self) -> None:
        """Should remove instance and release port."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc", port=9222)
        manager._instances["ses_abc"] = instance
        manager._used_ports.add(9222)

        with patch.object(manager, "_kill_chrome", new_callable=AsyncMock):
            await manager.destroy("ses_abc")

        assert "ses_abc" not in manager._instances
        assert 9222 not in manager._used_ports

    @pytest.mark.asyncio
    async def test_destroy_unknown_raises(self) -> None:
        """Should raise KeyError for unknown session."""
        manager = ChromePoolManager()

        with pytest.raises(KeyError):
            await manager.destroy("nonexistent")

    def test_list_sessions_returns_info(self) -> None:
        """Should return session info."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc", port=9222, pid=1234)
        manager._instances["ses_abc"] = instance

        sessions = manager.list_sessions()

        assert "ses_abc" in sessions
        assert sessions["ses_abc"]["port"] == 9222
        assert sessions["ses_abc"]["pid"] == 1234
        assert sessions["ses_abc"]["tab_count"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_all_destroys_all(self) -> None:
        """Should destroy all instances."""
        manager = ChromePoolManager()
        instance1 = make_chrome_instance("ses_1", port=9222)
        instance2 = make_chrome_instance("ses_2", port=9223)
        manager._instances = {"ses_1": instance1, "ses_2": instance2}
        manager._used_ports = {9222, 9223}

        with patch.object(manager, "_kill_chrome", new_callable=AsyncMock):
            await manager.cleanup_all()

        assert len(manager._instances) == 0
        assert len(manager._used_ports) == 0


# --- Tab Operations Tests ---


class TestChromePoolManagerTabs:
    """Tests for tab management within sessions."""

    @pytest.mark.asyncio
    async def test_create_tab_adds_to_instance(self) -> None:
        """Should create tab and add to instance."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc")
        manager._instances["ses_abc"] = instance

        # Mock the target listing after creation
        instance.proxy.list_targets = AsyncMock(
            return_value=[
                {
                    "id": "T1",
                    "type": "page",
                    "url": "about:blank",
                    "webSocketDebuggerUrl": "ws://...",
                },
                {
                    "id": "T2",
                    "type": "page",
                    "url": "https://example.com",
                    "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T2",
                },
            ]
        )

        target_id = await manager.create_tab("ses_abc", "https://example.com")

        assert target_id == "T2"
        assert "T2" in instance.targets
        assert instance.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_updates_current(self) -> None:
        """Should update current target."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc")
        instance.targets = ["T1", "T2"]
        instance.ws_urls["T2"] = "ws://localhost:9222/devtools/page/T2"
        manager._instances["ses_abc"] = instance

        await manager.switch_tab("ses_abc", "T2")

        assert instance.current_target_id == "T2"

    @pytest.mark.asyncio
    async def test_switch_tab_rejects_unknown(self) -> None:
        """Should reject switching to unknown target."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc")
        manager._instances["ses_abc"] = instance

        with pytest.raises(ValueError, match="does not belong"):
            await manager.switch_tab("ses_abc", "UNKNOWN")

    @pytest.mark.asyncio
    async def test_close_tab_removes_from_instance(self) -> None:
        """Should close tab and remove from tracking."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc")
        instance.targets = ["T1", "T2"]
        instance.ws_urls["T2"] = "ws://..."
        instance.current_target_id = "T2"
        manager._instances["ses_abc"] = instance

        await manager.close_tab("ses_abc", "T2")

        assert "T2" not in instance.targets
        assert instance.current_target_id == "T1"

    @pytest.mark.asyncio
    async def test_close_last_tab_raises(self) -> None:
        """Should not allow closing last tab."""
        manager = ChromePoolManager()
        instance = make_chrome_instance("ses_abc")
        manager._instances["ses_abc"] = instance

        with pytest.raises(ValueError, match="Cannot close the last tab"):
            await manager.close_tab("ses_abc", "T1")

    @pytest.mark.asyncio
    async def test_list_tabs_returns_session_tabs(self) -> None:
        """Should list tabs for session."""
        manager = ChromePoolManager()
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

    def test_multiple_sessions_get_different_ports(self) -> None:
        """Multiple sessions should have different ports."""
        manager = ChromePoolManager(port_min=9222, port_max=9230)

        inst1 = make_chrome_instance("ses_1", port=9222)
        inst2 = make_chrome_instance("ses_2", port=9223)
        inst3 = make_chrome_instance("ses_3", port=9224)

        manager._instances = {"ses_1": inst1, "ses_2": inst2, "ses_3": inst3}
        manager._used_ports = {9222, 9223, 9224}

        ports = {inst.port for inst in manager._instances.values()}
        assert len(ports) == 3  # All different

    def test_session_isolation(self) -> None:
        """Sessions should have independent tab tracking."""
        manager = ChromePoolManager()

        inst1 = make_chrome_instance("ses_1", port=9222)
        inst1.targets = ["A1", "A2"]

        inst2 = make_chrome_instance("ses_2", port=9223)
        inst2.targets = ["B1"]

        manager._instances = {"ses_1": inst1, "ses_2": inst2}

        # Each session has its own tabs
        assert inst1.targets == ["A1", "A2"]
        assert inst2.targets == ["B1"]

        # Modifying one doesn't affect the other
        inst1.targets.append("A3")
        assert "A3" not in inst2.targets
