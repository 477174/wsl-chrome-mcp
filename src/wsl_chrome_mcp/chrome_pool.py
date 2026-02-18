"""Chrome pool manager using shared Chrome with per-session isolation.

All sessions share a single Chrome process and debugging port. Session
isolation is provided by one CDP BrowserContext per session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .cdp_proxy import CDPProxyClient
from .persistent_cdp import PersistentCDPClient, enable_domains
from .ps_relay import PowerShellCDPRelay
from .wsl import get_windows_host_ip, is_mirrored_networking, is_wsl, run_windows_command

logger = logging.getLogger(__name__)


@dataclass
class ConsoleMessage:
    """A console message captured from the browser."""

    type: str  # log, warn, error, info, debug
    text: str
    timestamp: float | None = None
    stack_trace: list[dict[str, Any]] | None = None
    args: list[Any] | None = None


@dataclass
class NetworkRequest:
    """A network request captured from the browser."""

    request_id: str
    url: str
    method: str
    timestamp: float | None = None
    type: str | None = None  # Document, XHR, Fetch, etc.
    headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None
    response: dict[str, Any] | None = None
    response_body: bytes | None = None


@dataclass
class DialogInfo:
    """Information about a pending browser dialog."""

    type: str  # alert, confirm, prompt, beforeunload
    message: str
    default_prompt: str | None = None
    url: str | None = None


@dataclass
class ChromeInstance:
    """Session state backed by a shared Chrome process.

    Maintains persistent page-level CDP connection for real-time events.
    """

    session_id: str
    port: int
    pid: int | None
    user_data_dir: str
    created_at: datetime = field(default_factory=datetime.now)

    # Connection components
    cdp: PersistentCDPClient | PowerShellCDPRelay | None = None  # For current page
    proxy: CDPProxyClient | None = None  # Fallback for one-shot commands
    browser_context_id: str | None = None

    # Tab tracking within this Chrome instance
    current_target_id: str | None = None
    targets: list[str] = field(default_factory=list)

    # Event-collected data
    console_messages: list[ConsoleMessage] = field(default_factory=list)
    network_requests: dict[str, NetworkRequest] = field(default_factory=dict)
    pending_dialog: DialogInfo | None = None

    # Snapshot cache for accessibility tree
    snapshot_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot_node_ids: dict[str, int] = field(default_factory=dict)  # uid -> backendNodeId

    # Performance trace state
    trace_active: bool = False
    trace_events: list[dict[str, Any]] = field(default_factory=list)

    # Emulation state (persisted across navigations)
    emulation_state: dict[str, Any] = field(default_factory=dict)

    @property
    def is_connected(self) -> bool:
        """Check if CDP client is connected."""
        return self.cdp is not None and self.cdp.is_connected

    def clear_page_state(self) -> None:
        """Clear state that should be reset on navigation."""
        self.console_messages.clear()
        self.network_requests.clear()
        self.snapshot_cache.clear()
        self.snapshot_node_ids.clear()

    def add_console_message(
        self,
        msg_type: str,
        text: str,
        timestamp: float | None = None,
        stack_trace: list[dict[str, Any]] | None = None,
        args: list[Any] | None = None,
    ) -> None:
        """Add a console message to the collection."""
        self.console_messages.append(
            ConsoleMessage(
                type=msg_type,
                text=text,
                timestamp=timestamp,
                stack_trace=stack_trace,
                args=args,
            )
        )

    def add_network_request(self, request_id: str, request: NetworkRequest) -> None:
        """Add or update a network request."""
        self.network_requests[request_id] = request

    def set_dialog(self, dialog: DialogInfo | None) -> None:
        """Set or clear the pending dialog."""
        self.pending_dialog = dialog


class ChromePoolManager:
    """Manages session-isolated BrowserContexts in a shared Chrome process."""

    def __init__(
        self,
        port_min: int = 9222,
        port_max: int = 9322,
        headless: bool = False,
    ) -> None:
        """Initialize the Chrome pool manager.

        Args:
            port_min: Debugging port for the shared Chrome process.
            port_max: Ignored; kept for backward compatibility.
            headless: Whether to launch Chrome in headless mode.
        """
        self._instances: dict[str, ChromeInstance] = {}
        self._port = port_min
        self._headless = headless
        self._chrome_path: str | None = None
        self._direct_tcp_works: bool = True
        self._shared_pid: int | None = None
        self._shared_user_data_dir: str | None = None
        self._shared_proxy: CDPProxyClient | None = None
        self._browser_cdp: PersistentCDPClient | None = None

        self._cleanup_orphaned_temp_dirs()

    def _cleanup_orphaned_temp_dirs(self) -> None:
        """Remove orphaned chrome-mcp-* temp directories from previous crashes.

        Only removes directories older than 24 hours to avoid deleting
        active session data.
        """
        ps_cmd = (
            "Get-ChildItem -Path $env:TEMP -Filter 'chrome-mcp-*' "
            "-Directory -ErrorAction SilentlyContinue | "
            "Where-Object { $_.CreationTime -lt (Get-Date).AddHours(-24) } | "
            "ForEach-Object { "
            "Remove-Item -Path $_.FullName -Recurse -Force "
            "-ErrorAction SilentlyContinue; "
            "Write-Output $_.Name "
            "}"
        )
        try:
            result = run_windows_command(ps_cmd, timeout=30.0)
            if result.returncode == 0 and result.stdout.strip():
                removed = [d for d in result.stdout.strip().split("\n") if d.strip()]
                if removed:
                    logger.info(
                        "Cleaned up %d orphaned temp dir(s): %s",
                        len(removed),
                        ", ".join(removed),
                    )
        except Exception as e:
            logger.warning("Failed to clean up orphaned temp dirs: %s", e)

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

    def _setup_event_handlers(self, instance: ChromeInstance) -> None:
        """Set up CDP event handlers for an instance."""
        if not instance.cdp:
            return

        # Console messages
        def on_console(params: dict[str, Any]) -> None:
            args = params.get("args", [])
            text_parts = []
            for arg in args:
                if "value" in arg:
                    text_parts.append(str(arg["value"]))
                elif "description" in arg:
                    text_parts.append(arg["description"])
                elif "preview" in arg:
                    # Object preview
                    preview = arg["preview"]
                    text_parts.append(preview.get("description", str(preview)))

            instance.add_console_message(
                msg_type=params.get("type", "log"),
                text=" ".join(text_parts) if text_parts else "",
                timestamp=params.get("timestamp"),
                stack_trace=params.get("stackTrace", {}).get("callFrames"),
                args=args,
            )

        instance.cdp.on("Runtime.consoleAPICalled", on_console)

        # Network requests
        def on_request_will_be_sent(params: dict[str, Any]) -> None:
            request_id = params["requestId"]
            request = params["request"]

            instance.add_network_request(
                request_id,
                NetworkRequest(
                    request_id=request_id,
                    url=request["url"],
                    method=request["method"],
                    timestamp=params.get("timestamp"),
                    type=params.get("type"),
                    headers=request.get("headers", {}),
                    post_data=request.get("postData"),
                ),
            )

        def on_response_received(params: dict[str, Any]) -> None:
            request_id = params["requestId"]
            if request_id in instance.network_requests:
                response = params["response"]
                req = instance.network_requests[request_id]
                req.response = {
                    "status": response["status"],
                    "statusText": response.get("statusText", ""),
                    "headers": response.get("headers", {}),
                    "mimeType": response.get("mimeType"),
                }

        instance.cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
        instance.cdp.on("Network.responseReceived", on_response_received)

        # Dialogs
        def on_dialog_opening(params: dict[str, Any]) -> None:
            instance.set_dialog(
                DialogInfo(
                    type=params["type"],
                    message=params["message"],
                    default_prompt=params.get("defaultPrompt"),
                    url=params.get("url"),
                )
            )
            logger.info(
                "Session %s: Dialog opened - type=%s, message=%s",
                instance.session_id,
                params["type"],
                params["message"][:50],
            )

        def on_dialog_closed(params: dict[str, Any]) -> None:
            instance.set_dialog(None)

        instance.cdp.on("Page.javascriptDialogOpening", on_dialog_opening)
        instance.cdp.on("Page.javascriptDialogClosed", on_dialog_closed)

        # Navigation (clear page state)
        def on_frame_navigated(params: dict[str, Any]) -> None:
            frame = params.get("frame", {})
            if frame.get("parentId") is None:
                # Main frame navigation - clear page-specific state
                instance.clear_page_state()
                logger.debug("Session %s: Main frame navigated, cleared state", instance.session_id)

        instance.cdp.on("Page.frameNavigated", on_frame_navigated)

        # Trace events (for performance)
        def on_trace_data_collected(params: dict[str, Any]) -> None:
            if instance.trace_active:
                instance.trace_events.extend(params.get("value", []))

        def on_tracing_complete(params: dict[str, Any]) -> None:
            instance.trace_active = False
            logger.info("Session %s: Tracing complete", instance.session_id)

        instance.cdp.on("Tracing.dataCollected", on_trace_data_collected)
        instance.cdp.on("Tracing.tracingComplete", on_tracing_complete)

    async def _connect_cdp(self, instance: ChromeInstance, target_id: str) -> None:
        """Establish persistent CDP connection for a target.

        Tries multiple WebSocket URLs in order:
        1. Original URL (localhost — works via WSL2 localhostForwarding)
        2. Rewritten URL with Windows host IP (fallback)

        Raises:
            RuntimeError: If no connection method is available.
            ConnectionError: If all connection attempts fail.
        """
        if not instance.proxy:
            raise RuntimeError("No proxy available to discover targets")

        targets = await instance.proxy.list_targets()
        target = next((t for t in targets if t.get("id") == target_id), None)
        if not target:
            raise RuntimeError(f"Target {target_id} not found")

        original_ws_url = target.get("webSocketDebuggerUrl", "")
        if not original_ws_url:
            raise RuntimeError(f"No WebSocket URL for target {target_id}")

        if instance.cdp and instance.cdp.is_connected:
            await instance.cdp.disconnect()

        last_error: Exception | None = None

        if self._direct_tcp_works:
            candidate_urls = self._build_ws_candidates(original_ws_url)
            for ws_url in candidate_urls:
                try:
                    logger.debug("Trying direct CDP connection: %s", ws_url)
                    client = PersistentCDPClient(ws_url, timeout=5.0)
                    await client.connect()
                    instance.cdp = client
                    await enable_domains(instance.cdp, ["Page", "Runtime", "Network", "DOM"])
                    self._setup_event_handlers(instance)
                    logger.info(
                        "Session %s: CDP connected to target %s via %s",
                        instance.session_id,
                        target_id,
                        ws_url,
                    )
                    return
                except Exception as e:
                    logger.debug("Direct CDP failed for %s: %s", ws_url, e)
                    last_error = e

            self._direct_tcp_works = False
            logger.info("Direct TCP connections failed; future attempts will use relay directly")

        if is_wsl():
            try:
                logger.info("Trying PowerShell CDP relay for %s", original_ws_url)
                relay = PowerShellCDPRelay(original_ws_url)
                await relay.connect()
                instance.cdp = relay
                await enable_domains(relay, ["Page", "Runtime", "Network", "DOM"])
                self._setup_event_handlers(instance)
                logger.info(
                    "Session %s: CDP connected via PowerShell relay to target %s",
                    instance.session_id,
                    target_id,
                )
                return
            except Exception as e:
                logger.warning("PowerShell CDP relay failed: %s", e)
                last_error = e

        raise ConnectionError(
            f"All CDP connection attempts failed for target {target_id}: {last_error}"
        )

    @staticmethod
    def _build_ws_candidates(original_ws_url: str) -> list[str]:
        """Build ordered list of WebSocket URLs to try.

        Non-WSL: just the original URL.
        WSL mirrored: localhost only (shares Windows network stack).
        WSL NAT: localhost first, then Windows host IP.
        """
        if not is_wsl():
            return [original_ws_url]

        if is_mirrored_networking():
            return [original_ws_url]

        candidates = [original_ws_url]

        windows_host = get_windows_host_ip()
        if windows_host not in ("127.0.0.1", "localhost"):
            rewritten = re.sub(
                r"ws://(127\.0\.0\.1|localhost)(:\d+)",
                f"ws://{windows_host}\\2",
                original_ws_url,
            )
            if rewritten != original_ws_url:
                candidates.append(rewritten)

        return candidates

    async def _ensure_shared_chrome(self) -> None:
        """Ensure the shared Chrome process and browser CDP are available."""
        if self._shared_proxy:
            try:
                version = await self._shared_proxy.get_version()
                if version:
                    if not self._browser_cdp or not self._browser_cdp.is_connected:
                        await self._connect_browser_cdp()
                    return
            except Exception as e:
                logger.debug("Shared Chrome health check failed: %s", e)

            logger.warning("Shared Chrome appears to have died; invalidating sessions")
            await self._invalidate_all_sessions()

        chrome_path = await self._find_chrome_path()

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
            "Launching shared Chrome on port %d (user_data_dir=%s)",
            self._port,
            user_data_dir,
        )

        args = [
            f"--remote-debugging-port={self._port}",
            "--remote-debugging-address=0.0.0.0",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            args.append("--headless=new")

        args_str = '","'.join(args)
        launch_ps = (
            f'$proc = Start-Process -FilePath "{chrome_path}" '
            f'-ArgumentList "{args_str}" -PassThru; '
            "Write-Output $proc.Id"
        )
        result = run_windows_command(launch_ps, timeout=10.0)

        shared_pid: int | None = None
        if result.returncode == 0 and result.stdout.strip():
            try:
                shared_pid = int(result.stdout.strip())
                logger.info("Shared Chrome launched with PID %d", shared_pid)
            except ValueError:
                logger.warning("Could not parse shared Chrome PID: %s", result.stdout)

        shared_proxy = CDPProxyClient(self._port)

        for _attempt in range(30):
            await asyncio.sleep(1)
            version = await shared_proxy.get_version()
            if version:
                logger.info(
                    "Shared Chrome ready on port %d: %s",
                    self._port,
                    version.get("Browser", "unknown"),
                )
                break
        else:
            raise RuntimeError(f"Chrome did not start within 30 seconds on port {self._port}")

        self._shared_pid = shared_pid
        self._shared_user_data_dir = user_data_dir
        self._shared_proxy = shared_proxy

        targets = await shared_proxy.list_targets()
        default_pages = [target for target in targets if target.get("type") == "page"]
        if default_pages:
            default_target_id = default_pages[0].get("id")
            if default_target_id:
                with contextlib.suppress(Exception):
                    await shared_proxy.close_page(default_target_id)
                logger.debug("Closed shared Chrome default tab %s", default_target_id)

        try:
            await self._connect_browser_cdp()
        except Exception:
            await self._kill_shared_chrome()
            raise

    async def _connect_browser_cdp(self) -> None:
        """Connect browser-level CDP for BrowserContext management."""
        if not self._shared_proxy:
            raise RuntimeError("Shared Chrome proxy is not initialized")

        browser_ws_url = await self._shared_proxy.get_browser_ws_url()
        if not browser_ws_url:
            raise RuntimeError("Failed to get browser WebSocket URL")

        if self._browser_cdp:
            with contextlib.suppress(Exception):
                await self._browser_cdp.disconnect()
            self._browser_cdp = None

        last_error: Exception | None = None
        for ws_url in self._build_ws_candidates(browser_ws_url):
            try:
                client = PersistentCDPClient(ws_url, timeout=5.0)
                await client.connect()
                self._browser_cdp = client
                logger.info("Connected browser CDP via %s", ws_url)
                return
            except Exception as e:
                last_error = e
                logger.debug("Browser CDP connection failed for %s: %s", ws_url, e)

        raise ConnectionError(f"Failed to connect browser CDP: {last_error}")

    async def _invalidate_all_sessions(self) -> None:
        """Invalidate all sessions after shared Chrome failure."""
        for instance in self._instances.values():
            await self._disconnect_cdp(instance)
        self._instances.clear()

        if self._browser_cdp:
            with contextlib.suppress(Exception):
                await self._browser_cdp.disconnect()

        self._browser_cdp = None
        self._shared_proxy = None
        self._shared_pid = None
        self._shared_user_data_dir = None
        self._direct_tcp_works = True

    async def _kill_shared_chrome(self) -> None:
        """Kill shared Chrome process and clean up resources."""
        if self._browser_cdp:
            with contextlib.suppress(Exception):
                await self._browser_cdp.disconnect()

        if self._shared_pid:
            logger.info("Killing shared Chrome PID %d", self._shared_pid)
            kill_ps = f"Stop-Process -Id {self._shared_pid} -Force -ErrorAction SilentlyContinue"
            try:
                run_windows_command(kill_ps, timeout=10.0)
            except Exception as e:
                logger.warning("Error killing shared Chrome PID %d: %s", self._shared_pid, e)

        if self._shared_user_data_dir:
            cleanup_ps = (
                f'Remove-Item -Path "{self._shared_user_data_dir}" '
                "-Recurse -Force -ErrorAction SilentlyContinue"
            )
            try:
                run_windows_command(cleanup_ps, timeout=10.0)
            except Exception as e:
                logger.warning("Error cleaning up %s: %s", self._shared_user_data_dir, e)

        self._browser_cdp = None
        self._shared_proxy = None
        self._shared_pid = None
        self._shared_user_data_dir = None
        self._direct_tcp_works = True

    async def _disconnect_cdp(self, instance: ChromeInstance) -> None:
        """Disconnect page-level CDP for an instance."""
        if instance.cdp:
            with contextlib.suppress(Exception):
                await instance.cdp.disconnect()
            instance.cdp = None

    async def get_or_create(self, session_id: str) -> ChromeInstance:
        """Get existing Chrome instance or create new one for this session.

        Args:
            session_id: The opencode session identifier.

        Returns:
            ChromeInstance for the requested session.
        """
        if session_id in self._instances:
            instance = self._instances[session_id]
            logger.debug("Returning existing Chrome for session %s", session_id)

            if not instance.is_connected and instance.current_target_id:
                logger.info("Reconnecting CDP for session %s", session_id)
                try:
                    await self._connect_cdp(instance, instance.current_target_id)
                except Exception as e:
                    logger.warning("Failed to reconnect CDP: %s", e)

            return instance

        logger.info("Creating new Chrome session %s", session_id)

        await self._ensure_shared_chrome()

        if not self._browser_cdp or not self._shared_proxy:
            raise RuntimeError("Shared Chrome is not available")

        browser_context_id: str | None = None
        try:
            context_result = await self._browser_cdp.send(
                "Target.createBrowserContext",
                {"disposeOnDetach": True},
            )
            browser_context_id = context_result["browserContextId"]
            logger.info(
                "Session %s: created browser context %s",
                session_id,
                browser_context_id,
            )

            target_result = await self._browser_cdp.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "browserContextId": browser_context_id,
                },
            )
            initial_target_id = target_result["targetId"]

            instance = ChromeInstance(
                session_id=session_id,
                port=self._port,
                pid=self._shared_pid,
                user_data_dir=self._shared_user_data_dir or "",
                proxy=self._shared_proxy,
                browser_context_id=browser_context_id,
                current_target_id=initial_target_id,
                targets=[initial_target_id],
            )

            try:
                await self._connect_cdp(instance, initial_target_id)
            except Exception as e:
                logger.warning(
                    "Failed to establish persistent CDP connection, falling back to proxy: %s",
                    e,
                )

            if instance.is_connected:
                logger.info(
                    "Session %s: persistent CDP connection established",
                    session_id,
                )
            elif instance.proxy:
                logger.info(
                    "Session %s: using proxy-only mode (no persistent CDP)",
                    session_id,
                )
            else:
                logger.error(
                    "Session %s: no connection method available",
                    session_id,
                )

            self._instances[session_id] = instance
            return instance
        except Exception:
            if browser_context_id and self._browser_cdp and self._browser_cdp.is_connected:
                with contextlib.suppress(Exception):
                    await self._browser_cdp.send(
                        "Target.disposeBrowserContext",
                        {"browserContextId": browser_context_id},
                    )
            raise

    async def destroy(self, session_id: str) -> None:
        """Destroy a session's Chrome instance.

        Args:
            session_id: The session to destroy.

        Raises:
            KeyError: If session not found.
        """
        instance = self._instances.pop(session_id)
        await self._disconnect_cdp(instance)

        if instance.browser_context_id and self._browser_cdp and self._browser_cdp.is_connected:
            try:
                await self._browser_cdp.send(
                    "Target.disposeBrowserContext",
                    {"browserContextId": instance.browser_context_id},
                )
                logger.info(
                    "Session %s: disposed browser context %s",
                    session_id,
                    instance.browser_context_id,
                )
            except Exception as e:
                logger.warning(
                    "Session %s: failed to dispose browser context %s: %s",
                    session_id,
                    instance.browser_context_id,
                    e,
                )

        logger.info("Destroyed Chrome session %s", session_id)

    async def cleanup_all(self) -> None:
        """Dispose all sessions and shut down shared Chrome."""
        logger.info("Cleaning up %d Chrome session(s)", len(self._instances))
        for session_id in list(self._instances.keys()):
            try:
                await self.destroy(session_id)
            except Exception as e:
                logger.warning("Error destroying session %s: %s", session_id, e)

        await self._kill_shared_chrome()

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
                "browser_context_id": instance.browser_context_id,
                "tab_count": len(instance.targets),
                "current_target_id": instance.current_target_id,
                "created_at": instance.created_at.isoformat(),
                "connected": instance.is_connected,
                "console_count": len(instance.console_messages),
                "network_count": len(instance.network_requests),
            }
        return result

    # --- Tab operations (within a session's Chrome) ---

    async def create_tab(self, session_id: str, url: str = "about:blank") -> str:
        """Create a new tab in a session's Chrome.

        Args:
            session_id: The session to create the tab in.
            url: URL to open in the new tab.

        Returns:
            The target_id of the new tab.

        Raises:
            KeyError: If session not found.
        """
        instance = self._instances[session_id]

        if not instance.browser_context_id:
            raise RuntimeError("No browser context available for session")

        if not self._browser_cdp or not self._browser_cdp.is_connected:
            await self._ensure_shared_chrome()

        if not self._browser_cdp:
            raise RuntimeError("Browser CDP is not connected")

        result = await self._browser_cdp.send(
            "Target.createTarget",
            {
                "url": url,
                "browserContextId": instance.browser_context_id,
            },
        )
        target_id = result["targetId"]

        instance.targets.append(target_id)

        # Switch to new tab
        await self.switch_tab(session_id, target_id)

        logger.info("Session %s: created tab %s -> %s", session_id, target_id, url)
        return target_id

    async def switch_tab(self, session_id: str, target_id: str) -> None:
        """Switch the active tab in a session's Chrome.

        Args:
            session_id: The session to switch tabs in.
            target_id: The target_id to switch to.

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

        # Disconnect from old tab
        if instance.cdp:
            with contextlib.suppress(Exception):
                await instance.cdp.disconnect()
            instance.cdp = None

        if self._browser_cdp and self._browser_cdp.is_connected:
            try:
                await self._browser_cdp.send(
                    "Target.activateTarget",
                    {"targetId": target_id},
                )
            except Exception as e:
                logger.debug(
                    "Failed to activate target %s for session %s: %s",
                    target_id,
                    session_id,
                    e,
                )

        instance.current_target_id = target_id
        instance.clear_page_state()

        # Connect to new tab
        try:
            await self._connect_cdp(instance, target_id)
        except Exception as e:
            logger.warning("Failed to connect CDP to new tab: %s", e)

        logger.info("Session %s: switched to tab %s", session_id, target_id)

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

        # If closing current tab, disconnect CDP first
        if instance.current_target_id == target_id and instance.cdp:
            await instance.cdp.disconnect()
            instance.cdp = None

        # Close the target via proxy
        if instance.proxy:
            await instance.proxy.close_page(target_id)

        # Update state
        instance.targets.remove(target_id)

        # Switch to another tab if we closed the current one
        if instance.current_target_id == target_id:
            new_target_id = instance.targets[0]
            instance.current_target_id = new_target_id
            instance.clear_page_state()

            # Connect to new tab
            try:
                await self._connect_cdp(instance, new_target_id)
            except Exception as e:
                logger.warning("Failed to connect CDP to new tab: %s", e)

            logger.info("Session %s: auto-switched to tab %s", session_id, new_target_id)

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

        if not instance.proxy:
            return []

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
