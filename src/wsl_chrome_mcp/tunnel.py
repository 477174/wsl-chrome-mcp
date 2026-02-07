"""Windows-side TCP port forwarder for WSL to Chrome connectivity.

Chrome's recent versions ignore --remote-debugging-address=0.0.0.0 and always
bind to 127.0.0.1. This means WSL cannot reach Chrome directly. The CDPProxyClient
works because PowerShell runs on Windows and sees localhost. But for persistent
WebSocket connections (PersistentCDPClient), we need a TCP bridge.

This module runs a lightweight C# TCP forwarder as a background PowerShell process
ON WINDOWS. It listens on 0.0.0.0:<port> and forwards to 127.0.0.1:<chrome_port>,
making Chrome's debugging port accessible from WSL.

Data flow:
    WSL Python (websockets) -> Windows host IP:forward_port
        -> C# TcpForwarder (0.0.0.0:forward_port)
        -> Chrome (127.0.0.1:chrome_port)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import subprocess
from dataclasses import dataclass

import httpx

from .wsl import _find_windows_executable, get_windows_host_ip, is_wsl

logger = logging.getLogger(__name__)


# C# TCP forwarder source. Compiled at runtime inside PowerShell via Add-Type.
# Listens on 0.0.0.0:0 (OS picks free port), prints port to stdout, then
# proxies connections bidirectionally to 127.0.0.1:targetPort.
_TCP_FORWARDER_CSHARP = """\
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

public class TcpForwarder
{
    private static int _targetPort;

    public static void Start(int targetPort)
    {
        _targetPort = targetPort;
        var listener = new TcpListener(IPAddress.Any, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        Console.WriteLine(port);
        Console.Out.Flush();

        while (true)
        {
            var client = listener.AcceptTcpClient();
            ThreadPool.QueueUserWorkItem(HandleClient, client);
        }
    }

    private static void HandleClient(object state)
    {
        var client = (TcpClient)state;
        TcpClient target = null;
        try
        {
            target = new TcpClient("127.0.0.1", _targetPort);
            var cs = client.GetStream();
            var ts = target.GetStream();
            var t1 = cs.CopyToAsync(ts);
            var t2 = ts.CopyToAsync(cs);
            Task.WaitAny(t1, t2);
        }
        catch { }
        finally
        {
            try { if (client != null) client.Dispose(); } catch { }
            try { if (target != null) target.Dispose(); } catch { }
        }
    }
}
"""


@dataclass
class ForwarderInfo:
    """Information about an active port forwarder."""

    listen_port: int
    chrome_port: int
    windows_host: str
    process: subprocess.Popen | None


class WindowsPortForwarder:
    """Runs a TCP port forwarder on Windows to expose Chrome's debugging port.

    Chrome binds to 127.0.0.1 only (ignoring --remote-debugging-address=0.0.0.0
    in recent versions). This class starts a C# TCP forwarder as a background
    PowerShell process on Windows that listens on 0.0.0.0 and forwards to
    Chrome's 127.0.0.1 port — making it reachable from WSL.

    Example:
        forwarder = WindowsPortForwarder(chrome_port=9222)
        await forwarder.start()
        # Now connect to windows_host_ip:forwarder.listen_port from WSL
        await forwarder.stop()
    """

    def __init__(
        self,
        chrome_port: int,
        windows_host: str | None = None,
    ) -> None:
        """Initialize the forwarder.

        Args:
            chrome_port: Chrome debugging port on Windows (127.0.0.1).
            windows_host: Windows host IP as seen from WSL (auto-detected if None).
        """
        self.chrome_port = chrome_port
        self._windows_host = windows_host
        self._process: subprocess.Popen | None = None
        self._listen_port: int | None = None
        self._started = False

    @property
    def listen_port(self) -> int | None:
        """Get the port the forwarder is listening on (None until started)."""
        return self._listen_port

    @property
    def windows_host(self) -> str:
        """Get the Windows host IP."""
        if self._windows_host is None:
            self._windows_host = get_windows_host_ip()
        return self._windows_host

    @property
    def is_running(self) -> bool:
        """Check if the forwarder process is still alive."""
        return self._started and self._process is not None and self._process.poll() is None

    async def start(self) -> None:
        """Start the port forwarder on Windows.

        Launches a PowerShell process that compiles and runs a C# TCP forwarder.
        The forwarder listens on 0.0.0.0:<random_port> and proxies to
        127.0.0.1:<chrome_port>.

        Raises:
            RuntimeError: If PowerShell is not found or forwarder fails to start.
        """
        if self._started:
            logger.warning("Port forwarder already started for Chrome port %d", self.chrome_port)
            return

        if not is_wsl():
            # Not in WSL — no forwarder needed, connect directly
            logger.info("Not in WSL, skipping port forwarder (direct connection)")
            self._listen_port = self.chrome_port
            self._started = True
            return

        powershell = _find_windows_executable("powershell.exe")
        if not powershell:
            raise RuntimeError("powershell.exe not found. Ensure WSL interop is enabled.")

        # Encode C# source as base64 to avoid all quoting/escaping issues
        encoded_csharp = base64.b64encode(_TCP_FORWARDER_CSHARP.encode("utf-8")).decode("ascii")

        ps_script = (
            f"$bytes = [System.Convert]::FromBase64String('{encoded_csharp}');"
            "$code = [System.Text.Encoding]::UTF8.GetString($bytes);"
            "Add-Type -TypeDefinition $code;"
            f"[TcpForwarder]::Start({self.chrome_port})"
        )

        logger.info(
            "Starting port forwarder for Chrome port %d",
            self.chrome_port,
        )

        try:
            self._process = subprocess.Popen(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Read the listen port from stdout (first line output by C# code)
            loop = asyncio.get_event_loop()
            try:
                line = await asyncio.wait_for(
                    loop.run_in_executor(None, self._process.stdout.readline),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                self._kill_process()
                raise RuntimeError(
                    f"Port forwarder for Chrome port {self.chrome_port} "
                    "did not start within 30 seconds"
                ) from None

            port_str = line.decode("utf-8", errors="replace").strip()
            if not port_str:
                stderr = ""
                if self._process.stderr:
                    with contextlib.suppress(Exception):
                        stderr = self._process.stderr.read1(4096).decode("utf-8", errors="replace")
                self._kill_process()
                raise RuntimeError(
                    f"Port forwarder for Chrome port {self.chrome_port} "
                    f"failed to report listen port. stderr: {stderr}"
                )

            try:
                self._listen_port = int(port_str)
            except ValueError:
                self._kill_process()
                raise RuntimeError(f"Port forwarder returned invalid port: {port_str!r}") from None

            # Verify the process is still running
            if self._process.poll() is not None:
                self._kill_process()
                raise RuntimeError(
                    "Port forwarder exited immediately after starting "
                    f"(exit code: {self._process.returncode})"
                )

            self._started = True
            logger.info(
                "Port forwarder started: 0.0.0.0:%d -> 127.0.0.1:%d (accessible at %s:%d from WSL)",
                self._listen_port,
                self.chrome_port,
                self.windows_host,
                self._listen_port,
            )

        except RuntimeError:
            raise
        except Exception as e:
            logger.error("Failed to start port forwarder: %s", e)
            self._kill_process()
            raise RuntimeError(f"Failed to start port forwarder: {e}") from e

    def _kill_process(self) -> None:
        """Kill the forwarder process if running."""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2)
                except Exception:
                    pass
            self._process = None

    async def stop(self) -> None:
        """Stop the port forwarder."""
        if not self._started:
            return

        if self._process is not None:
            logger.info(
                "Stopping port forwarder on port %d (Chrome port %d)",
                self._listen_port or 0,
                self.chrome_port,
            )
            self._kill_process()

        self._started = False
        self._listen_port = None

    async def verify_connectivity(self, timeout: float = 5.0) -> bool:
        """Verify end-to-end connectivity: WSL -> forwarder -> Chrome.

        Makes an HTTP request from WSL through the forwarder to Chrome's
        /json/version endpoint.

        Args:
            timeout: Maximum time to wait for the health check.

        Returns:
            True if Chrome is reachable through the forwarder.
        """
        if not self._started or self._listen_port is None:
            return False

        url = f"http://{self.windows_host}:{self._listen_port}/json/version"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    logger.debug(
                        "Port forwarder health check passed: %s:%d",
                        self.windows_host,
                        self._listen_port,
                    )
                    return True
                logger.warning(
                    "Port forwarder health check failed: HTTP %d",
                    response.status_code,
                )
                return False
        except Exception as e:
            logger.warning("Port forwarder health check failed: %s", e)
            return False

    def get_ws_url(self, path: str = "") -> str:
        """Get WebSocket URL to connect through the forwarder from WSL.

        Args:
            path: URL path to append (e.g., "/devtools/page/XXX").

        Returns:
            Full WebSocket URL like ws://windows_host:port/path
        """
        return f"ws://{self.windows_host}:{self._listen_port}{path}"

    async def __aenter__(self) -> WindowsPortForwarder:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.stop()


class PortForwarderManager:
    """Manages port forwarders for multiple Chrome instances.

    Each Chrome instance runs on a different port, so we need a separate
    forwarder for each.
    """

    def __init__(self) -> None:
        """Initialize the manager."""
        self._forwarders: dict[int, WindowsPortForwarder] = {}  # chrome_port -> forwarder

    async def get_or_create(
        self,
        chrome_port: int,
        verify: bool = True,
        max_retries: int = 2,
    ) -> WindowsPortForwarder:
        """Get existing forwarder or create new one for a Chrome port.

        Args:
            chrome_port: The Chrome debugging port to forward.
            verify: If True, verify end-to-end connectivity.
            max_retries: Number of retry attempts on failure.

        Returns:
            Active WindowsPortForwarder instance.

        Raises:
            RuntimeError: If forwarder creation fails after retries.
        """
        if chrome_port in self._forwarders:
            forwarder = self._forwarders[chrome_port]
            if forwarder.is_running:
                if verify and not await forwarder.verify_connectivity(timeout=3.0):
                    logger.warning(
                        "Existing forwarder for Chrome port %d failed health check, recreating",
                        chrome_port,
                    )
                    await self.remove(chrome_port)
                else:
                    return forwarder
            else:
                logger.warning(
                    "Forwarder for Chrome port %d died, recreating",
                    chrome_port,
                )
                await self.remove(chrome_port)

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                forwarder = WindowsPortForwarder(chrome_port=chrome_port)
                await forwarder.start()

                if verify:
                    healthy = await forwarder.verify_connectivity(timeout=5.0)
                    if not healthy:
                        logger.warning(
                            "Forwarder for Chrome port %d failed health check (attempt %d/%d)",
                            chrome_port,
                            attempt + 1,
                            max_retries + 1,
                        )
                        await forwarder.stop()
                        last_error = RuntimeError(
                            f"Port forwarder health check failed for Chrome port {chrome_port}"
                        )
                        continue

                self._forwarders[chrome_port] = forwarder
                return forwarder

            except Exception as e:
                last_error = e
                logger.warning(
                    "Forwarder creation failed for Chrome port %d (attempt %d/%d): %s",
                    chrome_port,
                    attempt + 1,
                    max_retries + 1,
                    e,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0)

        raise RuntimeError(
            f"Failed to create port forwarder for Chrome port {chrome_port} "
            f"after {max_retries + 1} attempts: {last_error}"
        )

    async def remove(self, chrome_port: int) -> None:
        """Remove and stop a forwarder.

        Args:
            chrome_port: The Chrome port of the forwarder to remove.
        """
        if chrome_port in self._forwarders:
            forwarder = self._forwarders.pop(chrome_port)
            await forwarder.stop()

    async def cleanup_all(self) -> None:
        """Stop all forwarders."""
        logger.info("Cleaning up %d port forwarder(s)", len(self._forwarders))
        for port in list(self._forwarders.keys()):
            await self.remove(port)

    def list_forwarders(self) -> dict[int, ForwarderInfo]:
        """List all active forwarders.

        Returns:
            Dict mapping Chrome port to ForwarderInfo.
        """
        result = {}
        for port, fwd in self._forwarders.items():
            result[port] = ForwarderInfo(
                listen_port=fwd.listen_port or 0,
                chrome_port=fwd.chrome_port,
                windows_host=fwd.windows_host,
                process=fwd._process,
            )
        return result
