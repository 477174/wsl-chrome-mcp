"""Live system state detection for WSL Chrome MCP configuration TUI."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .wsl import is_mirrored_networking, is_wsl


OPENCODE_PLUGIN_DIR = Path.home() / ".config" / "opencode" / "plugins"
PLUGIN_FILENAME = "chrome-session.ts"


@dataclass
class ChromeState:
    running: bool = False
    pid: int | None = None
    port: int = 9222
    version: str = ""
    active_targets: int = 0


@dataclass
class WslState:
    is_wsl: bool = False
    version: str = ""  # "WSL1" | "WSL2" | "Native"
    windows_build: str = ""
    mirrored_networking: bool = False


@dataclass
class InstallState:
    mcp_installed: bool = False
    mcp_version: str = ""
    mcp_path: str = ""
    plugin_installed: bool = False
    plugin_path: str = ""


@dataclass
class SystemState:
    chrome: ChromeState = field(default_factory=ChromeState)
    wsl: WslState = field(default_factory=WslState)
    install: InstallState = field(default_factory=InstallState)


def _detect_wsl_state() -> WslState:
    state = WslState(is_wsl=is_wsl())

    if not state.is_wsl:
        state.version = "Native"
        return state

    try:
        version_text = Path("/proc/version").read_text()
        state.version = "WSL2" if "microsoft" in version_text.lower() else "WSL1"
    except OSError:
        state.version = "WSL2"

    state.mirrored_networking = is_mirrored_networking()

    ps_path = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [
                ps_path,
                "-NoProfile",
                "-Command",
                "Write-Host (Get-CimInstance Win32_OperatingSystem).BuildNumber",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            state.windows_build = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    return state


def _detect_chrome_state(port: int) -> ChromeState:
    state = ChromeState(port=port)
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"http://localhost:{port}/json/version")
            if resp.status_code == 200:
                data = resp.json()
                state.running = True
                state.version = data.get("Browser", "")

            targets_resp = client.get(f"http://localhost:{port}/json/list")
            if targets_resp.status_code == 200:
                targets = targets_resp.json()
                state.active_targets = len([t for t in targets if t.get("type") == "page"])
    except (httpx.RequestError, httpx.HTTPStatusError):
        pass

    return state


def _detect_install_state() -> InstallState:
    state = InstallState()

    mcp_path = shutil.which("wsl-chrome-mcp")
    if mcp_path:
        state.mcp_installed = True
        state.mcp_path = mcp_path
        try:
            result = subprocess.run(
                ["wsl-chrome-mcp", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                state.mcp_version = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            state.mcp_version = "installed"

    plugin_path = OPENCODE_PLUGIN_DIR / PLUGIN_FILENAME
    if plugin_path.exists():
        state.plugin_installed = True
        state.plugin_path = str(plugin_path)

    return state


@dataclass
class ChromeProfile:
    dir_name: str
    display_name: str


_SKIP_USER_DIRS = frozenset({"Public", "Default", "Default User", "All Users"})


def discover_chrome_profiles() -> list[ChromeProfile]:
    users_dir = Path("/mnt/c/Users")
    if not users_dir.is_dir():
        return []

    for user_dir in sorted(users_dir.iterdir()):
        if not user_dir.is_dir() or user_dir.name in _SKIP_USER_DIRS:
            continue
        local_state = (
            user_dir / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Local State"
        )
        if not local_state.exists():
            continue
        try:
            data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
            info_cache = data.get("profile", {}).get("info_cache", {})
            return [
                ChromeProfile(dir_name=dn, display_name=info.get("name", dn))
                for dn, info in sorted(info_cache.items())
            ]
        except (json.JSONDecodeError, OSError, KeyError):
            continue

    return []


def detect_system_state(chrome_port: int = 9222) -> SystemState:
    """Detect full system state for the TUI dashboard."""
    return SystemState(
        chrome=_detect_chrome_state(chrome_port),
        wsl=_detect_wsl_state(),
        install=_detect_install_state(),
    )
