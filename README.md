# WSL Chrome MCP

Chrome DevTools MCP server that works seamlessly in WSL (Windows Subsystem for Linux) by automatically detecting and connecting to Windows Chrome.

## The Problem

When running Claude Code or other MCP clients in WSL, browser automation tools like [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) don't work out of the box because:

1. Chrome is installed on Windows, not in WSL
2. The MCP server runs in WSL and can't directly launch Windows Chrome
3. Network connectivity between WSL and Windows requires special handling

## The Solution

**wsl-chrome-mcp** automatically:

- Detects when running in WSL
- Finds Chrome on Windows
- Launches Chrome with remote debugging enabled
- Connects to Chrome via the Chrome DevTools Protocol (CDP)
- Provides the same tools as chrome-devtools-mcp

**Zero extra effort** - it just works like on native Ubuntu.

## Installation

### 1. Install the MCP server

```bash
# Clone the repository
git clone https://github.com/yourusername/wsl-chrome-mcp.git
cd wsl-chrome-mcp

# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

### 2. Add to Claude Code

```bash
claude mcp add wsl-chrome-mcp -- uv run wsl-chrome-mcp
```

Or manually add to your MCP configuration (`~/.config/claude-code/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "wsl-chrome-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/wsl-chrome-mcp", "wsl-chrome-mcp"]
    }
  }
}
```

### Alternative: Connect to existing Chrome

If you prefer to manage Chrome yourself:

1. Start Chrome on Windows with remote debugging:
   ```powershell
   # In PowerShell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
   ```

2. The MCP will automatically connect to it instead of launching a new instance.

## Available Tools

| Tool | Description |
|------|-------------|
| `chrome_navigate` | Navigate to a URL |
| `chrome_screenshot` | Take a screenshot (viewport or full page) |
| `chrome_click` | Click on an element by CSS selector |
| `chrome_type` | Type text into an input field |
| `chrome_get_html` | Get HTML content of page or element |
| `chrome_evaluate` | Execute JavaScript and get result |
| `chrome_console` | Get console messages (logs, errors, warnings) |
| `chrome_network` | Get network requests made by the page |
| `chrome_wait` | Wait for an element to appear |
| `chrome_scroll` | Scroll the page or element |
| `chrome_tabs` | List all open browser tabs |
| `chrome_new_tab` | Open a new tab |
| `chrome_close_tab` | Close a tab |
| `chrome_switch_tab` | Switch to a different tab |
| `chrome_pdf` | Generate a PDF of the current page |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CHROME_DEBUG_PORT` | `9222` | Remote debugging port |
| `CHROME_HEADLESS` | `false` | Run Chrome in headless mode |
| `CHROME_USER_DATA_DIR` | (temp) | Custom user data directory |

Example with custom config:

```json
{
  "mcpServers": {
    "wsl-chrome-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/wsl-chrome-mcp", "wsl-chrome-mcp"],
      "env": {
        "CHROME_DEBUG_PORT": "9223",
        "CHROME_HEADLESS": "true"
      }
    }
  }
}
```

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                           WSL Linux                             │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    wsl-chrome-mcp                           ││
│  │  ┌─────────────────────┐    ┌─────────────────────────────┐ ││
│  │  │   MCP Server        │────│  CDP Client                 │ ││
│  │  │   (Python)          │    │  (connects to Windows)      │ ││
│  │  └─────────────────────┘    └─────────────────────────────┘ ││
│  └─────────────────────────────────────────────────────────────┘│
│                               │                                 │
│                               │ CDP over TCP                    │
│                               │ (port 9222)                     │
└───────────────────────────────┼─────────────────────────────────┘
                                │
┌───────────────────────────────┼─────────────────────────────────┐
│                        Windows Host                              │
│                               │                                  │
│  ┌────────────────────────────▼────────────────────────────────┐│
│  │           Chrome Browser (with remote debugging)            ││
│  │           --remote-debugging-port=9222                      ││
│  └─────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

1. **WSL Detection**: Checks `/proc/version`, `/proc/sys/fs/binfmt_misc/WSLInterop`, and environment variables
2. **Windows Host IP**: Resolves from `/etc/resolv.conf` nameserver (WSL2) or environment variables
3. **Chrome Launch**: Uses `powershell.exe` from WSL to start Chrome on Windows
4. **CDP Connection**: Connects to Chrome's debugging port over the WSL-Windows network bridge

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Lint and format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy src
```

## Troubleshooting

### Chrome not found

Make sure Chrome is installed on Windows in one of these locations:
- `C:\Program Files\Google\Chrome\Application\chrome.exe`
- `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`
- `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`

### Connection refused

1. Check if Chrome is running with remote debugging:
   ```bash
   curl http://$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):9222/json/version
   ```

2. Windows Firewall might be blocking the connection. Add an inbound rule for port 9222.

3. Try starting Chrome manually first:
   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
   ```

### WSL1 vs WSL2

This MCP is optimized for WSL2. For WSL1, you may need to use `localhost` instead of the Windows host IP. Set the environment variable:

```bash
export WSL_HOST_IP=127.0.0.1
```

## License

MIT

## Credits

Inspired by [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) from the Chrome DevTools team.

## Sources

- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)
- [Chrome DevTools Protocol Documentation](https://chromedevtools.github.io/devtools-protocol/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
