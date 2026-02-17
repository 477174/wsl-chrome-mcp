"""Persistent CDP relay via a long-running PowerShell WebSocket process.

In WSL2, direct TCP from Linux to Windows Chrome is blocked (firewall + localhost
binding). This module keeps ONE PowerShell process alive with ONE persistent
WebSocket to Chrome, piping CDP JSON through stdin/stdout.

Interface mirrors PersistentCDPClient so it drops in as instance.cdp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from .persistent_cdp import CDPError
from .wsl import _find_windows_executable, convert_wsl_to_windows_path

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

# C# compiled inside PowerShell — bidirectional stdin/stdout <-> WebSocket relay.
# Background Task reads WebSocket and writes JSON lines to stdout.
# Main thread reads JSON lines from stdin and writes to WebSocket.
_RELAY_CSHARP = r"""
using System;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

public class CDPRelay
{
    public static void Run(string wsUrl)
    {
        var ws = new ClientWebSocket();
        ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(30);
        var cts = new CancellationTokenSource();
        var token = cts.Token;

        try
        {
            ws.ConnectAsync(new Uri(wsUrl), token).GetAwaiter().GetResult();
            Console.Error.WriteLine("CONNECTED");
            Console.Error.Flush();

            // WebSocket -> stdout (background)
            var reader = Task.Run(() =>
            {
                var buf = new byte[4 * 1024 * 1024];
                try
                {
                    while (ws.State == WebSocketState.Open && !token.IsCancellationRequested)
                    {
                        var sb = new StringBuilder();
                        WebSocketReceiveResult recv;
                        do
                        {
                            var seg = new ArraySegment<byte>(buf);
                            recv = ws.ReceiveAsync(seg, token).GetAwaiter().GetResult();
                            if (recv.MessageType == WebSocketMessageType.Close) return;
                            sb.Append(Encoding.UTF8.GetString(buf, 0, recv.Count));
                        } while (!recv.EndOfMessage);

                        Console.Out.WriteLine(sb.ToString());
                        Console.Out.Flush();
                    }
                }
                catch (OperationCanceledException) { }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("READER_ERROR:" + ex.Message);
                    Console.Error.Flush();
                }
            }, token);

            // stdin -> WebSocket (main thread)
            string line;
            while ((line = Console.In.ReadLine()) != null)
            {
                if (ws.State != WebSocketState.Open) break;
                var bytes = Encoding.UTF8.GetBytes(line);
                var seg = new ArraySegment<byte>(bytes);
                ws.SendAsync(seg, WebSocketMessageType.Text, true, token)
                    .GetAwaiter().GetResult();
            }

            cts.Cancel();
            reader.Wait(TimeSpan.FromSeconds(2));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("FATAL:" + ex.Message);
            Console.Error.Flush();
        }
        finally
        {
            if (ws.State == WebSocketState.Open)
            {
                try
                {
                    ws.CloseAsync(
                        WebSocketCloseStatus.NormalClosure, "",
                        CancellationToken.None
                    ).GetAwaiter().GetResult();
                }
                catch { }
            }
            ws.Dispose();
        }
    }
}
"""


def _build_relay_script(ws_url: str) -> str:
    escaped_url = ws_url.replace("'", "''")
    return f"""$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
{_RELAY_CSHARP}
'@
[CDPRelay]::Run('{escaped_url}')
"""


class PowerShellCDPRelay:
    """CDP client relaying commands through a persistent PowerShell WebSocket.

    Drop-in replacement for PersistentCDPClient when direct TCP is blocked.
    """

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout

        self._process: asyncio.subprocess.Process | None = None
        self._message_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._receive_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._connected = False
        self._script_path: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._process is not None

    async def connect(self) -> None:
        if self._connected:
            return

        powershell = _find_windows_executable("powershell.exe")
        if not powershell:
            raise RuntimeError("powershell.exe not found")

        script_content = _build_relay_script(self.ws_url)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ps1",
            prefix="cdp_relay_",
            dir="/tmp",
            delete=False,
        ) as tmp:
            tmp.write(script_content)
            self._script_path = tmp.name

        win_script_path = convert_wsl_to_windows_path(self._script_path)

        logger.debug("Starting PowerShell CDP relay for %s", self.ws_url)

        # Use 16MB buffer limit to handle large CDP messages (accessibility
        # trees, full-page DOM snapshots, etc.).  The asyncio default is 64KB
        # which causes "Separator is not found, and chunk exceed the limit"
        # errors on content-heavy pages like google.com.
        buf_limit = 16 * 1024 * 1024  # 16 MB

        self._process = await asyncio.create_subprocess_exec(
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            win_script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=buf_limit,
        )

        try:
            connected = await asyncio.wait_for(
                self._wait_for_connected(),
                timeout=30.0,
            )
            if not connected:
                raise ConnectionError("PowerShell relay failed to connect")
        except asyncio.TimeoutError as err:
            await self._kill_process()
            raise ConnectionError("PowerShell relay timed out connecting") from err

        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        logger.info("PowerShell CDP relay connected: %s", self.ws_url)

    async def _wait_for_connected(self) -> bool:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return False
            text = line.decode("utf-8", errors="replace").strip()
            if text == "CONNECTED":
                return True
            if text.startswith("FATAL:"):
                logger.error("Relay fatal: %s", text)
                return False

    async def disconnect(self) -> None:
        if not self._connected:
            return

        self._connected = False

        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Relay disconnected"))
        self._pending.clear()

        if self._receive_task:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

        await self._kill_process()

        if self._script_path:
            with contextlib.suppress(OSError):
                os.unlink(self._script_path)
            self._script_path = None

        logger.info("PowerShell CDP relay disconnected")

    async def _kill_process(self) -> None:
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
            self._process = None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self._connected or not self._process or not self._process.stdin:
            raise ConnectionError("Relay not connected")

        self._message_id += 1
        msg_id = self._message_id

        message: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            line = json.dumps(message, separators=(",", ":")) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except asyncio.TimeoutError as err:
            self._pending.pop(msg_id, None)
            raise asyncio.TimeoutError(f"Timeout waiting for {method}") from err
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    def on(self, event: str, handler: EventHandler) -> None:
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    def off(self, event: str, handler: EventHandler | None = None) -> None:
        if event not in self._event_handlers:
            return
        if handler is None:
            del self._event_handlers[event]
        else:
            self._event_handlers[event] = [h for h in self._event_handlers[event] if h != handler]

    async def _receive_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            while self._connected:
                line = await self._process.stdout.readline()
                if not line:
                    logger.warning("Relay stdout closed")
                    self._connected = False
                    break
                try:
                    data = json.loads(line)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.warning("Invalid JSON from relay: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Relay receive error: %s", e)
            self._connected = False

    async def _stderr_loop(self) -> None:
        assert self._process and self._process.stderr
        try:
            while self._connected:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("Relay stderr: %s", text)
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, data: dict[str, Any]) -> None:
        if "id" in data:
            msg_id = data["id"]
            if msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if "error" in data:
                    error = data["error"]
                    future.set_exception(
                        CDPError(error.get("message", "Unknown error"), error.get("code"))
                    )
                else:
                    future.set_result(data.get("result", {}))
        elif "method" in data:
            await self._dispatch_event(data["method"], data.get("params", {}))

    async def _dispatch_event(self, event: str, params: dict[str, Any]) -> None:
        handlers = self._event_handlers.get(event, [])
        for handler in handlers:
            try:
                result = handler(params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("Event handler error for %s: %s", event, e)
