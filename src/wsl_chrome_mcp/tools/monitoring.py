"""Monitoring tools for Chrome MCP.

Includes: get_console, get_network
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.types import TextContent

from .base import ContentResult, ToolCategory, ToolContext, ToolDefinition, register_tool

logger = logging.getLogger(__name__)


# --- get_console ---
async def _get_console_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Get console messages from the browser."""
    clear = args.get("clear", False)
    types = args.get("types")  # Optional filter: ["log", "warn", "error", etc.]
    limit = args.get("limit", 100)
    offset = args.get("offset", 0)

    messages = list(ctx.instance.console_messages)

    # Filter by type if specified
    if types:
        messages = [m for m in messages if m.type in types]

    # Apply pagination
    total = len(messages)
    messages = messages[offset : offset + limit]

    if clear:
        ctx.instance.console_messages.clear()

    if not messages:
        return [TextContent(type="text", text="No console messages collected.")]

    lines = [f"Console messages ({len(messages)} of {total}):"]
    for i, msg in enumerate(messages):
        idx = offset + i
        lines.append(f"  [{idx}] [{msg.type.upper()}] {msg.text}")

    return [TextContent(type="text", text="\n".join(lines))]


get_console = register_tool(
    ToolDefinition(
        name="get_console",
        description="Get console messages (logs, warnings, errors) from the browser.",
        category=ToolCategory.MONITORING,
        read_only=True,
        schema={
            "clear": {
                "type": "boolean",
                "description": "Clear messages after returning (default: false)",
                "default": False,
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by message types: log, warn, error, info, debug",
            },
            "limit": {
                "type": "number",
                "description": "Maximum number of messages to return (default: 100)",
                "default": 100,
            },
            "offset": {
                "type": "number",
                "description": "Offset for pagination (default: 0)",
                "default": 0,
            },
        },
        handler=_get_console_handler,
    )
)


# --- get_network ---
async def _get_network_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Get network requests made by the page."""
    clear = args.get("clear", False)
    resource_types = args.get("resourceTypes")  # Optional filter
    limit = args.get("limit", 100)
    offset = args.get("offset", 0)

    requests = list(ctx.instance.network_requests.values())

    # Filter by resource type if specified
    if resource_types:
        requests = [r for r in requests if r.type in resource_types]

    # Apply pagination
    total = len(requests)
    requests = requests[offset : offset + limit]

    if clear:
        ctx.instance.network_requests.clear()

    if not requests:
        return [TextContent(type="text", text="No network requests collected.")]

    lines = [f"Network requests ({len(requests)} of {total}):"]
    for i, req in enumerate(requests):
        idx = offset + i
        status = ""
        if req.response:
            status = f" -> {req.response.get('status', '?')}"
        lines.append(f"  [{idx}] {req.method} {req.url[:80]}{status}")
        if req.type:
            lines.append(f"        type: {req.type}")

    return [TextContent(type="text", text="\n".join(lines))]


get_network = register_tool(
    ToolDefinition(
        name="get_network",
        description="Get network requests made by the page.",
        category=ToolCategory.MONITORING,
        read_only=True,
        schema={
            "clear": {
                "type": "boolean",
                "description": "Clear requests after returning (default: false)",
                "default": False,
            },
            "resourceTypes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by types: Document, Stylesheet, Image, Script, XHR, Fetch",
            },
            "limit": {
                "type": "number",
                "description": "Maximum number of requests to return (default: 100)",
                "default": 100,
            },
            "offset": {
                "type": "number",
                "description": "Offset for pagination (default: 0)",
                "default": 0,
            },
        },
        handler=_get_network_handler,
    )
)


# --- get_console_message ---
async def _get_console_message_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Get a specific console message by its index."""
    msgid = args.get("msgid")
    if msgid is None:
        return [TextContent(type="text", text="Error: msgid is required")]

    messages = ctx.instance.console_messages
    if msgid < 0 or msgid >= len(messages):
        return [
            TextContent(
                type="text",
                text=f"Error: msgid {msgid} out of range (0-{len(messages) - 1})",
            )
        ]

    msg = messages[msgid]
    lines = [
        f"Console message [{msgid}]:",
        f"  Type: {msg.type}",
        f"  Text: {msg.text}",
    ]
    if msg.timestamp:
        lines.append(f"  Timestamp: {msg.timestamp}")
    if msg.stack_trace:
        lines.append("  Stack trace:")
        for frame in msg.stack_trace:
            url = frame.get("url", "")
            line_no = frame.get("lineNumber", "?")
            col = frame.get("columnNumber", "?")
            fn = frame.get("functionName", "(anonymous)")
            lines.append(f"    {fn} at {url}:{line_no}:{col}")
    if msg.args:
        lines.append(f"  Args: {msg.args}")

    return [TextContent(type="text", text="\n".join(lines))]


get_console_message = register_tool(
    ToolDefinition(
        name="get_console_message",
        description="Get a console message by its ID.",
        category=ToolCategory.MONITORING,
        read_only=True,
        required=["msgid"],
        schema={
            "msgid": {
                "type": "number",
                "description": "The msgid of a console message from get_console.",
            },
        },
        handler=_get_console_message_handler,
    )
)


# --- get_network_request ---
async def _get_network_request_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Get detailed info about a specific network request."""
    reqid = args.get("reqid")
    if reqid is None:
        return [TextContent(type="text", text="Error: reqid is required")]

    reqid_str = str(reqid)
    req = ctx.instance.network_requests.get(reqid_str)
    if not req:
        return [TextContent(type="text", text=f"Error: request {reqid} not found")]

    lines = [
        f"Network request [{reqid}]:",
        f"  URL: {req.url}",
        f"  Method: {req.method}",
    ]
    if req.type:
        lines.append(f"  Type: {req.type}")
    if req.headers:
        lines.append("  Request headers:")
        for key, val in req.headers.items():
            lines.append(f"    {key}: {val}")
    if req.response:
        lines.append(f"  Status: {req.response.get('status', '?')}")
        resp_headers = req.response.get("headers", {})
        if resp_headers:
            lines.append("  Response headers:")
            for key, val in resp_headers.items():
                lines.append(f"    {key}: {val}")

    # Try to get response body via CDP
    try:
        body_result = await ctx.send_cdp("Network.getResponseBody", {"requestId": reqid_str})
        body = body_result.get("body", "")
        is_base64 = body_result.get("base64Encoded", False)
        if is_base64:
            lines.append(f"  Response body: [base64, {len(body)} chars]")
        elif body:
            # Truncate large bodies
            if len(body) > 2000:
                lines.append(f"  Response body ({len(body)} chars, truncated):")
                lines.append(f"    {body[:2000]}...")
            else:
                lines.append("  Response body:")
                lines.append(f"    {body}")
    except Exception:
        pass  # Network.getResponseBody may fail for some requests

    return [TextContent(type="text", text="\n".join(lines))]


get_network_request = register_tool(
    ToolDefinition(
        name="get_network_request",
        description="Get detailed info about a specific network request.",
        category=ToolCategory.MONITORING,
        read_only=True,
        required=["reqid"],
        schema={
            "reqid": {
                "type": "string",
                "description": "The request ID from get_network.",
            },
        },
        handler=_get_network_request_handler,
    )
)
