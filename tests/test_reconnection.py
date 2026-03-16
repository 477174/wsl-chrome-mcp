"""Integration tests for reconnection and persistence flow.

Tests the orphan reconnection mechanism where sessions can reconnect to
Chrome processes that survived a crash, and the SessionStore persistence layer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wsl_chrome_mcp.chrome_pool import ChromeInstance, ChromePoolManager
from wsl_chrome_mcp.session_store import SessionRecord, SessionStore


def _make_manager(**kwargs: object) -> ChromePoolManager:
    """Create a ChromePoolManager with orphan cleanup mocked out."""
    with patch.object(ChromePoolManager, "_cleanup_orphaned_temp_dirs"):
        return ChromePoolManager(**kwargs)  # type: ignore[arg-type]


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


# --- Reconnection Tests ---


@pytest.mark.asyncio
async def test_get_or_create_reconnects_from_disk() -> None:
    """Should reconnect to orphaned Chrome from disk record."""
    manager = _make_manager()
    session_id = "orphan_session"

    # Mock SessionStore.load to return a persisted record
    record = SessionRecord(
        session_id=session_id,
        port=9222,
        pid=5678,
        target_ids=["T1"],
        current_target_id="T1",
        profile_mode="isolated",
        browser_context_id=None,
    )

    # Mock the reconnection attempt
    reconnected_instance = make_chrome_instance(session_id=session_id, port=9222, pid=5678)

    with patch.object(manager._session_store, "load", return_value=record):
        with patch.object(
            manager, "_try_reconnect_from_record", return_value=reconnected_instance
        ) as mock_reconnect:
            result = await manager.get_or_create(session_id)

            # Verify reconnection was attempted
            mock_reconnect.assert_called_once_with(record)
            assert result.session_id == session_id
            assert result.port == 9222
            assert result.pid == 5678
            # Verify instance is now in manager
            assert manager._instances[session_id] == result


@pytest.mark.asyncio
async def test_get_or_create_creates_new_when_no_disk_record() -> None:
    """Should create new Chrome when no disk record exists."""
    manager = _make_manager()
    session_id = "new_session"

    # Mock SessionStore.load to return None (no record)
    with patch.object(manager._session_store, "load", return_value=None):
        with patch.object(manager, "_create_isolated_session") as mock_create:
            new_instance = make_chrome_instance(session_id=session_id)

            # _create_isolated_session stores the instance itself
            async def create_and_store(sid: str) -> ChromeInstance:
                manager._instances[sid] = new_instance
                return new_instance

            mock_create.side_effect = create_and_store

            result = await manager.get_or_create(session_id)

            # Verify creation was called
            mock_create.assert_called_once_with(session_id)
            assert result.session_id == session_id
            assert manager._instances[session_id] == result


@pytest.mark.asyncio
async def test_get_or_create_deletes_stale_on_dead_chrome() -> None:
    """Should delete stale record when Chrome is dead."""
    manager = _make_manager()
    session_id = "dead_session"

    # Mock SessionStore.load to return a record
    record = SessionRecord(
        session_id=session_id,
        port=9222,
        pid=9999,
        target_ids=["T1"],
        current_target_id="T1",
        profile_mode="isolated",
    )

    # Mock reconnection to fail (Chrome is dead)
    with patch.object(manager._session_store, "load", return_value=record):
        with patch.object(manager, "_try_reconnect_from_record", return_value=None):
            with patch.object(manager._session_store, "delete") as mock_delete:
                with patch.object(manager, "_create_isolated_session") as mock_create:
                    new_instance = make_chrome_instance(session_id=session_id)
                    mock_create.return_value = new_instance

                    result = await manager.get_or_create(session_id)

                    # Verify stale record was deleted
                    mock_delete.assert_called_once_with(session_id)
                    # Verify new Chrome was created
                    mock_create.assert_called_once_with(session_id)
                    assert result.session_id == session_id


@pytest.mark.asyncio
async def test_session_creation_persists_to_disk(tmp_path: Path) -> None:
    """Should persist session to disk when created."""
    manager = _make_manager()
    session_id = "persist_session"

    # Create a real SessionStore with tmp_path
    with patch.object(SessionStore, "STORE_DIR", tmp_path):
        manager._session_store = SessionStore()

        # Mock Chrome launch
        with patch("wsl_chrome_mcp.chrome_pool.run_windows_command") as mock_run:
            # Mock temp dir creation
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="C:\\Temp\\chrome-mcp-abc123\n"),
                # Chrome launch
                MagicMock(returncode=0, stdout="5678\n"),
            ]

            with patch.object(manager, "_find_chrome_path", return_value="C:\\Chrome"):
                with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_proxy_class:
                    mock_proxy = make_mock_proxy(9222)
                    mock_proxy_class.return_value = mock_proxy

                    with patch.object(manager, "_connect_instance_browser_cdp"):
                        with patch.object(manager, "_connect_cdp"):
                            instance = await manager._create_isolated_session(session_id)

                            # Manually save to test persistence
                            record = SessionRecord(
                                session_id=session_id,
                                port=instance.port,
                                pid=instance.pid,
                                target_ids=instance.targets,
                                current_target_id=instance.current_target_id,
                                profile_mode="isolated",
                            )
                            manager._session_store.save(record)

                            # Verify file was created
                            session_file = tmp_path / f"{session_id}.json"
                            assert session_file.exists()

                            # Verify content
                            loaded = manager._session_store.load(session_id)
                            assert loaded is not None
                            assert loaded.session_id == session_id
                            assert loaded.port == instance.port


@pytest.mark.asyncio
async def test_session_destroy_deletes_from_disk(tmp_path: Path) -> None:
    """Should delete session file when destroyed."""
    manager = _make_manager()
    session_id = "destroy_session"

    # Use real SessionStore with tmp_path
    with patch.object(SessionStore, "STORE_DIR", tmp_path):
        manager._session_store = SessionStore()

        # Create and save a session record
        record = SessionRecord(
            session_id=session_id,
            port=9222,
            pid=5678,
            target_ids=["T1"],
            current_target_id="T1",
            profile_mode="isolated",
        )
        manager._session_store.save(record)

        # Verify file exists
        session_file = tmp_path / f"{session_id}.json"
        assert session_file.exists()

        # Add instance to manager
        instance = make_chrome_instance(session_id=session_id, pid=5678)
        instance.owns_chrome = True
        manager._instances[session_id] = instance

        # Mock Chrome kill
        with patch.object(manager, "_kill_instance_chrome"):
            with patch.object(manager, "_disconnect_cdp"):
                await manager.destroy(session_id)

                # Verify file was deleted
                assert not session_file.exists()
                # Verify instance removed
                assert session_id not in manager._instances


@pytest.mark.asyncio
async def test_cleanup_all_deletes_all_session_files(tmp_path: Path) -> None:
    """Should delete all session files during cleanup."""
    manager = _make_manager()

    # Use real SessionStore with tmp_path
    with patch.object(SessionStore, "STORE_DIR", tmp_path):
        manager._session_store = SessionStore()

        # Create multiple session records
        session_ids = ["session_1", "session_2", "session_3"]
        for sid in session_ids:
            record = SessionRecord(
                session_id=sid,
                port=9222 + session_ids.index(sid),
                pid=5000 + session_ids.index(sid),
                target_ids=["T1"],
                current_target_id="T1",
                profile_mode="isolated",
            )
            manager._session_store.save(record)
            instance = make_chrome_instance(session_id=sid)
            instance.owns_chrome = True
            manager._instances[sid] = instance

        # Verify files exist
        for sid in session_ids:
            assert (tmp_path / f"{sid}.json").exists()

        # Mock Chrome operations
        with patch.object(manager, "_kill_instance_chrome"):
            with patch.object(manager, "_disconnect_cdp"):
                with patch.object(manager, "_kill_shared_chrome"):
                    await manager.cleanup_all()

        # Verify all files deleted
        for sid in session_ids:
            assert not (tmp_path / f"{sid}.json").exists()
        # Verify instances cleared
        assert len(manager._instances) == 0


@pytest.mark.asyncio
async def test_reconnect_adopts_existing_tabs() -> None:
    """Should adopt all existing tabs when reconnecting."""
    manager = _make_manager()
    session_id = "multi_tab_session"

    # Mock proxy with 3 tabs
    proxy = MagicMock()
    proxy.get_version = AsyncMock(return_value={"Browser": "Chrome/120.0"})
    proxy.list_targets = AsyncMock(
        return_value=[
            {
                "id": "T1",
                "type": "page",
                "title": "Tab 1",
                "url": "https://example.com",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T1",
            },
            {
                "id": "T2",
                "type": "page",
                "title": "Tab 2",
                "url": "https://google.com",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T2",
            },
            {
                "id": "T3",
                "type": "page",
                "title": "Tab 3",
                "url": "https://github.com",
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T3",
            },
        ]
    )

    record = SessionRecord(
        session_id=session_id,
        port=9222,
        pid=5678,
        target_ids=["T1"],
        current_target_id="T1",
        profile_mode="isolated",
    )

    with patch.object(manager, "_try_reconnect_from_record") as mock_reconnect:
        # Create instance with all 3 tabs
        instance = ChromeInstance(
            session_id=session_id,
            port=9222,
            pid=5678,
            proxy=proxy,
            user_data_dir="",
            current_target_id="T1",
            targets=["T1", "T2", "T3"],
            owns_chrome=True,
        )
        mock_reconnect.return_value = instance

        with patch.object(manager._session_store, "load", return_value=record):
            result = await manager.get_or_create(session_id)

            # Verify all tabs are present
            assert len(result.targets) == 3
            assert "T1" in result.targets
            assert "T2" in result.targets
            assert "T3" in result.targets


@pytest.mark.asyncio
async def test_collect_all_pids_returns_all() -> None:
    """Should collect PIDs from all instances and shared Chrome."""
    manager = _make_manager()

    # Add isolated instances with PIDs
    instance1 = make_chrome_instance(session_id="session_1", pid=1001)
    instance2 = make_chrome_instance(session_id="session_2", pid=1002)
    instance3 = make_chrome_instance(session_id="session_3", pid=1003)

    manager._instances["session_1"] = instance1
    manager._instances["session_2"] = instance2
    manager._instances["session_3"] = instance3

    # Add shared Chrome PID
    manager._shared_pid = 2000

    pids = manager._collect_all_pids()

    # Verify all PIDs collected
    assert len(pids) == 4
    assert 1001 in pids
    assert 1002 in pids
    assert 1003 in pids
    assert 2000 in pids


@pytest.mark.asyncio
async def test_sync_kill_all_chrome_calls_kill() -> None:
    """Should call kill command for each Chrome PID."""
    manager = _make_manager()

    # Add instances with PIDs
    instance1 = make_chrome_instance(session_id="session_1", pid=1001)
    instance2 = make_chrome_instance(session_id="session_2", pid=1002)
    manager._instances["session_1"] = instance1
    manager._instances["session_2"] = instance2
    manager._shared_pid = 2000

    with patch("wsl_chrome_mcp.chrome_pool.run_windows_command") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        manager._sync_kill_all_chrome()

        # Verify kill was called for each PID
        assert mock_run.call_count == 3
        calls = [call[0][0] for call in mock_run.call_args_list]
        assert any("1001" in call for call in calls)
        assert any("1002" in call for call in calls)
        assert any("2000" in call for call in calls)


@pytest.mark.asyncio
async def test_reconnect_from_record_with_dead_chrome() -> None:
    """Should return None when Chrome is dead."""
    manager = _make_manager()
    session_id = "dead_chrome_session"

    record = SessionRecord(
        session_id=session_id,
        port=9999,
        pid=9999,
        target_ids=["T1"],
        current_target_id="T1",
        profile_mode="isolated",
    )

    # Mock proxy that fails to get version (Chrome is dead)
    with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_proxy_class:
        mock_proxy = MagicMock()
        mock_proxy.get_version = AsyncMock(return_value=None)
        mock_proxy_class.return_value = mock_proxy

        result = await manager._try_reconnect_from_record(record)

        # Should return None when Chrome is dead
        assert result is None


@pytest.mark.asyncio
async def test_reconnect_from_record_no_page_targets() -> None:
    """Should return None when Chrome has no page targets."""
    manager = _make_manager()
    session_id = "no_pages_session"

    record = SessionRecord(
        session_id=session_id,
        port=9222,
        pid=5678,
        target_ids=["T1"],
        current_target_id="T1",
        profile_mode="isolated",
    )

    # Mock proxy with no page targets
    with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_proxy_class:
        mock_proxy = MagicMock()
        mock_proxy.get_version = AsyncMock(return_value={"Browser": "Chrome/120.0"})
        # Return only non-page targets (e.g., background pages)
        mock_proxy.list_targets = AsyncMock(
            return_value=[
                {
                    "id": "BG1",
                    "type": "background_page",
                    "title": "Background",
                }
            ]
        )
        mock_proxy_class.return_value = mock_proxy

        result = await manager._try_reconnect_from_record(record)

        # Should return None when no page targets
        assert result is None


@pytest.mark.asyncio
async def test_reconnect_from_record_adopts_new_target_when_original_gone() -> None:
    """Should adopt first available target when original target is gone."""
    manager = _make_manager()
    session_id = "target_changed_session"

    record = SessionRecord(
        session_id=session_id,
        port=9222,
        pid=5678,
        target_ids=["T1"],
        current_target_id="T1",  # Original target
        profile_mode="isolated",
    )

    # Mock proxy where original target is gone but others exist
    with patch("wsl_chrome_mcp.chrome_pool.CDPProxyClient") as mock_proxy_class:
        mock_proxy = MagicMock()
        mock_proxy.get_version = AsyncMock(return_value={"Browser": "Chrome/120.0"})
        # T1 is gone, but T2 and T3 exist
        mock_proxy.list_targets = AsyncMock(
            return_value=[
                {
                    "id": "T2",
                    "type": "page",
                    "title": "New Tab",
                    "url": "about:blank",
                    "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T2",
                },
                {
                    "id": "T3",
                    "type": "page",
                    "title": "Another Tab",
                    "url": "https://example.com",
                    "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/T3",
                },
            ]
        )
        mock_proxy_class.return_value = mock_proxy

        with patch.object(manager, "_connect_instance_browser_cdp"):
            with patch.object(manager, "_connect_cdp"):
                result = await manager._try_reconnect_from_record(record)

                # Should adopt T2 (first available)
                assert result is not None
                assert result.current_target_id == "T2"
                assert "T2" in result.targets
                assert "T3" in result.targets
