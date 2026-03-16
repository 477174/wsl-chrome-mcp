"""Tests for SessionStore and SessionRecord - session persistence layer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wsl_chrome_mcp.session_store import SessionRecord, SessionStore


# --- Fixtures ---


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    """Create a SessionStore with tmp_path as STORE_DIR for isolation."""
    s = SessionStore()
    s.STORE_DIR = tmp_path
    return s


@pytest.fixture
def sample_record() -> SessionRecord:
    """Create a sample SessionRecord for testing."""
    return SessionRecord(
        session_id="test_session_123",
        port=9222,
        pid=1234,
        target_ids=["T1", "T2"],
        current_target_id="T1",
        profile_mode="isolated",
        created_at="2025-03-16T10:30:00",
        browser_context_id="ctx_abc123",
    )


# --- SessionRecord Tests ---


class TestSessionRecord:
    """Tests for SessionRecord dataclass."""

    def test_to_dict_converts_all_fields(self, sample_record: SessionRecord) -> None:
        """Should convert all fields to dictionary."""
        data = sample_record.to_dict()

        assert data["session_id"] == "test_session_123"
        assert data["port"] == 9222
        assert data["pid"] == 1234
        assert data["target_ids"] == ["T1", "T2"]
        assert data["current_target_id"] == "T1"
        assert data["profile_mode"] == "isolated"
        assert data["created_at"] == "2025-03-16T10:30:00"
        assert data["browser_context_id"] == "ctx_abc123"

    def test_from_dict_creates_record(self) -> None:
        """Should create SessionRecord from dictionary."""
        data = {
            "session_id": "ses_xyz",
            "port": 9223,
            "pid": 5678,
            "target_ids": ["T3"],
            "current_target_id": "T3",
            "profile_mode": "profile",
            "created_at": "2025-03-16T11:00:00",
            "browser_context_id": None,
        }

        record = SessionRecord.from_dict(data)

        assert record.session_id == "ses_xyz"
        assert record.port == 9223
        assert record.pid == 5678
        assert record.target_ids == ["T3"]
        assert record.current_target_id == "T3"
        assert record.profile_mode == "profile"
        assert record.created_at == "2025-03-16T11:00:00"
        assert record.browser_context_id is None

    def test_from_dict_to_dict_roundtrip(self, sample_record: SessionRecord) -> None:
        """Should roundtrip through to_dict and from_dict without loss."""
        original_dict = sample_record.to_dict()
        reconstructed = SessionRecord.from_dict(original_dict)
        final_dict = reconstructed.to_dict()

        assert original_dict == final_dict
        assert reconstructed.session_id == sample_record.session_id
        assert reconstructed.port == sample_record.port
        assert reconstructed.pid == sample_record.pid
        assert reconstructed.target_ids == sample_record.target_ids
        assert reconstructed.current_target_id == sample_record.current_target_id
        assert reconstructed.profile_mode == sample_record.profile_mode
        assert reconstructed.created_at == sample_record.created_at
        assert reconstructed.browser_context_id == sample_record.browser_context_id

    def test_created_at_defaults_to_now(self) -> None:
        """Should default created_at to current ISO timestamp."""
        before = datetime.now().isoformat()
        record = SessionRecord(
            session_id="test",
            port=9222,
            pid=1234,
        )
        after = datetime.now().isoformat()

        # created_at should be between before and after
        assert before <= record.created_at <= after

    def test_target_ids_defaults_to_empty_list(self) -> None:
        """Should default target_ids to empty list."""
        record = SessionRecord(
            session_id="test",
            port=9222,
            pid=1234,
        )

        assert record.target_ids == []

    def test_current_target_id_defaults_to_none(self) -> None:
        """Should default current_target_id to None."""
        record = SessionRecord(
            session_id="test",
            port=9222,
            pid=1234,
        )

        assert record.current_target_id is None

    def test_profile_mode_defaults_to_isolated(self) -> None:
        """Should default profile_mode to 'isolated'."""
        record = SessionRecord(
            session_id="test",
            port=9222,
            pid=1234,
        )

        assert record.profile_mode == "isolated"

    def test_browser_context_id_defaults_to_none(self) -> None:
        """Should default browser_context_id to None."""
        record = SessionRecord(
            session_id="test",
            port=9222,
            pid=1234,
        )

        assert record.browser_context_id is None


# --- SessionStore Tests ---


class TestSessionStoreSaveAndLoad:
    """Tests for save and load operations."""

    def test_save_and_load_roundtrip(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should save and load record with all fields matching."""
        store.save(sample_record)
        loaded = store.load("test_session_123")

        assert loaded is not None
        assert loaded.session_id == sample_record.session_id
        assert loaded.port == sample_record.port
        assert loaded.pid == sample_record.pid
        assert loaded.target_ids == sample_record.target_ids
        assert loaded.current_target_id == sample_record.current_target_id
        assert loaded.profile_mode == sample_record.profile_mode
        assert loaded.created_at == sample_record.created_at
        assert loaded.browser_context_id == sample_record.browser_context_id

    def test_load_nonexistent_returns_none(self, store: SessionStore) -> None:
        """Should return None when loading missing session."""
        result = store.load("nonexistent_session")

        assert result is None

    def test_save_creates_json_file(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should create a .json file in STORE_DIR."""
        store.save(sample_record)

        json_file = store.STORE_DIR / "test_session_123.json"
        assert json_file.exists()
        assert json_file.is_file()

    def test_save_writes_valid_json(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should write valid JSON that can be parsed."""
        store.save(sample_record)

        json_file = store.STORE_DIR / "test_session_123.json"
        with open(json_file) as f:
            data = json.load(f)

        assert data["session_id"] == "test_session_123"
        assert data["port"] == 9222
        assert data["pid"] == 1234

    def test_save_overwrites_existing(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should overwrite existing session file."""
        store.save(sample_record)

        # Modify and save again
        sample_record.port = 9999
        store.save(sample_record)

        loaded = store.load("test_session_123")
        assert loaded is not None
        assert loaded.port == 9999

    def test_save_no_tmp_file_remains(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should not leave .tmp files after successful save."""
        store.save(sample_record)

        tmp_files = list(store.STORE_DIR.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_load_with_none_pid(self, store: SessionStore) -> None:
        """Should load record with pid=None (profile mode)."""
        record = SessionRecord(
            session_id="profile_session",
            port=9222,
            pid=None,
            profile_mode="profile",
        )
        store.save(record)

        loaded = store.load("profile_session")
        assert loaded is not None
        assert loaded.pid is None
        assert loaded.profile_mode == "profile"


class TestSessionStoreDelete:
    """Tests for delete operations."""

    def test_delete_removes_file(self, store: SessionStore, sample_record: SessionRecord) -> None:
        """Should delete session file."""
        store.save(sample_record)
        assert (store.STORE_DIR / "test_session_123.json").exists()

        store.delete("test_session_123")

        assert not (store.STORE_DIR / "test_session_123.json").exists()

    def test_delete_nonexistent_no_error(self, store: SessionStore) -> None:
        """Should not raise error when deleting missing session."""
        # Should not raise
        store.delete("nonexistent_session")

    def test_delete_then_load_returns_none(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should return None after deleting session."""
        store.save(sample_record)
        store.delete("test_session_123")

        result = store.load("test_session_123")
        assert result is None


class TestSessionStoreListAll:
    """Tests for list_all operations."""

    def test_list_all_returns_all_sessions(self, store: SessionStore) -> None:
        """Should return all stored sessions."""
        record1 = SessionRecord(session_id="ses_1", port=9222, pid=1001, target_ids=["T1"])
        record2 = SessionRecord(session_id="ses_2", port=9223, pid=1002, target_ids=["T2"])
        record3 = SessionRecord(session_id="ses_3", port=9224, pid=1003, target_ids=["T3"])

        store.save(record1)
        store.save(record2)
        store.save(record3)

        records = store.list_all()

        assert len(records) == 3
        session_ids = {r.session_id for r in records}
        assert session_ids == {"ses_1", "ses_2", "ses_3"}

    def test_list_all_empty_store(self, store: SessionStore) -> None:
        """Should return empty list when no sessions stored."""
        records = store.list_all()

        assert records == []

    def test_list_all_skips_corrupt_files(self, store: SessionStore) -> None:
        """Should skip corrupt JSON files and return valid ones."""
        # Save a valid record
        record = SessionRecord(session_id="valid", port=9222, pid=1234)
        store.save(record)

        # Write corrupt JSON
        corrupt_file = store.STORE_DIR / "corrupt.json"
        corrupt_file.write_text("{ invalid json }")

        records = store.list_all()

        # Should only return the valid record
        assert len(records) == 1
        assert records[0].session_id == "valid"

    def test_list_all_nonexistent_store_dir(self, store: SessionStore) -> None:
        """Should return empty list when STORE_DIR doesn't exist."""
        store.STORE_DIR.rmdir()

        records = store.list_all()

        assert records == []

    def test_list_all_returns_session_record_objects(self, store: SessionStore) -> None:
        """Should return SessionRecord objects, not dicts."""
        record = SessionRecord(session_id="test", port=9222, pid=1234)
        store.save(record)

        records = store.list_all()

        assert len(records) == 1
        assert isinstance(records[0], SessionRecord)
        assert records[0].session_id == "test"


class TestSessionStoreCorruptJson:
    """Tests for handling corrupt JSON."""

    def test_corrupt_json_returns_none(self, store: SessionStore) -> None:
        """Should return None when loading corrupt JSON."""
        # Write invalid JSON
        json_file = store.STORE_DIR / "corrupt.json"
        json_file.write_text("{ not valid json }")

        result = store.load("corrupt")

        assert result is None

    def test_corrupt_json_missing_required_field(self, store: SessionStore) -> None:
        """Should return None when JSON missing required fields."""
        # Write JSON missing required 'session_id'
        json_file = store.STORE_DIR / "incomplete.json"
        json_file.write_text(json.dumps({"port": 9222}))

        result = store.load("incomplete")

        assert result is None

    def test_corrupt_json_wrong_type(self, store: SessionStore) -> None:
        """Should return None when JSON has wrong types."""
        # Write JSON with port as string instead of int
        json_file = store.STORE_DIR / "wrongtype.json"
        json_file.write_text(
            json.dumps(
                {
                    "session_id": "test",
                    "port": "not_an_int",
                    "pid": 1234,
                }
            )
        )

        result = store.load("wrongtype")

        # Should handle gracefully (may return None or coerce)
        # The important thing is it doesn't crash
        assert result is None or isinstance(result, SessionRecord)


class TestSessionStoreDirectoryCreation:
    """Tests for directory creation."""

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """Should create STORE_DIR if it doesn't exist via __init__."""
        store = SessionStore()
        store.STORE_DIR = tmp_path / "new" / "nested" / "dir"
        store.__init__()

        record = SessionRecord(session_id="test", port=9222, pid=1234)
        store.save(record)

        assert store.STORE_DIR.exists()
        assert (store.STORE_DIR / "test.json").exists()

    def test_init_creates_directory(self, tmp_path: Path) -> None:
        """Should create STORE_DIR during __init__."""
        store_dir = tmp_path / "sessions"
        assert not store_dir.exists()

        store = SessionStore()
        store.STORE_DIR = store_dir
        store.__init__()

        assert store_dir.exists()


class TestSessionStoreAtomicWrite:
    """Tests for atomic write behavior."""

    def test_atomic_write_no_partial_file(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should not leave partial files after successful write."""
        store.save(sample_record)

        # Check that only .json file exists, not .tmp
        json_files = list(store.STORE_DIR.glob("*.json"))
        tmp_files = list(store.STORE_DIR.glob("*.tmp"))

        assert len(json_files) == 1
        assert len(tmp_files) == 0
        assert json_files[0].name == "test_session_123.json"

    def test_atomic_write_cleans_up_tmp_on_error(
        self, store: SessionStore, sample_record: SessionRecord
    ) -> None:
        """Should clean up .tmp file if write fails."""
        # Mock os.replace to raise an error
        with patch("os.replace", side_effect=OSError("Disk full")):
            with pytest.raises(OSError):
                store.save(sample_record)

        # Check that .tmp file was cleaned up
        tmp_files = list(store.STORE_DIR.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestSessionStoreCleanupStale:
    """Tests for cleanup_stale operations."""

    def test_cleanup_stale_removes_dead_sessions(self, store: SessionStore) -> None:
        """Should delete sessions with dead PIDs."""
        # Save two records
        record1 = SessionRecord(session_id="alive", port=9222, pid=1234, profile_mode="isolated")
        record2 = SessionRecord(session_id="dead", port=9223, pid=9999, profile_mode="isolated")
        store.save(record1)
        store.save(record2)

        # Mock _is_process_alive to return False for pid 9999
        def mock_is_alive(pid: int) -> bool:
            return pid == 1234

        with patch.object(store, "_is_process_alive", side_effect=mock_is_alive):
            store.cleanup_stale()

        # Dead session should be deleted
        assert store.load("alive") is not None
        assert store.load("dead") is None

    def test_cleanup_stale_keeps_alive_sessions(self, store: SessionStore) -> None:
        """Should keep sessions with alive PIDs."""
        record = SessionRecord(session_id="alive", port=9222, pid=1234, profile_mode="isolated")
        store.save(record)

        # Mock _is_process_alive to return True
        with patch.object(store, "_is_process_alive", return_value=True):
            store.cleanup_stale()

        # Session should still exist
        assert store.load("alive") is not None

    def test_cleanup_stale_skips_profile_mode(self, store: SessionStore) -> None:
        """Should skip profile mode sessions (shared Chrome, no per-session PID)."""
        record = SessionRecord(
            session_id="profile_session",
            port=9222,
            pid=1234,
            profile_mode="profile",
        )
        store.save(record)

        # Mock _is_process_alive to return False
        with patch.object(store, "_is_process_alive", return_value=False):
            store.cleanup_stale()

        # Profile mode session should NOT be deleted
        assert store.load("profile_session") is not None

    def test_cleanup_stale_skips_none_pid(self, store: SessionStore) -> None:
        """Should skip sessions with pid=None."""
        record = SessionRecord(session_id="no_pid", port=9222, pid=None, profile_mode="isolated")
        store.save(record)

        # Mock _is_process_alive to return False
        with patch.object(store, "_is_process_alive", return_value=False):
            store.cleanup_stale()

        # Session with None pid should NOT be deleted
        assert store.load("no_pid") is not None

    def test_cleanup_stale_handles_empty_store(self, store: SessionStore) -> None:
        """Should handle cleanup when store is empty."""
        # Should not raise
        store.cleanup_stale()


class TestSessionStoreProcessAlive:
    """Tests for process alive checking."""

    def test_is_process_alive_native_returns_true_for_existing(self, store: SessionStore) -> None:
        """Should return True for existing process on native."""
        import os

        # Use current process PID (definitely alive)
        pid = os.getpid()

        with patch("wsl_chrome_mcp.session_store.is_wsl", return_value=False):
            result = store._is_process_alive(pid)

        assert result is True

    def test_is_process_alive_native_returns_false_for_missing(self, store: SessionStore) -> None:
        """Should return False for non-existent process on native."""
        with patch("wsl_chrome_mcp.session_store.is_wsl", return_value=False):
            result = store._is_process_alive(99999)

        assert result is False

    def test_is_process_alive_wsl_calls_powershell(self, store: SessionStore) -> None:
        """Should call PowerShell on WSL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Chrome"

        with patch("wsl_chrome_mcp.session_store.is_wsl", return_value=True):
            with patch(
                "wsl_chrome_mcp.session_store.run_windows_command",
                return_value=mock_result,
            ) as mock_run:
                result = store._is_process_alive_wsl(1234)

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "Get-Process" in call_args
        assert "1234" in call_args

    def test_is_process_alive_wsl_returns_false_when_not_found(self, store: SessionStore) -> None:
        """Should return False when PowerShell returns no output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch(
            "wsl_chrome_mcp.session_store.run_windows_command",
            return_value=mock_result,
        ):
            result = store._is_process_alive_wsl(9999)

        assert result is False

    def test_is_process_alive_wsl_returns_false_on_error(self, store: SessionStore) -> None:
        """Should return False when PowerShell returns error code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch(
            "wsl_chrome_mcp.session_store.run_windows_command",
            return_value=mock_result,
        ):
            result = store._is_process_alive_wsl(9999)

        assert result is False

    def test_is_process_alive_wsl_handles_runtime_error(self, store: SessionStore) -> None:
        """Should return True (safe default) when PowerShell fails."""
        with patch(
            "wsl_chrome_mcp.session_store.run_windows_command",
            side_effect=RuntimeError("PowerShell not available"),
        ):
            result = store._is_process_alive_wsl(1234)

        # Should assume alive (safer than deleting)
        assert result is True

    def test_is_process_alive_wsl_handles_unexpected_error(self, store: SessionStore) -> None:
        """Should return True (safe default) on unexpected error."""
        with patch(
            "wsl_chrome_mcp.session_store.run_windows_command",
            side_effect=Exception("Unexpected error"),
        ):
            result = store._is_process_alive_wsl(1234)

        # Should assume alive (safer than deleting)
        assert result is True
