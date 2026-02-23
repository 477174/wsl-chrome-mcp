"""Manage Windows-side configuration from WSL.

Handles .wslconfig networking settings and Chrome always-on CDP
(registry + shortcut modification).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .wsl import is_wsl, run_windows_command

logger = logging.getLogger("wsl-chrome-mcp")

_SKIP_USERS = frozenset(
    {
        "Public",
        "Default",
        "Default User",
        "All Users",
    }
)

# Prefixes to skip (Portuguese locale variants)
_SKIP_PREFIXES = ("Todos", "Usu")


def find_wslconfig_path() -> Path | None:
    """Find the .wslconfig path for the current Windows user.

    Strategy:
    1. Try /mnt/c/Users/$USER/ first (even if .wslconfig doesn't exist yet,
       this is the default write target if $USER maps to a Windows directory).
    2. Scan /mnt/c/Users/*/ — skip system directories and locale variants.
    3. Return the first real user directory found (for writing).
    4. Return None if no usable Windows user directory found.

    Returns:
        Path to .wslconfig (may not exist yet), or None.
    """
    users_dir = Path("/mnt/c/Users")
    if not users_dir.is_dir():
        return None

    # Attempt 1: match $USER to a Windows user directory
    login = os.environ.get("USER", "")
    if login:
        candidate = users_dir / login / ".wslconfig"
        if candidate.parent.is_dir():
            return candidate

    # Attempt 2: scan for the first real user directory
    for entry in sorted(users_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in _SKIP_USERS:
            continue
        if any(name.startswith(p) for p in _SKIP_PREFIXES):
            continue
        return entry / ".wslconfig"

    return None


def is_mirrored_enabled(config_path: Path | None = None) -> bool:
    """Check if networkingMode=mirrored is set in .wslconfig.

    Args:
        config_path: Explicit path to .wslconfig. Auto-detected if None.

    Returns:
        True if mirrored networking is enabled.
    """
    if config_path is None:
        config_path = find_wslconfig_path()
    if config_path is None or not config_path.exists():
        return False

    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"networkingMode\s*=\s*mirrored", stripped, re.IGNORECASE):
            return True

    return False


def set_mirrored_networking(enable: bool, config_path: Path | None = None) -> str:
    """Enable or disable mirrored networking in .wslconfig.

    Modifies the file in-place, preserving all other settings.

    Args:
        enable: True to enable, False to disable.
        config_path: Explicit path. Auto-detected if None.

    Returns:
        Human-readable status message.
    """
    if config_path is None:
        config_path = find_wslconfig_path()
    if config_path is None:
        return "Error: Could not find .wslconfig path (no Windows user directory found)"

    _NETWORKING_RE = re.compile(r"^\s*networkingMode\s*=", re.IGNORECASE)
    _MIRRORED_RE = re.compile(r"^\s*networkingMode\s*=\s*mirrored\s*$", re.IGNORECASE)
    _WSL2_HEADER_RE = re.compile(r"^\s*\[wsl2\]\s*$", re.IGNORECASE)

    if enable:
        return _enable_mirrored(config_path, _NETWORKING_RE, _MIRRORED_RE, _WSL2_HEADER_RE)
    return _disable_mirrored(config_path, _NETWORKING_RE, _MIRRORED_RE)


def _enable_mirrored(
    config_path: Path,
    networking_re: re.Pattern[str],
    mirrored_re: re.Pattern[str],
    wsl2_re: re.Pattern[str],
) -> str:
    """Enable mirrored networking."""
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("[wsl2]\nnetworkingMode=mirrored\n", encoding="utf-8")
        return f"Mirrored networking enabled in {config_path}. Restart WSL to apply."

    lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    # Check if already set
    for line in lines:
        if mirrored_re.match(line):
            return "Already enabled"

    # Check if networkingMode exists with different value → replace
    for i, line in enumerate(lines):
        if networking_re.match(line):
            lines[i] = "networkingMode=mirrored\n"
            config_path.write_text("".join(lines), encoding="utf-8")
            return f"Mirrored networking enabled in {config_path}. Restart WSL to apply."

    # Check if [wsl2] section exists → add after it
    for i, line in enumerate(lines):
        if wsl2_re.match(line):
            lines.insert(i + 1, "networkingMode=mirrored\n")
            config_path.write_text("".join(lines), encoding="utf-8")
            return f"Mirrored networking enabled in {config_path}. Restart WSL to apply."

    # No [wsl2] section → append
    # Ensure trailing newline before adding section
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n[wsl2]\nnetworkingMode=mirrored\n"
    config_path.write_text(text, encoding="utf-8")
    return f"Mirrored networking enabled in {config_path}. Restart WSL to apply."


def _disable_mirrored(
    config_path: Path,
    networking_re: re.Pattern[str],
    mirrored_re: re.Pattern[str],
) -> str:
    """Disable mirrored networking by removing the line."""
    if not config_path.exists():
        return "Already disabled"

    lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    # Find and remove the networkingMode=mirrored line
    found = False
    new_lines: list[str] = []
    for line in lines:
        if mirrored_re.match(line):
            found = True
            continue
        new_lines.append(line)

    if not found:
        return "Already disabled"

    config_path.write_text("".join(new_lines), encoding="utf-8")
    return f"Mirrored networking disabled in {config_path}. Restart WSL to apply."


# ---------------------------------------------------------------------------
# Always-on CDP: inject --remote-debugging-port into Chrome launch paths
# ---------------------------------------------------------------------------

_CDP_FLAG_PATTERN = r"--remote-debugging-port=\d+"


def set_always_on_cdp(enable: bool, port: int = 9222) -> str:
    if not is_wsl():
        return "Error: not running in WSL"

    flag = f"--remote-debugging-port={port}"

    if enable:
        return _enable_cdp(flag, port)
    return _disable_cdp()


_CDP_SHORTCUT_DIRS = r"""$shortcutDirs = @(
    "$env:USERPROFILE\Desktop",
    "$env:PUBLIC\Desktop",
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
    "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar",
    "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch"
)"""


def _enable_cdp(flag: str, port: int) -> str:
    ps = f"""
$flag = "{flag}"
$modified = [System.Collections.Generic.List[string]]::new()
$shell = New-Object -ComObject WScript.Shell

{_CDP_SHORTCUT_DIRS}
foreach ($dir in $shortcutDirs) {{
    if (-not (Test-Path $dir)) {{ continue }}
    Get-ChildItem $dir -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object {{
        $sc = $shell.CreateShortcut($_.FullName)
        if ($sc.TargetPath -match 'chrome\\.exe' -and $sc.Arguments -notmatch '--remote-debugging-port') {{
            try {{
                $sc.Arguments = ("$flag " + $sc.Arguments).Trim()
                $sc.Save()
                $modified.Add("shortcut:" + $_.Name)
            }} catch {{}}
        }}
    }}
}}

$hkcuBase = 'HKCU:\\Software\\Classes'
$handled = [System.Collections.Generic.HashSet[string]]::new()

foreach ($root in @($hkcuBase, 'HKLM:\\SOFTWARE\\Classes')) {{
    Get-ChildItem $root -ErrorAction SilentlyContinue | Where-Object {{
        $_.PSChildName -match '^ChromeHTML'
    }} | ForEach-Object {{
        $progId = $_.PSChildName
        if ($handled.Contains($progId)) {{ return }}
        $cmdPath = Join-Path $_.PSPath 'shell\\open\\command'
        if (-not (Test-Path $cmdPath)) {{ return }}
        $val = (Get-ItemProperty -Path $cmdPath -Name '(Default)').'(Default)'
        if ($val -notmatch 'chrome\\.exe') {{ return }}
        $handled.Add($progId) | Out-Null
        if ($val -match '--remote-debugging-port') {{ return }}
        $newVal = $val -replace '(chrome\.exe")\\s*', ('$1 ' + $flag + ' ')
        $hkcuCmdPath = "$hkcuBase\\$progId\\shell\\open\\command"
        if (-not (Test-Path $hkcuCmdPath)) {{
            New-Item -Path $hkcuCmdPath -Force | Out-Null
        }}
        Set-ItemProperty -Path $hkcuCmdPath -Name '(Default)' -Value $newVal
        $modified.Add("registry:" + $progId)
    }}
}}

if ($modified.Count -gt 0) {{
    Write-Output ("MODIFIED:" + ($modified -join ","))
}} else {{
    Write-Output "ALREADY_SET"
}}
"""
    try:
        result = run_windows_command(ps, timeout=20.0)
    except Exception as e:
        return f"Error: {e}"

    output = result.stdout.strip()
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"

    if output == "ALREADY_SET":
        return f"Already enabled (port {port})"

    if output.startswith("MODIFIED:"):
        targets = output.removeprefix("MODIFIED:").split(",")
        label = ", ".join(targets)
        return f"Always-on CDP enabled (port {port}). Modified: {label}. Restart Chrome to apply."

    return f"CDP enabled (port {port}). Output: {output}"


def _disable_cdp() -> str:
    ps = f"""
$modified = [System.Collections.Generic.List[string]]::new()
$shell = New-Object -ComObject WScript.Shell

{_CDP_SHORTCUT_DIRS}
foreach ($dir in $shortcutDirs) {{
    if (-not (Test-Path $dir)) {{ continue }}
    Get-ChildItem $dir -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object {{
        $sc = $shell.CreateShortcut($_.FullName)
        if ($sc.TargetPath -match 'chrome\\.exe' -and $sc.Arguments -match '--remote-debugging-port=\\d+') {{
            try {{
                $a = $sc.Arguments
                $a = $a -replace '\\s*--remote-debugging-port=\\d+', ''
                $a = $a -replace '\\s*--user-data-dir="[^"]*"', ''
                $a = $a -replace '\\s*--user-data-dir=\\S+', ''
                $sc.Arguments = $a.Trim()
                $sc.Save()
                $modified.Add("shortcut:" + $_.Name)
            }} catch {{}}
        }}
    }}
}}

$hkcuBase = 'HKCU:\\Software\\Classes'
Get-ChildItem $hkcuBase -ErrorAction SilentlyContinue | Where-Object {{
    $_.PSChildName -match '^ChromeHTML'
}} | ForEach-Object {{
    $cmdPath = Join-Path $_.PSPath 'shell\\open\\command'
    if (Test-Path $cmdPath) {{
        $val = (Get-ItemProperty -Path $cmdPath -Name '(Default)').'(Default)'
        if ($val -match '--remote-debugging-port=\\d+') {{
            $newVal = $val
            $newVal = $newVal -replace '\\s*--remote-debugging-port=\\d+', ''
            $newVal = $newVal -replace '\\s*--user-data-dir="[^"]*"', ''
            $newVal = $newVal -replace '\\s*--user-data-dir=\\S+', ''
            Set-ItemProperty -Path $cmdPath -Name '(Default)' -Value $newVal.Trim()
            $modified.Add("registry:" + $_.PSChildName)
        }}
    }}
}}

$junctionPath = "$env:LOCALAPPDATA\\Google\\Chrome\\MCP Data"
if (Test-Path $junctionPath) {{
    cmd /c rmdir "`"$junctionPath`"" 2>$null
    $modified.Add("junction:MCP Data")
}}

if ($modified.Count -gt 0) {{
    Write-Output ("REMOVED:" + ($modified -join ","))
}} else {{
    Write-Output "ALREADY_OFF"
}}
"""
    try:
        result = run_windows_command(ps, timeout=20.0)
    except Exception as e:
        return f"Error: {e}"

    output = result.stdout.strip()
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"

    if output == "ALREADY_OFF":
        return "Already disabled"

    if output.startswith("REMOVED:"):
        targets = output.removeprefix("REMOVED:").split(",")
        label = ", ".join(targets)
        return f"Always-on CDP disabled. Restored: {label}. Restart Chrome to apply."

    return f"CDP disabled. Output: {output}"
