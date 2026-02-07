"""Navigation tools for Chrome MCP.

Includes: navigate_page, list_pages, select_page, new_page, close_page, resize_page, handle_dialog
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mcp.types import TextContent

from .base import (
    TIMEOUT_SCHEMA,
    ContentResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    register_tool,
)

logger = logging.getLogger(__name__)


# --- navigate_page ---
async def _wait_for_load(ctx: ToolContext, timeout_s: float) -> None:
    """Wait for page load event with timeout."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_poll_load_complete(ctx), timeout=timeout_s)


async def _poll_load_complete(ctx: ToolContext) -> None:
    """Poll until document.readyState is complete."""
    while True:
        state = await ctx.evaluate_js("document.readyState")
        if state == "complete":
            return
        await asyncio.sleep(0.3)


async def _navigate_page_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Navigate the page by URL, back, forward, or reload."""
    nav_type = args.get("type", "url")
    url = args.get("url")
    timeout_ms = args.get("timeout") or 10000
    timeout_s = timeout_ms / 1000
    init_script = args.get("initScript")
    handle_unload = args.get("handleBeforeUnload", "accept")

    if not nav_type and not url:
        return [TextContent(type="text", text="Error: URL or type is required")]
    if not nav_type:
        nav_type = "url"

    await ctx.send_cdp("Page.enable")

    # Install init script if provided
    script_id = None
    if init_script:
        result = await ctx.send_cdp(
            "Page.addScriptToEvaluateOnNewDocument", {"source": init_script}
        )
        script_id = result.get("identifier")

    # Handle beforeunload dialogs during navigation
    if handle_unload == "accept":
        # Auto-accept beforeunload via Page domain
        pass  # CDP Page.navigate auto-handles beforeunload

    try:
        if nav_type == "url":
            if not url:
                return [TextContent(type="text", text="Error: URL required for type=url")]
            try:
                result = await ctx.send_cdp("Page.navigate", {"url": url})
                await _wait_for_load(ctx, timeout_s)
                title = await ctx.evaluate_js("document.title")
                frame_id = result.get("frameId", "unknown")
                return [
                    TextContent(
                        type="text",
                        text=f"Navigated to {url}\nTitle: {title}\nFrame: {frame_id}",
                    )
                ]
            except Exception as e:
                return [TextContent(type="text", text=f"Navigation error: {e}")]

        elif nav_type == "back":
            history = await ctx.send_cdp("Page.getNavigationHistory")
            index = history.get("currentIndex", 0)
            if index > 0:
                entries = history.get("entries", [])
                await ctx.send_cdp(
                    "Page.navigateToHistoryEntry", {"entryId": entries[index - 1]["id"]}
                )
                await _wait_for_load(ctx, timeout_s)
                new_url = await ctx.evaluate_js("window.location.href")
                return [TextContent(type="text", text=f"Navigated back to {new_url}")]
            return [TextContent(type="text", text="Cannot go back: at start of history")]

        elif nav_type == "forward":
            history = await ctx.send_cdp("Page.getNavigationHistory")
            index = history.get("currentIndex", 0)
            entries = history.get("entries", [])
            if index < len(entries) - 1:
                await ctx.send_cdp(
                    "Page.navigateToHistoryEntry", {"entryId": entries[index + 1]["id"]}
                )
                await _wait_for_load(ctx, timeout_s)
                new_url = await ctx.evaluate_js("window.location.href")
                return [TextContent(type="text", text=f"Navigated forward to {new_url}")]
            return [TextContent(type="text", text="Cannot go forward: at end of history")]

        elif nav_type == "reload":
            ignore_cache = args.get("ignoreCache", False)
            await ctx.send_cdp("Page.reload", {"ignoreCache": ignore_cache})
            await _wait_for_load(ctx, timeout_s)
            return [TextContent(type="text", text="Successfully reloaded the page")]

        return [TextContent(type="text", text=f"Unknown navigation type: {nav_type}")]

    finally:
        # Clean up init script
        if script_id:
            with contextlib.suppress(Exception):
                await ctx.send_cdp(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": script_id},
                )


navigate_page = register_tool(
    ToolDefinition(
        name="navigate_page",
        description="Navigate the selected page by URL, back, forward, or reload.",
        category=ToolCategory.NAVIGATION,
        read_only=False,
        schema={
            "type": {
                "type": "string",
                "enum": ["url", "back", "forward", "reload"],
                "description": "Navigate by URL, back, forward, or reload.",
            },
            "url": {
                "type": "string",
                "description": "Target URL (only for type=url)",
            },
            "ignoreCache": {
                "type": "boolean",
                "description": "Whether to ignore cache on reload.",
                "default": False,
            },
            "handleBeforeUnload": {
                "type": "string",
                "enum": ["accept", "decline"],
                "description": "How to handle beforeunload dialogs. Default: accept.",
            },
            "initScript": {
                "type": "string",
                "description": "JS to execute on new document before page scripts.",
            },
            **TIMEOUT_SCHEMA,
        },
        handler=_navigate_page_handler,
    )
)


# --- list_pages ---
async def _list_pages_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """List all open pages."""
    tabs = await ctx.pool.list_tabs(ctx.instance.session_id)

    if not tabs:
        return [TextContent(type="text", text="No pages open.")]

    lines = [f"Open pages ({len(tabs)}):"]
    for i, tab in enumerate(tabs):
        current = " (selected)" if tab.get("is_current") else ""
        lines.append(
            f"  [{i}] {tab.get('title', 'Untitled')}: {tab.get('url', 'about:blank')}{current}"
        )
        lines.append(f"      id: {tab.get('id')}")

    return [TextContent(type="text", text="\n".join(lines))]


list_pages = register_tool(
    ToolDefinition(
        name="list_pages",
        description="Get a list of pages open in the browser.",
        category=ToolCategory.NAVIGATION,
        read_only=True,
        schema={},
        handler=_list_pages_handler,
    )
)


# --- select_page ---
async def _select_page_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Select a page as the context for future tool calls."""
    page_id = args.get("pageId")
    if page_id is None:
        page_id = args.get("tab_id")

    if page_id is None:
        return [TextContent(type="text", text="Error: pageId is required")]

    try:
        await ctx.pool.switch_tab(ctx.instance.session_id, str(page_id))

        # Bring to front if requested
        if args.get("bringToFront", False):
            await ctx.send_cdp("Page.bringToFront")

        return [TextContent(type="text", text=f"Switched to page: {page_id}")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]


select_page = register_tool(
    ToolDefinition(
        name="select_page",
        description="Select a page as the context for future tool calls.",
        category=ToolCategory.NAVIGATION,
        read_only=True,
        required=["pageId"],
        schema={
            "pageId": {
                "type": "string",
                "description": "The ID of the page to select. Call list_pages for IDs.",
            },
            "bringToFront": {
                "type": "boolean",
                "description": "Whether to focus the page and bring it to the top.",
                "default": False,
            },
        },
        handler=_select_page_handler,
    )
)


# --- new_page ---
async def _new_page_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Create a new page."""
    url = args.get("url", "about:blank")
    background = args.get("background", False)

    target_id = await ctx.pool.create_tab(ctx.instance.session_id, url)

    # If not background, switch to the new tab
    if not background:
        with contextlib.suppress(ValueError, KeyError):
            await ctx.pool.switch_tab(ctx.instance.session_id, target_id)

    return [TextContent(type="text", text=f"Created new page: {target_id}\nURL: {url}")]


new_page = register_tool(
    ToolDefinition(
        name="new_page",
        description="Create a new page in the browser.",
        category=ToolCategory.NAVIGATION,
        read_only=False,
        schema={
            "url": {
                "type": "string",
                "description": "URL to load in the new page.",
                "default": "about:blank",
            },
            "background": {
                "type": "boolean",
                "description": "Whether to open in background without bringing to front.",
                "default": False,
            },
            **TIMEOUT_SCHEMA,
        },
        handler=_new_page_handler,
    )
)


# --- close_page ---
async def _close_page_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Close a page."""
    page_id = args.get("pageId")
    if page_id is None:
        page_id = args.get("tab_id")

    if page_id is None:
        return [TextContent(type="text", text="Error: pageId is required")]

    try:
        await ctx.pool.close_tab(ctx.instance.session_id, str(page_id))
        return [TextContent(type="text", text=f"Closed page: {page_id}")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]


close_page = register_tool(
    ToolDefinition(
        name="close_page",
        description="Close a page. The last open page cannot be closed.",
        category=ToolCategory.NAVIGATION,
        read_only=False,
        required=["pageId"],
        schema={
            "pageId": {
                "type": "string",
                "description": "The ID of the page to close. Call list_pages to list pages.",
            },
        },
        handler=_close_page_handler,
    )
)


# --- resize_page ---
async def _resize_page_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Resize the page's viewport."""
    width = args.get("width", 1280)
    height = args.get("height", 720)

    await ctx.send_cdp(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )

    return [TextContent(type="text", text=f"Resized page to {width}x{height}")]


resize_page = register_tool(
    ToolDefinition(
        name="resize_page",
        description="Resize the selected page's viewport.",
        category=ToolCategory.EMULATION,
        read_only=False,
        required=["width", "height"],
        schema={
            "width": {
                "type": "number",
                "description": "Page width in pixels",
            },
            "height": {
                "type": "number",
                "description": "Page height in pixels",
            },
        },
        handler=_resize_page_handler,
    )
)


# --- handle_dialog ---
async def _handle_dialog_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Handle a browser dialog (alert/confirm/prompt)."""
    action = args.get("action", "accept")
    prompt_text = args.get("promptText")

    dialog = ctx.instance.pending_dialog
    if dialog is None:
        return [TextContent(type="text", text="No open dialog found")]

    try:
        if action == "accept":
            if prompt_text and dialog.type == "prompt":
                # For prompt dialogs, we need to use Page.handleJavaScriptDialog
                await ctx.send_cdp(
                    "Page.handleJavaScriptDialog",
                    {"accept": True, "promptText": prompt_text},
                )
            else:
                await ctx.send_cdp("Page.handleJavaScriptDialog", {"accept": True})
            result_msg = "Successfully accepted the dialog"
        else:
            await ctx.send_cdp("Page.handleJavaScriptDialog", {"accept": False})
            result_msg = "Successfully dismissed the dialog"

        # Clear the dialog
        ctx.instance.pending_dialog = None

        return [TextContent(type="text", text=result_msg)]

    except Exception as e:
        return [TextContent(type="text", text=f"Error handling dialog: {e}")]


handle_dialog = register_tool(
    ToolDefinition(
        name="handle_dialog",
        description="Handle a browser dialog (alert/confirm/prompt).",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["action"],
        schema={
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": "Whether to accept or dismiss the dialog",
            },
            "promptText": {
                "type": "string",
                "description": "Optional text to enter into a prompt dialog.",
            },
        },
        handler=_handle_dialog_handler,
    )
)
