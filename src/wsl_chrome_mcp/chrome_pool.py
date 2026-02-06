"""Chrome pool manager for per-session Chrome instances.

Each opencode chat session gets its own Chrome process on a unique port,
providing complete isolation between sessions.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .cdp_proxy import CDPProxyClient
from .wsl import run_windows_command

logger = logging.getLogger(__name__)


@dataclass
class ChromeInstance:
    """A Chrome instance dedicated to a single session."""

    session_id: str
    port: int
    pid: int | None  # Windows PID for cleanup
    proxy: CDPProxyClient
    user_data_dir: str  # Temp dir on Windows
    created_at: datetime

    # Tab tracking within this Chrome instance
    current_target_id: str | None = None
    targets: list[str] = field(default_factory=list)
    ws_urls: dict[str, str] = field(default_factory=dict)  # target_id -> ws_url

    @property
    def current_ws_url(self) -> str | None:
        """Get WebSocket URL for the current (active) tab."""
        if self.current_target_id and self.current_target_id in self.ws_urls:
            return self.ws_urls[self.current_target_id]
        return None


class ChromePoolManager:
    """Manages one Chrome instance per session.

    Each session gets its own Chrome process on a unique port.
    This provides complete isolation - no window confusion, no race conditions.
    """

    def __init__(
        self,
        port_min: int = 9222,
        port_max: int = 9322,
        headless: bool = False,
    ) -> None:
        """Initialize the Chrome pool manager.

        Args:
            port_min: Start of port range for Chrome instances.
            port_max: End of port range (exclusive).
            headless: Whether to launch Chrome in headless mode.
        """
        self._instances: dict[str, ChromeInstance] = {}
        self._port_min = port_min
        self._port_max = port_max
        self._used_ports: set[int] = set()
        self._headless = headless
        self._chrome_path: str | None = None

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is actually in use by attempting to connect.

        This catches cases where a Chrome from a previous server run is still
        running on a port that we think is available (because _used_ports is
        in-memory only and gets cleared on restart).

        Args:
            port: The port to check.

        Returns:
            True if something is listening on the port, False otherwise.
        """
        proxy = CDPProxyClient(port)
        try:
            # If we get a response, something is already using this port
            version = proxy._make_http_request("/json/version")
            return version is not None
        except Exception:
            return False

    def _allocate_port(self) -> int:
        """Find next available port in range.

        Checks both in-memory tracking and actual port availability to handle
        cases where Chrome instances survive server restarts.

        Returns:
            An available port number.

        Raises:
            RuntimeError: If no ports are available.
        """
        for port in range(self._port_min, self._port_max):
            if port in self._used_ports:
                continue
            if self._is_port_in_use(port):
                logger.warning(
                    "Port %d is in use by external process (orphaned Chrome?), skipping",
                    port,
                )
                # Add to used_ports so we don't check it again this session
                self._used_ports.add(port)
                continue
            self._used_ports.add(port)
            logger.debug("Allocated port %d", port)
            return port
        raise RuntimeError(
            f"No available ports in range {self._port_min}-{self._port_max}. "
            f"Too many concurrent sessions ({len(self._used_ports)})."
        )

    def _release_port(self, port: int) -> None:
        """Return port to available pool."""
        self._used_ports.discard(port)
        logger.debug("Released port %d", port)

    async def _find_chrome_path(self) -> str:
        """Find Chrome executable on Windows."""
        if self._chrome_path:
            return self._chrome_path

        find_chrome_ps = """
        $paths = @(
            "$env:PROGRAMFILES\\Google\\Chrome\\Application\\chrome.exe",
            "${env:PROGRAMFILES(x86)}\\Google\\Chrome\\Application\\chrome.exe",
            "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
        )
        foreach ($p in $paths) { if (Test-Path $p) { Write-Output $p; break } }
        """
        result = run_windows_command(find_chrome_ps, timeout=10.0)
        chrome_path = result.stdout.strip() if result.returncode == 0 else None

        if not chrome_path:
            raise RuntimeError("Chrome not found on Windows")

        self._chrome_path = chrome_path
        logger.info("Found Chrome at: %s", chrome_path)
        return chrome_path

    async def _launch_chrome(self, session_id: str, port: int) -> ChromeInstance:
        """Launch a new Chrome instance on Windows.

        Args:
            session_id: The session this Chrome belongs to.
            port: The debugging port to use.

        Returns:
            ChromeInstance with connection established.
        """
        chrome_path = await self._find_chrome_path()

        # Create temp directory on Windows
        create_temp_ps = (
            '$temp = Join-Path $env:TEMP ("chrome-mcp-" + '
            "[System.IO.Path]::GetRandomFileName()); "
            "New-Item -ItemType Directory -Path $temp -Force | Out-Null; "
            "Write-Output $temp"
        )
        result = run_windows_command(create_temp_ps, timeout=10.0)
        user_data_dir = result.stdout.strip() if result.returncode == 0 else None

        if not user_data_dir:
            raise RuntimeError("Failed to create temp directory on Windows")

        logger.info(
            "Launching Chrome for session %s on port %d (user_data_dir=%s)",
            session_id,
            port,
            user_data_dir,
        )

        # Build Chrome arguments
        args = [
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=0.0.0.0",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            args.append("--headless=new")

        args_str = '","'.join(args)

        # Launch Chrome and get PID
        launch_ps = (
            f'$proc = Start-Process -FilePath "{chrome_path}" '
            f'-ArgumentList "{args_str}" -PassThru; '
            "Write-Output $proc.Id"
        )
        result = run_windows_command(launch_ps, timeout=10.0)

        pid: int | None = None
        if result.returncode == 0 and result.stdout.strip():
            try:
                pid = int(result.stdout.strip())
                logger.info("Chrome launched with PID %d", pid)
            except ValueError:
                logger.warning("Could not parse Chrome PID: %s", result.stdout)

        # Wait for Chrome to be ready
        proxy = CDPProxyClient(port)
        for _attempt in range(30):
            await asyncio.sleep(1)
            version = await proxy.get_version()
            if version:
                logger.info(
                    "Chrome ready on port %d: %s",
                    port,
                    version.get("Browser", "unknown"),
                )
                break
        else:
            raise RuntimeError(f"Chrome did not start within 30 seconds on port {port}")

        # Get initial tab
        targets = await proxy.list_targets()
        page_targets = [t for t in targets if t.get("type") == "page"]

        if not page_targets:
            # Create a new page if none exists
            new_page = await proxy.new_page()
            if new_page:
                initial_target_id = new_page.get("id")
                initial_ws_url = new_page.get("webSocketDebuggerUrl")
            else:
                raise RuntimeError("Failed to create initial page")
        else:
            initial_target_id = page_targets[0].get("id")
            initial_ws_url = page_targets[0].get("webSocketDebuggerUrl")

        return ChromeInstance(
            session_id=session_id,
            port=port,
            pid=pid,
            proxy=proxy,
            user_data_dir=user_data_dir,
            created_at=datetime.now(),
            current_target_id=initial_target_id,
            targets=[initial_target_id] if initial_target_id else [],
            ws_urls={initial_target_id: initial_ws_url}
            if initial_target_id and initial_ws_url
            else {},
        )

    async def _kill_chrome(self, instance: ChromeInstance) -> None:
        """Kill Chrome process and cleanup temp directory.

        Args:
            instance: The Chrome instance to kill.
        """
        if instance.pid:
            logger.info(
                "Killing Chrome PID %d for session %s",
                instance.pid,
                instance.session_id,
            )
            kill_ps = f"Stop-Process -Id {instance.pid} -Force -ErrorAction SilentlyContinue"
            try:
                run_windows_command(kill_ps, timeout=10.0)
            except Exception as e:
                logger.warning("Error killing Chrome PID %d: %s", instance.pid, e)

        # Cleanup temp directory
        if instance.user_data_dir:
            cleanup_ps = (
                f'Remove-Item -Path "{instance.user_data_dir}" '
                "-Recurse -Force -ErrorAction SilentlyContinue"
            )
            try:
                run_windows_command(cleanup_ps, timeout=10.0)
            except Exception as e:
                logger.warning(
                    "Error cleaning up %s: %s",
                    instance.user_data_dir,
                    e,
                )

    async def get_or_create(self, session_id: str) -> ChromeInstance:
        """Get existing Chrome instance or create new one for this session.

        Args:
            session_id: The opencode session identifier.

        Returns:
            ChromeInstance for the requested session.
        """
        if session_id in self._instances:
            logger.debug("Returning existing Chrome for session %s", session_id)
            return self._instances[session_id]

        logger.info("Creating new Chrome instance for session %s", session_id)
        port = self._allocate_port()

        try:
            instance = await self._launch_chrome(session_id, port)
            self._instances[session_id] = instance
            return instance
        except Exception:
            self._release_port(port)
            raise

    async def destroy(self, session_id: str) -> None:
        """Destroy a session's Chrome instance.

        Args:
            session_id: The session to destroy.

        Raises:
            KeyError: If session not found.
        """
        instance = self._instances.pop(session_id)
        await self._kill_chrome(instance)
        self._release_port(instance.port)
        logger.info("Destroyed Chrome for session %s", session_id)

    async def cleanup_all(self) -> None:
        """Kill all Chrome instances. Called on server shutdown."""
        logger.info("Cleaning up %d Chrome instance(s)", len(self._instances))
        for session_id in list(self._instances.keys()):
            try:
                await self.destroy(session_id)
            except Exception as e:
                logger.warning("Error destroying session %s: %s", session_id, e)

    def list_sessions(self) -> dict[str, dict[str, Any]]:
        """List all active sessions.

        Returns:
            Dict mapping session_id to session info.
        """
        result = {}
        for session_id, instance in self._instances.items():
            result[session_id] = {
                "session_id": session_id,
                "port": instance.port,
                "pid": instance.pid,
                "tab_count": len(instance.targets),
                "current_target_id": instance.current_target_id,
                "created_at": instance.created_at.isoformat(),
            }
        return result

    # --- Tab operations (within a session's Chrome) ---

    async def create_tab(self, session_id: str, url: str = "about:blank") -> str:
        """Create a new tab in a session's Chrome.

        Since each session has its own Chrome with one window,
        Target.createTarget simply works - no window confusion!

        Args:
            session_id: The session to create the tab in.
            url: URL to open in the new tab.

        Returns:
            The target_id of the new tab.

        Raises:
            KeyError: If session not found.
        """
        instance = self._instances[session_id]

        browser_ws = await instance.proxy.get_browser_ws_url()
        if not browser_ws:
            raise RuntimeError("Failed to get browser WebSocket URL")

        result = await instance.proxy.send_cdp_command(
            browser_ws,
            "Target.createTarget",
            {"url": url},
        )
        target_id = result["targetId"]

        # Get WebSocket URL for the new tab
        targets = await instance.proxy.list_targets()
        target = next((t for t in targets if t.get("id") == target_id), None)

        if not target:
            raise RuntimeError(f"New target {target_id} not found in targets list")

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(f"No WebSocket URL for target {target_id}")

        instance.targets.append(target_id)
        instance.ws_urls[target_id] = ws_url
        instance.current_target_id = target_id

        logger.info("Session %s: created tab %s -> %s", session_id, target_id, url)
        return target_id

    async def switch_tab(self, session_id: str, target_id: str) -> str:
        """Switch the active tab in a session's Chrome.

        Args:
            session_id: The session to switch tabs in.
            target_id: The target_id to switch to.

        Returns:
            WebSocket URL of the newly active tab.

        Raises:
            KeyError: If session not found.
            ValueError: If target_id not in this session.
        """
        instance = self._instances[session_id]

        if target_id not in instance.targets:
            raise ValueError(
                f"Target {target_id} does not belong to session {session_id}. "
                f"Available: {instance.targets}"
            )

        # Activate the target
        browser_ws = await instance.proxy.get_browser_ws_url()
        if browser_ws:
            await instance.proxy.send_cdp_command(
                browser_ws,
                "Target.activateTarget",
                {"targetId": target_id},
            )

        instance.current_target_id = target_id

        # Ensure we have the WebSocket URL
        if target_id not in instance.ws_urls:
            targets = await instance.proxy.list_targets()
            target = next((t for t in targets if t.get("id") == target_id), None)
            if target:
                instance.ws_urls[target_id] = target.get("webSocketDebuggerUrl", "")

        logger.info("Session %s: switched to tab %s", session_id, target_id)
        return instance.ws_urls.get(target_id, "")

    async def close_tab(self, session_id: str, target_id: str) -> None:
        """Close a tab in a session's Chrome.

        Args:
            session_id: The session that owns the tab.
            target_id: The target_id to close.

        Raises:
            KeyError: If session not found.
            ValueError: If target_id not in session or is the last tab.
        """
        instance = self._instances[session_id]

        if target_id not in instance.targets:
            raise ValueError(f"Target {target_id} does not belong to session {session_id}")

        if len(instance.targets) <= 1:
            raise ValueError(
                f"Cannot close the last tab in session {session_id}. "
                "Use destroy() to close the entire session."
            )

        # Close the target
        await instance.proxy.close_page(target_id)

        # Update state
        instance.targets.remove(target_id)
        if target_id in instance.ws_urls:
            del instance.ws_urls[target_id]

        # Switch to another tab if we closed the current one
        if instance.current_target_id == target_id:
            instance.current_target_id = instance.targets[0]
            logger.info(
                "Session %s: auto-switched to tab %s",
                session_id,
                instance.current_target_id,
            )

        logger.info("Session %s: closed tab %s", session_id, target_id)

    async def list_tabs(self, session_id: str) -> list[dict[str, Any]]:
        """List all tabs in a session's Chrome.

        Args:
            session_id: The session to list tabs for.

        Returns:
            List of tab info dicts.

        Raises:
            KeyError: If session not found.
        """
        instance = self._instances[session_id]

        all_targets = await instance.proxy.list_targets()
        session_targets = [t for t in all_targets if t.get("id") in instance.targets]

        tabs = []
        for target in session_targets:
            tabs.append(
                {
                    "id": target.get("id"),
                    "title": target.get("title", ""),
                    "url": target.get("url", ""),
                    "is_current": target.get("id") == instance.current_target_id,
                }
            )

        return tabs
