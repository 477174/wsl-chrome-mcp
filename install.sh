#!/bin/bash
set -euo pipefail

# WSL Chrome MCP - Install Script
# Installs the MCP server and opencode plugin for multi-session Chrome support

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="${HOME}/.config/opencode/plugins"
PLUGIN_SRC="${SCRIPT_DIR}/opencode-plugin/chrome-session.ts"

echo "=== WSL Chrome MCP Installer ==="
echo ""

# Step 1: Install the MCP server
echo "[1/2] Installing wsl-chrome-mcp..."
if command -v uv &>/dev/null; then
    uv tool install "${SCRIPT_DIR}" --force
    echo "  Installed via uv"
elif command -v pip &>/dev/null; then
    pip install "${SCRIPT_DIR}"
    echo "  Installed via pip"
else
    echo "  ERROR: Neither uv nor pip found. Install uv: https://docs.astral.sh/uv/"
    exit 1
fi

# Verify installation
if command -v wsl-chrome-mcp &>/dev/null; then
    echo "  wsl-chrome-mcp command is available"
else
    echo "  WARNING: wsl-chrome-mcp not found on PATH. You may need to add ~/.local/bin to PATH."
fi

# Step 2: Install the opencode plugin
echo ""
echo "[2/2] Installing opencode plugin..."
mkdir -p "${PLUGIN_DIR}"
cp "${PLUGIN_SRC}" "${PLUGIN_DIR}/chrome-session.ts"
echo "  Copied to ${PLUGIN_DIR}/chrome-session.ts"

# Done
echo ""
echo "=== Installation complete ==="
echo ""
echo "Add this to your opencode.json (project or ~/.config/opencode/opencode.json):"
echo ""
echo '  {'
echo '    "mcp": {'
echo '      "wsl-chrome-mcp": {'
echo '        "type": "local",'
echo '        "command": ["wsl-chrome-mcp"],'
echo '        "enabled": true'
echo '      }'
echo '    }'
echo '  }'
echo ""
echo "The plugin auto-injects session IDs - each chat gets its own Chrome window."
