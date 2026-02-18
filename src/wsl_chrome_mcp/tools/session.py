"""Session management tools for Chrome MCP.

Includes: chrome_session_start, chrome_session_list, chrome_session_end

These tools manage Chrome session lifecycle and are handled specially
by the server since they need access before a session exists.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.types import TextContent

from .base import (
    ContentResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    register_tool,
)

logger = logging.getLogger(__name__)


async def _session_start_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Start a Chrome session."""
    url = args.get("url", "about:blank")

    # Navigate if URL provided
    if url != "about:blank":
        await ctx.send_cdp("Page.enable")
        await ctx.send_cdp("Page.navigate", {"url": url})

    return [
        TextContent(
            type="text",
            text=(
                f"Session started: {ctx.instance.session_id}\n"
                f"Port: {ctx.instance.port}\n"
                f"Tab: {ctx.instance.current_target_id}\n"
                f"Connected: {ctx.instance.is_connected}"
            ),
        )
    ]


chrome_session_start = register_tool(
    ToolDefinition(
        name="chrome_session_start",
        description="Start a Chrome session. Auto-created on first tool call.",
        category=ToolCategory.NAVIGATION,
        read_only=False,
        schema={
            "url": {
                "type": "string",
                "description": "URL to open (default: about:blank)",
                "default": "about:blank",
            },
        },
        handler=_session_start_handler,
    )
)


async def _session_list_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """List all active sessions."""
    sessions = ctx.pool.list_sessions()

    if not sessions:
        return [TextContent(type="text", text="No active sessions.")]

    lines = ["Active sessions:"]
    for sid, info in sessions.items():
        lines.append(
            f"  - {sid}: port={info['port']}, pid={info['pid']}, "
            f"tabs={info['tab_count']}, connected={info['connected']}"
        )

    return [TextContent(type="text", text="\n".join(lines))]


chrome_session_list = register_tool(
    ToolDefinition(
        name="chrome_session_list",
        description="List all active Chrome sessions.",
        category=ToolCategory.NAVIGATION,
        read_only=True,
        schema={},
        handler=_session_list_handler,
    )
)


async def _session_end_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """End a Chrome session.

    Note: This handler is intercepted by server.py which handles
    session destruction at the server level (since the instance must
    be destroyed, not used). This handler exists as the registered
    definition so the tool appears in list_tools.
    """
    # Server intercepts this tool before it reaches here.
    # If somehow called directly, attempt destruction anyway.
    session_id = ctx.instance.session_id
    try:
        await ctx.pool.destroy(session_id)
        return [TextContent(type="text", text=f"Session ended: {session_id}")]
    except KeyError:
        return [TextContent(type="text", text=f"Session not found: {session_id}")]


chrome_session_end = register_tool(
    ToolDefinition(
        name="chrome_session_end",
        description="End a session, closing its browser context.",
        category=ToolCategory.NAVIGATION,
        read_only=False,
        required=["session_id"],
        schema={},
        handler=_session_end_handler,
    )
)
