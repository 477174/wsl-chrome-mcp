#!/bin/bash
set -euo pipefail

# WSL Chrome MCP - Network Setup Script
# Enables WSL2 mirrored networking for sub-second Chrome DevTools connections.
#
# What this does:
#   Adds networkingMode=mirrored to your .wslconfig so WSL2 shares the
#   Windows network stack. This lets the MCP server connect directly to
#   Chrome's WebSocket on localhost instead of going through a slow
#   PowerShell relay (~50ms vs ~1-2s per CDP call).
#
# Requirements:
#   - Windows 11 22H2+ (build 22621+)
#   - WSL2 2.0+
#
# After running this script, you MUST restart WSL:
#   wsl.exe --shutdown
#   (then reopen your terminal)

WSLCONFIG="/mnt/c/Users/${USER}/.wslconfig"
MIN_BUILD=22621

detect_windows_user() {
    if [[ -f "${WSLCONFIG}" ]]; then
        return 0
    fi

    for dir in /mnt/c/Users/*/; do
        local user
        user=$(basename "$dir")
        [[ "$user" == "Public" || "$user" == "Default" || "$user" == "Default User" || "$user" == "All Users" ]] && continue
        [[ "$user" == Todos* || "$user" == Usu* ]] && continue
        [[ ! -d "$dir" ]] && continue
        WSLCONFIG="${dir}.wslconfig"
        echo "  Detected Windows user: ${user}"
        return 0
    done

    echo "ERROR: Could not find Windows user home directory."
    echo "Set WSLCONFIG manually: WSLCONFIG=/mnt/c/Users/YourName/.wslconfig $0"
    exit 1
}

check_windows_build() {
    local ps_path="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if [[ ! -x "$ps_path" ]]; then
        ps_path=$(command -v powershell.exe 2>/dev/null || true)
    fi

    if [[ -z "$ps_path" ]]; then
        echo "WARNING: Cannot verify Windows build (powershell.exe not found)."
        echo "         Mirrored networking requires Windows 11 build ${MIN_BUILD}+."
        return 0
    fi

    local build
    build=$("$ps_path" -NoProfile -Command "Write-Host (Get-CimInstance Win32_OperatingSystem).BuildNumber" 2>/dev/null | tr -d '\r')

    if [[ -z "$build" || ! "$build" =~ ^[0-9]+$ ]]; then
        echo "WARNING: Cannot determine Windows build number."
        return 0
    fi

    echo "  Windows build: ${build}"

    if (( build < MIN_BUILD )); then
        echo "ERROR: Windows build ${build} is too old. Mirrored networking requires ${MIN_BUILD}+."
        echo "       Update Windows to 22H2 or later."
        exit 1
    fi
}

update_wslconfig() {
    if [[ -f "$WSLCONFIG" ]]; then
        if grep -qi "networkingMode\s*=\s*mirrored" "$WSLCONFIG" 2>/dev/null; then
            echo "  networkingMode=mirrored is already set in ${WSLCONFIG}"
            return 0
        fi

        if grep -qi "networkingMode" "$WSLCONFIG" 2>/dev/null; then
            echo "  WARNING: networkingMode is set to something other than 'mirrored'."
            echo "  Current setting:"
            grep -i "networkingMode" "$WSLCONFIG"
            echo ""
            read -rp "  Replace with networkingMode=mirrored? [y/N] " answer
            if [[ "$answer" != [yY]* ]]; then
                echo "  Skipped. No changes made."
                exit 0
            fi
            sed -i 's/^[[:space:]]*networkingMode[[:space:]]*=.*/networkingMode=mirrored/' "$WSLCONFIG"
            echo "  Updated networkingMode to mirrored"
            return 0
        fi

        if grep -qi "^\[wsl2\]" "$WSLCONFIG" 2>/dev/null; then
            sed -i '/^\[wsl2\]/a networkingMode=mirrored' "$WSLCONFIG"
            echo "  Added networkingMode=mirrored under [wsl2]"
        else
            printf '\n[wsl2]\nnetworkingMode=mirrored\n' >> "$WSLCONFIG"
            echo "  Added [wsl2] section with networkingMode=mirrored"
        fi
    else
        printf '[wsl2]\nnetworkingMode=mirrored\n' > "$WSLCONFIG"
        echo "  Created ${WSLCONFIG} with networkingMode=mirrored"
    fi
}

echo "=== WSL Chrome MCP - Network Setup ==="
echo ""
echo "This enables WSL2 mirrored networking for sub-second Chrome connections."
echo ""

echo "[1/3] Detecting Windows user..."
detect_windows_user

echo "[2/3] Checking Windows build..."
check_windows_build

echo "[3/3] Updating .wslconfig..."
update_wslconfig

echo ""
echo "=== Done ==="
echo ""
echo "Config file: ${WSLCONFIG}"
echo ""
echo "Current contents:"
cat "$WSLCONFIG"
echo ""
echo "IMPORTANT: You must restart WSL for changes to take effect:"
echo "  1. Save all work in WSL terminals"
echo "  2. Run in PowerShell:  wsl.exe --shutdown"
echo "  3. Reopen your WSL terminal"
echo ""
echo "After restart, the MCP server will connect directly to Chrome via"
echo "localhost WebSocket (~50ms) instead of the PowerShell relay (~1-2s)."
