"""Per-session JSON persistence for Chrome session state."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from wsl_chrome_mcp.wsl import is_wsl, run_windows_command

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    """Persistent record of a Chrome session state.

    Attributes:
        session_id: Unique session identifier.
        port: CDP debugging port.
        pid: Process ID of Chrome instance (None if shared profile mode).
        target_ids: List of CDP target IDs (tabs/windows).
        current_target_id: Currently active target ID.
        profile_mode: "isolated" or "profile".
        created_at: ISO 8601 timestamp of session creation.
        browser_context_id: Browser context ID for isolated mode.
    """

    session_id: str
    port: int
    pid: int | None
    target_ids: list[str] = field(default_factory=list)
    current_target_id: str | None = None
    profile_mode: str = "isolated"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    browser_context_id: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SessionRecord:
        """Create from dictionary (JSON deserialization)."""
        return cls(**data)


class SessionStore:
    """Per-session JSON file persistence for Chrome session state.

    Stores session records in /tmp/wsl-chrome-mcp/sessions/ with atomic writes.
    Provides cleanup for stale sessions (dead PIDs).
    """

    STORE_DIR = Path("/tmp/wsl-chrome-mcp/sessions")

    def __init__(self) -> None:
        """Initialize store directory."""
        self.STORE_DIR.mkdir(parents=True, exist_ok=True)
        logger.debug(f"SessionStore initialized at {self.STORE_DIR}")

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session record."""
        return self.STORE_DIR / f"{session_id}.json"

    def save(self, record: SessionRecord) -> None:
        """Save session record with atomic write (write to .tmp, then rename).

        Args:
            record: SessionRecord to persist.
        """
        session_path = self._get_session_path(record.session_id)
        tmp_path = session_path.with_suffix(".json.tmp")

        try:
            # Write to temporary file
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2)

            # Atomic rename
            os.replace(tmp_path, session_path)
            logger.debug(f"Saved session {record.session_id} to {session_path}")
        except OSError as e:
            logger.error(f"Failed to save session {record.session_id}: {e}")
            # Clean up temp file if it exists
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    def load(self, session_id: str) -> SessionRecord | None:
        """Load session record from file.

        Handles missing files and corrupt JSON gracefully.

        Args:
            session_id: Session ID to load.

        Returns:
            SessionRecord if found and valid, None otherwise.
        """
        session_path = self._get_session_path(session_id)

        if not session_path.exists():
            logger.debug(f"Session file not found: {session_path}")
            return None

        try:
            with open(session_path, encoding="utf-8") as f:
                data = json.load(f)
            record = SessionRecord.from_dict(data)
            logger.debug(f"Loaded session {session_id}")
            return record
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Corrupt session file {session_path}: {e}")
            return None
        except OSError as e:
            logger.error(f"Failed to read session {session_id}: {e}")
            return None

    def delete(self, session_id: str) -> None:
        """Delete session record file.

        Handles missing files gracefully.

        Args:
            session_id: Session ID to delete.
        """
        session_path = self._get_session_path(session_id)

        try:
            session_path.unlink(missing_ok=True)
            logger.debug(f"Deleted session {session_id}")
        except OSError as e:
            logger.error(f"Failed to delete session {session_id}: {e}")

    def list_all(self) -> list[SessionRecord]:
        """List all stored session records.

        Returns:
            List of SessionRecord objects, skipping corrupt files.
        """
        records: list[SessionRecord] = []

        if not self.STORE_DIR.exists():
            return records

        try:
            for json_file in self.STORE_DIR.glob("*.json"):
                session_id = json_file.stem
                record = self.load(session_id)
                if record is not None:
                    records.append(record)
        except OSError as e:
            logger.error(f"Failed to list sessions: {e}")

        return records

    def cleanup_stale(self) -> None:
        """Remove session records for dead processes.

        Synchronous operation. Checks each stored PID:
        - On WSL: uses PowerShell Get-Process
        - On native: uses os.kill(pid, 0)

        Deletes files for dead PIDs.
        """
        records = self.list_all()

        for record in records:
            # Skip profile mode (shared Chrome, no per-session PID)
            if record.pid is None or record.profile_mode == "profile":
                continue

            if self._is_process_alive(record.pid):
                logger.debug(f"Process {record.pid} (session {record.session_id}) is alive")
            else:
                logger.info(
                    f"Process {record.pid} (session {record.session_id}) is dead, cleaning up"
                )
                self.delete(record.session_id)

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is alive.

        Args:
            pid: Process ID to check.

        Returns:
            True if process exists, False otherwise.
        """
        if is_wsl():
            return self._is_process_alive_wsl(pid)
        else:
            return self._is_process_alive_native(pid)

    def _is_process_alive_wsl(self, pid: int) -> bool:
        """Check if Windows process is alive using PowerShell.

        Args:
            pid: Process ID to check.

        Returns:
            True if process exists, False otherwise.
        """
        try:
            result = run_windows_command(f"Get-Process -Id {pid} -ErrorAction SilentlyContinue")
            # If Get-Process succeeds (returncode 0) and has output, process is alive
            return result.returncode == 0 and bool(result.stdout.strip())
        except RuntimeError as e:
            logger.warning(f"Failed to check process {pid} on Windows: {e}")
            # Assume alive if we can't check (safer than deleting)
            return True
        except Exception as e:
            logger.error(f"Unexpected error checking process {pid}: {e}")
            return True

    def _is_process_alive_native(self, pid: int) -> bool:
        """Check if native process is alive using os.kill(pid, 0).

        Args:
            pid: Process ID to check.

        Returns:
            True if process exists, False otherwise.
        """
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, PermissionError) as e:
            logger.warning(f"Failed to check process {pid}: {e}")
            # Assume alive if we can't check (safer than deleting)
            return True
