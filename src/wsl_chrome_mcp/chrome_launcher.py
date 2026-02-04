"""Chrome launcher for Windows from WSL."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

import httpx

from .wsl import (
    find_windows_chrome,
    get_windows_host_ip,
    is_wsl,
    run_windows_command,
)

logger = logging.getLogger(__name__)

DEFAULT_DEBUGGING_PORT = 9222
CHROME_STARTUP_TIMEOUT = 30.0
CHROME_POLL_INTERVAL = 0.5


@dataclass
class ChromeInstance:
    """Represents a running Chrome instance with remote debugging."""

    host: str
    port: int
    websocket_url: str | None = None
    _process_id: int | None = field(default=None, repr=False)
    _managed: bool = field(default=False, repr=False)

    @property
    def debugger_url(self) -> str:
        """Get the Chrome DevTools HTTP endpoint."""
        return f"http://{self.host}:{self.port}"

    async def get_websocket_url(self) -> str:
        """Get the WebSocket debugger URL for this Chrome instance."""
        if self.websocket_url:
            return self.websocket_url

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.debugger_url}/json/version")
            response.raise_for_status()
            data = response.json()
            self.websocket_url = data.get("webSocketDebuggerUrl")
            if not self.websocket_url:
                raise RuntimeError("Chrome did not return webSocketDebuggerUrl")
            return self.websocket_url

    async def close(self) -> None:
        """Close this Chrome instance if it was started by us."""
        if not self._managed or not self._process_id:
            return

        logger.info(f"Closing managed Chrome instance (PID: {self._process_id})")

        if is_wsl():
            try:
                run_windows_command(
                    f"Stop-Process -Id {self._process_id} -Force -ErrorAction SilentlyContinue",
                    timeout=5.0,
                )
            except Exception as e:
                logger.warning(f"Failed to close Chrome: {e}")
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self._process_id, 15)  # SIGTERM


class ChromeLauncher:
    """Manages Chrome browser lifecycle for MCP connections."""

    def __init__(
        self,
        port: int = DEFAULT_DEBUGGING_PORT,
        headless: bool = False,
        user_data_dir: str | None = None,
    ) -> None:
        """Initialize the Chrome launcher.

        Args:
            port: Remote debugging port to use.
            headless: Whether to run Chrome in headless mode.
            user_data_dir: Custom user data directory. If None, uses a temp directory.
        """
        self.port = port
        self.headless = headless
        self.user_data_dir = user_data_dir
        self._instance: ChromeInstance | None = None
        self._windows_temp_dir: str | None = None  # Windows-side temp dir to clean up
        self._native_temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def _get_candidate_hosts(self) -> list[str]:
        """Get list of candidate hosts to try for Chrome connection.

        Returns multiple hosts to handle different WSL networking modes:
        - localhost/127.0.0.1: Works with WSL2 mirrored networking
        - Windows host IP: Works with WSL2 NAT networking
        """
        hosts = ["localhost", "127.0.0.1"]
        if is_wsl():
            windows_ip = get_windows_host_ip()
            if windows_ip not in hosts:
                hosts.append(windows_ip)
        return hosts

    async def connect_or_launch(self) -> ChromeInstance:
        """Connect to existing Chrome or launch a new one.

        This method first tries to connect to an existing Chrome instance
        running with remote debugging on the configured port. If none is
        found, it launches a new Chrome instance.

        Returns:
            ChromeInstance ready for CDP communication.
        """
        candidate_hosts = self._get_candidate_hosts()

        # Try to connect to existing instance on any of the candidate hosts
        for host in candidate_hosts:
            instance = await self._try_connect_existing(host)
            if instance:
                logger.info(f"Connected to existing Chrome at {instance.debugger_url}")
                self._instance = instance
                return instance

        # Launch new Chrome instance, then try to connect
        logger.info("No existing Chrome found, launching new instance...")
        instance = await self._launch_chrome(candidate_hosts)
        self._instance = instance
        return instance

    async def _try_connect_existing(self, host: str) -> ChromeInstance | None:
        """Try to connect to an existing Chrome instance.

        Args:
            host: Host address to connect to.

        Returns:
            ChromeInstance if connection successful, None otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"http://{host}:{self.port}/json/version")
                if response.status_code == 200:
                    data = response.json()
                    return ChromeInstance(
                        host=host,
                        port=self.port,
                        websocket_url=data.get("webSocketDebuggerUrl"),
                        _managed=False,
                    )
        except (httpx.RequestError, httpx.HTTPStatusError):
            pass
        return None

    async def _launch_chrome(self, candidate_hosts: list[str]) -> ChromeInstance:
        """Launch a new Chrome instance with remote debugging.

        Args:
            candidate_hosts: List of host addresses to try connecting to.

        Returns:
            ChromeInstance for the newly launched Chrome.

        Raises:
            RuntimeError: If Chrome cannot be launched or doesn't start in time.
        """
        if is_wsl():
            return await self._launch_chrome_wsl(candidate_hosts)
        else:
            return await self._launch_chrome_native(candidate_hosts)

    async def _launch_chrome_wsl(self, candidate_hosts: list[str]) -> ChromeInstance:
        """Launch Chrome on Windows from WSL.

        Args:
            candidate_hosts: List of host addresses to try connecting to.

        Returns:
            ChromeInstance for the launched Chrome.
        """
        chrome_path = find_windows_chrome()
        if not chrome_path:
            raise RuntimeError(
                "Chrome not found on Windows. Please install Chrome or start it manually "
                f"with: chrome.exe --remote-debugging-port={self.port}"
            )

        # Create user data directory on Windows (not WSL) for Chrome compatibility
        if self.user_data_dir:
            user_data = self.user_data_dir
        else:
            # Create temp directory on Windows side using PowerShell
            create_temp_cmd = (
                "$temp = Join-Path $env:TEMP ('wsl-chrome-mcp-' + "
                "[System.IO.Path]::GetRandomFileName()); "
                "New-Item -ItemType Directory -Path $temp -Force | Out-Null; "
                "Write-Output $temp"
            )
            result = run_windows_command(create_temp_cmd, timeout=10.0)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to create temp directory: {result.stderr}")
            user_data = result.stdout.strip()
            self._windows_temp_dir = user_data  # Track for cleanup

        # Build Chrome arguments
        args = self._build_chrome_args(user_data)

        # Launch Chrome via PowerShell
        # Build ArgumentList as PowerShell array for proper parsing
        args_escaped = [f'"{arg}"' for arg in args]
        args_ps_array = ",".join(args_escaped)

        ps_command = f'''
        $chrome = Start-Process -FilePath "{chrome_path}" -ArgumentList {args_ps_array} -PassThru
        Write-Output $chrome.Id
        '''

        logger.debug(f"Launching Chrome with: {ps_command}")

        try:
            result = run_windows_command(ps_command, timeout=10.0)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to launch Chrome: {result.stderr}")

            pid = int(result.stdout.strip())
            logger.info(f"Chrome launched with PID: {pid}")

        except (ValueError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"Failed to launch Chrome: {e}") from e

        # Wait for Chrome to be ready
        instance = await self._wait_for_chrome(candidate_hosts, pid)
        return instance

    async def _launch_chrome_native(self, candidate_hosts: list[str]) -> ChromeInstance:
        """Launch Chrome natively (non-WSL Linux or macOS).

        Args:
            candidate_hosts: List of host addresses to try connecting to.

        Returns:
            ChromeInstance for the launched Chrome.
        """
        # Find Chrome on native system
        chrome_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]

        chrome_path = None
        for path in chrome_paths:
            if os.path.exists(path):
                chrome_path = path
                break

        if not chrome_path:
            raise RuntimeError(
                "Chrome not found. Please install Chrome or start it manually with: "
                f"google-chrome --remote-debugging-port={self.port}"
            )

        # Create user data directory
        if self.user_data_dir:
            user_data = self.user_data_dir
        else:
            self._native_temp_dir = tempfile.TemporaryDirectory(prefix="wsl-chrome-mcp-")
            user_data = self._native_temp_dir.name

        # Build and launch
        args = [chrome_path] + self._build_chrome_args(user_data)
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        logger.info(f"Chrome launched with PID: {process.pid}")

        instance = await self._wait_for_chrome(candidate_hosts, process.pid)
        return instance

    def _build_chrome_args(self, user_data_dir: str) -> list[str]:
        """Build Chrome command-line arguments.

        Args:
            user_data_dir: Path to user data directory.

        Returns:
            List of command-line arguments.
        """
        args = [
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=0.0.0.0",  # Listen on all interfaces for WSL
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]

        if self.headless:
            args.append("--headless=new")

        return args

    async def _wait_for_chrome(
        self, candidate_hosts: list[str], pid: int
    ) -> ChromeInstance:
        """Wait for Chrome to become ready for debugging.

        Args:
            candidate_hosts: List of host addresses to try.
            pid: Process ID of Chrome.

        Returns:
            ChromeInstance when ready.

        Raises:
            RuntimeError: If Chrome doesn't start within timeout.
        """
        deadline = asyncio.get_event_loop().time() + CHROME_STARTUP_TIMEOUT

        while asyncio.get_event_loop().time() < deadline:
            # Try all candidate hosts on each iteration
            for host in candidate_hosts:
                instance = await self._try_connect_existing(host)
                if instance:
                    instance._process_id = pid
                    instance._managed = True
                    return instance

            await asyncio.sleep(CHROME_POLL_INTERVAL)

        raise RuntimeError(
            f"Chrome did not start within {CHROME_STARTUP_TIMEOUT}s. "
            f"Please try starting Chrome manually with: "
            f"chrome.exe --remote-debugging-port={self.port}"
        )

    async def close(self) -> None:
        """Close the Chrome instance and clean up resources."""
        if self._instance:
            await self._instance.close()
            self._instance = None

        if self._windows_temp_dir:
            try:
                # Clean up Windows temp directory via PowerShell
                cleanup_cmd = (
                    f'Remove-Item -Path "{self._windows_temp_dir}" '
                    f'-Recurse -Force -ErrorAction SilentlyContinue'
                )
                run_windows_command(cleanup_cmd, timeout=10.0)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory: {e}")
            self._windows_temp_dir = None

        if self._native_temp_dir:
            try:
                self._native_temp_dir.cleanup()
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory: {e}")
            self._native_temp_dir = None

    @property
    def instance(self) -> ChromeInstance | None:
        """Get the current Chrome instance, if any."""
        return self._instance
