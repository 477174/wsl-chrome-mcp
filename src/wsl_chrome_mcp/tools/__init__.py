"""Chrome MCP Tools - Modular tool registration system.

This package provides a modular tool system matching ChromeDevTools/chrome-devtools-mcp.
Tools are organized by category and registered through a central registry.

Tool naming follows ChromeDevTools conventions:
- navigate_page (not chrome_navigate)
- take_snapshot (not chrome_snapshot)
- click (not chrome_click)
"""

from .base import (
    ContentResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    get_all_tools,
    get_tool,
)
from .emulation import emulate
from .input import click, click_at, drag, fill, fill_form, hover, press_key, upload_file
from .monitoring import get_console, get_console_message, get_network, get_network_request
from .navigation import (
    close_page,
    handle_dialog,
    list_pages,
    navigate_page,
    new_page,
    resize_page,
    select_page,
)
from .performance import (
    performance_analyze_insight,
    performance_start_trace,
    performance_stop_trace,
)
from .screenshot import generate_pdf, take_screenshot
from .script import evaluate, get_html, scroll
from .session import chrome_session_end, chrome_session_list, chrome_session_start
from .snapshot import take_snapshot, wait_for

__all__ = [
    # Base
    "ToolDefinition",
    "ToolCategory",
    "ToolContext",
    "ContentResult",
    "get_all_tools",
    "get_tool",
    # Navigation
    "navigate_page",
    "list_pages",
    "select_page",
    "new_page",
    "close_page",
    "resize_page",
    "handle_dialog",
    # Input
    "click",
    "click_at",
    "fill",
    "fill_form",
    "hover",
    "drag",
    "press_key",
    "scroll",
    "upload_file",
    # Snapshot
    "take_snapshot",
    "wait_for",
    # Screenshot
    "take_screenshot",
    "generate_pdf",
    # Monitoring
    "get_console",
    "get_console_message",
    "get_network",
    "get_network_request",
    # Script
    "evaluate",
    "get_html",
    # Emulation
    "emulate",
    # Performance
    "performance_start_trace",
    "performance_stop_trace",
    "performance_analyze_insight",
    # Session
    "chrome_session_start",
    "chrome_session_list",
    "chrome_session_end",
]
