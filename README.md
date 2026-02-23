# WSL Chrome MCP

Chrome DevTools Protocol (CDP) server for the [Model Context Protocol](https://modelcontextprotocol.io/), designed for WSL. Each MCP session gets its own isolated Chrome instance on Windows — no tab conflicts, no data leaks between sessions.

## Key Features

- **Per-session Chrome isolation** — Each MCP client session launches its own Chrome process with a temporary profile. Sessions can't interfere with each other or your personal browser.
- **Profile mode** — Optionally share a single Chrome instance across sessions using window-scoped tab isolation, preserving your logged-in state and bookmarks.
- **33 browser automation tools** — Navigation, input, screenshots, accessibility snapshots, JavaScript execution, network monitoring, performance tracing, device emulation.
- **Persistent CDP connection** — Direct WebSocket connection with automatic retry (3 attempts), PowerShell relay fallback, and HTTP proxy as last resort.
- **TUI configuration dashboard** — Interactive terminal UI (`wsl-chrome-mcp config`) for managing all settings.
- **WSL2 mirrored networking** — Native support for WSL2's mirrored networking mode (localhost access to Windows).
- **Always-on CDP** — Optional mode to keep Chrome's debugging port enabled permanently by injecting `--remote-debugging-port` into Windows shortcuts and registry protocol handlers.

## Installation

```bash
# Clone the repository
git clone https://github.com/477174/wsl-chrome-mcp.git
cd wsl-chrome-mcp

# Install with uv (recommended)
uv pip install -e .
```

### Add to your MCP client

**Claude Code:**

```bash
claude mcp add wsl-chrome-mcp -- uv run wsl-chrome-mcp
```

**Manual configuration** (`~/.config/claude-code/mcp.json` or equivalent):

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

## Available Tools (33)

### Session Management (3)

| Tool | Description |
|------|-------------|
| `chrome_session_start` | Start a Chrome session with optional URL |
| `chrome_session_list` | List all active sessions |
| `chrome_session_end` | End a session and clean up its Chrome instance |

### Navigation (7)

| Tool | Description |
|------|-------------|
| `navigate_page` | Navigate to a URL and wait for load |
| `list_pages` | List all open pages/tabs |
| `select_page` | Switch to a different page/tab |
| `new_page` | Open a new page/tab |
| `close_page` | Close a page/tab |
| `resize_page` | Resize the browser viewport |
| `handle_dialog` | Accept or dismiss JavaScript dialogs (alert, confirm, prompt) |

### Input (8)

| Tool | Description |
|------|-------------|
| `click` | Click an element by accessibility UID |
| `click_at` | Click at specific x,y coordinates |
| `fill` | Type text into an input field by UID |
| `fill_form` | Fill multiple form fields at once |
| `hover` | Hover over an element by UID |
| `drag` | Drag from one element to another |
| `press_key` | Press keyboard keys (Enter, Tab, shortcuts) |
| `upload_file` | Upload a file to a file input element |

### Snapshot & Wait (2)

| Tool | Description |
|------|-------------|
| `take_snapshot` | Capture accessibility tree as structured text with UIDs for element targeting |
| `wait_for` | Wait for an element matching text/role to appear |

### Screenshot & PDF (2)

| Tool | Description |
|------|-------------|
| `take_screenshot` | Take a screenshot (viewport, full page, or specific element) |
| `generate_pdf` | Generate a PDF of the current page |

### Script (3)

| Tool | Description |
|------|-------------|
| `evaluate` | Execute JavaScript expressions or functions in page context |
| `get_html` | Get HTML content of the page or a specific element |
| `scroll` | Scroll the page or a specific element |

### Monitoring (4)

| Tool | Description |
|------|-------------|
| `get_console` | Get console messages (log, warn, error) with filtering |
| `get_console_message` | Get a specific console message by index |
| `get_network` | Get network requests with filtering by URL, method, status |
| `get_network_request` | Get detailed info for a specific network request |

### Emulation (1)

| Tool | Description |
|------|-------------|
| `emulate` | Emulate devices, viewports, dark mode, geolocation, network throttling (Slow 3G, Fast 3G, offline), CPU throttling, and custom user agents |

### Performance (3)

| Tool | Description |
|------|-------------|
| `performance_start_trace` | Start a Chrome performance trace |
| `performance_stop_trace` | Stop trace and return recorded data |
| `performance_analyze_insight` | Analyze trace data for performance insights |

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                            WSL Linux                                 │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                      wsl-chrome-mcp                            │  │
│  │                                                                │  │
│  │  ┌──────────────┐    ┌──────────────────┐    ┌─────────────┐  │  │
│  │  │  MCP Server   │───>│  Pool Manager    │───>│ Persistent  │  │  │
│  │  │  (FastMCP)    │    │  (chrome_pool)   │    │ CDP Client  │  │  │
│  │  └──────────────┘    └──────────────────┘    └──────┬──────┘  │  │
│  │                                                      │         │  │
│  │  ┌──────────────┐    ┌──────────────────┐           │         │  │
│  │  │  33 Tools     │    │  Config Manager  │           │         │  │
│  │  │  (modular)    │    │  (TOML)          │           │         │  │
│  │  └──────────────┘    └──────────────────┘           │         │  │
│  └──────────────────────────────────────────────────────┼─────────┘  │
│                                                         │            │
│                          CDP over WebSocket              │            │
│                          (port 9222)                     │            │
└─────────────────────────────────────────────────────────┼────────────┘
                                                          │
┌─────────────────────────────────────────────────────────┼────────────┐
│                         Windows Host                     │            │
│                                                          │            │
│  Isolated Mode (default):          Profile Mode:         │            │
│  ┌──────────┐ ┌──────────┐        ┌──────────────────┐  │            │
│  │ Chrome 1  │ │ Chrome 2  │        │ Shared Chrome    │<─┘            │
│  │ (temp     │ │ (temp     │        │ (your profile)   │               │
│  │  profile) │ │  profile) │        │ window-scoped    │               │
│  └──────────┘ └──────────┘        └──────────────────┘               │
└──────────────────────────────────────────────────────────────────────┘
```

## Session Modes

### Isolated Mode (default)

Each MCP session launches a **separate Chrome process** with a temporary user data directory. Sessions are completely independent — different cookies, storage, and history. When the session ends, the temporary profile is deleted.

- No interference with your personal Chrome
- No interference between concurrent MCP sessions
- Clean state every time

```toml
[chrome]
profile_mode = "isolated"
```

### Profile Mode

All sessions share a **single Chrome instance** using your existing Chrome profile. Each session gets its own window and tracks only the tabs it created. Your logged-in sessions, bookmarks, and extensions are available.

- Preserves login state across sessions
- Access to your bookmarks and extensions
- Sessions isolated by window (not by process)

```toml
[chrome]
profile_mode = "profile"
profile_name = "Profile 1"
```

## Connection Strategy

The server uses a multi-layer connection strategy with automatic fallback:

1. **Direct WebSocket** — Connects directly to Chrome's CDP WebSocket endpoint (3 retries with 1s delay between attempts)
2. **PowerShell Relay** — If direct connection fails, uses PowerShell on Windows as a WebSocket relay
3. **HTTP Proxy** — Last resort, proxies CDP commands over HTTP

Connection status is reported in tool responses (`Connected: True/False`). Even with `Connected: False` (proxy mode), all tools remain functional.

## Configuration

Settings are stored in `~/.config/wsl-chrome-mcp/config.toml`:

```toml
[chrome]
debug_port = 9222         # CDP debugging port
headless = false          # Run Chrome headless
profile_mode = "isolated" # "isolated" or "profile"
profile_name = ""         # Chrome profile name (profile mode only)

[network]
mirrored_networking = true  # WSL2 mirrored networking mode

[cdp]
always_on = false         # Keep CDP enabled permanently via registry

[plugin]
installed = true          # Whether OpenCode plugin is installed
```

### Interactive Configuration

Run the TUI dashboard to configure all settings interactively:

```bash
wsl-chrome-mcp config
```

The TUI provides toggle switches, dropdown selectors, and profile detection with a real-time preview of your configuration.

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run tests (98 unit tests)
uv run pytest

# Run E2E concurrency test (requires Windows Chrome)
uv run pytest tests/test_isolated_concurrency.py -v

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
   curl http://localhost:9222/json/version
   ```

2. Windows Firewall may be blocking port 9222. Add an inbound rule.

3. If using WSL2 without mirrored networking, try the Windows host IP:
   ```bash
   curl http://$(cat /etc/resolv.conf | grep nameserver | awk '{print $2}'):9222/json/version
   ```

### Connected: False (proxy fallback)

This means the direct WebSocket connection failed but the HTTP proxy is working. All tools still function normally. To restore direct connections:

1. Ensure Chrome is running with `--remote-debugging-port=9222`
2. Check that no other process is using port 9222
3. Restart the MCP server to retry the connection

### WSL1 vs WSL2

This MCP is optimized for WSL2. For WSL1, you may need to use `localhost` instead of the Windows host IP:

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
- [Model Context Protocol](https://modelcontextprotocol.io/)
