"""CLI entry point for wsl-chrome-mcp.

Dispatches to MCP server (default) or config TUI (wsl-chrome-mcp config).
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .tui.app import run_config_tui

        run_config_tui()
    elif len(sys.argv) > 1 and sys.argv[1] == "--version":
        print("wsl-chrome-mcp 0.1.0")
    else:
        import asyncio

        from dotenv import load_dotenv

        from .server import ChromeMCPServer

        load_dotenv()
        server = ChromeMCPServer()
        asyncio.run(server.run())


if __name__ == "__main__":
    main()
