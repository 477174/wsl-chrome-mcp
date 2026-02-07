"""Input tools for Chrome MCP.

Includes: click, fill, hover, drag, press_key, upload_file, fill_form

These tools use UID-based element targeting from accessibility snapshots.
UIDs come from take_snapshot and map to backendNodeId for CDP interactions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.types import TextContent

from .base import (
    INCLUDE_SNAPSHOT_SCHEMA,
    UID_SCHEMA,
    ContentResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    register_tool,
)

logger = logging.getLogger(__name__)


class ElementNotFoundError(Exception):
    """Raised when an element cannot be found by UID."""

    pass


async def get_element_info(ctx: ToolContext, uid: str) -> dict[str, Any]:
    """Get element info by UID from snapshot cache.

    Args:
        ctx: Tool context
        uid: Element UID from take_snapshot

    Returns:
        Element info dict with backendNodeId, role, name, etc.

    Raises:
        ElementNotFoundError: If element not found in snapshot cache
    """
    cache = ctx.instance.snapshot_cache
    if uid in cache:
        return cache[uid]

    raise ElementNotFoundError(
        f"Element with uid={uid} not found. Use take_snapshot first to get element UIDs."
    )


async def get_element_center(ctx: ToolContext, backend_node_id: int) -> tuple[float, float]:
    """Get center coordinates of element by backendNodeId.

    Returns:
        (x, y) coordinates of element center

    Raises:
        RuntimeError: If unable to get element box model
    """
    try:
        box = await ctx.send_cdp("DOM.getBoxModel", {"backendNodeId": backend_node_id})
        content = box.get("model", {}).get("content", [])
        if len(content) >= 6:
            # content is [x1, y1, x2, y2, x3, y3, x4, y4] - corners
            x = (content[0] + content[2]) / 2
            y = (content[1] + content[5]) / 2
            return x, y
    except Exception as e:
        raise RuntimeError(f"Cannot get element position: {e}") from e

    raise RuntimeError("Element has no valid bounding box")


async def scroll_element_into_view(ctx: ToolContext, backend_node_id: int) -> None:
    """Scroll element into view using CDP."""
    try:
        result = await ctx.send_cdp("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = result.get("object", {}).get("objectId")
        if object_id:
            await ctx.send_cdp(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """
                        function() {
                            this.scrollIntoViewIfNeeded();
                        }
                    """,
                },
            )
    except Exception as e:
        logger.debug("scrollIntoView failed: %s", e)


async def click_element(ctx: ToolContext, uid: str, double_click: bool = False) -> tuple[bool, str]:
    """Click element by UID using CDP.

    Args:
        ctx: Tool context
        uid: Element UID from take_snapshot
        double_click: If True, perform double click

    Returns:
        (success, message) tuple
    """
    try:
        element = await get_element_info(ctx, uid)
    except ElementNotFoundError as e:
        return False, str(e)

    backend_node_id = element.get("backendNodeId")
    if not backend_node_id:
        return False, f"Element uid={uid} has no backendNodeId for CDP interaction"

    try:
        # Scroll element into view
        await scroll_element_into_view(ctx, backend_node_id)
        await asyncio.sleep(0.1)  # Brief pause for scroll

        # Get click coordinates
        x, y = await get_element_center(ctx, backend_node_id)

        click_count = 2 if double_click else 1

        # Perform click using CDP Input domain
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": click_count,
            },
        )
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": click_count,
            },
        )

        action = "Double clicked" if double_click else "Clicked"
        role = element.get("role", "element")
        name = element.get("name", "")
        desc = f' "{name}"' if name else ""
        return True, f"Successfully {action.lower()} on {role}{desc}"

    except Exception as e:
        logger.warning("CDP click failed for uid=%s: %s", uid, e)
        return False, f"Click failed: {e}"


async def fill_element(
    ctx: ToolContext, uid: str, value: str, clear_first: bool = True
) -> tuple[bool, str]:
    """Fill text into element by UID using CDP.

    Args:
        ctx: Tool context
        uid: Element UID from take_snapshot
        value: Text value to fill
        clear_first: Whether to clear existing value first

    Returns:
        (success, message) tuple
    """
    try:
        element = await get_element_info(ctx, uid)
    except ElementNotFoundError as e:
        return False, str(e)

    backend_node_id = element.get("backendNodeId")
    if not backend_node_id:
        return False, f"Element uid={uid} has no backendNodeId for CDP interaction"

    try:
        # Focus the element
        await ctx.send_cdp("DOM.focus", {"backendNodeId": backend_node_id})

        # Get objectId for the element
        result = await ctx.send_cdp("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = result.get("object", {}).get("objectId")

        if object_id:
            # Use JavaScript to set the value (most reliable cross-element)
            clear_js = "this.value = '';" if clear_first else ""
            await ctx.send_cdp(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": f"""
                        function(newValue) {{
                            {clear_js}
                            this.value = newValue;
                            this.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            this.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    """,
                    "arguments": [{"value": value}],
                },
            )

        return True, f"Successfully filled element uid={uid}"

    except Exception as e:
        logger.warning("CDP fill failed for uid=%s: %s", uid, e)
        return False, f"Fill failed: {e}"


async def hover_element(ctx: ToolContext, uid: str) -> tuple[bool, str]:
    """Hover over element by UID using CDP.

    Args:
        ctx: Tool context
        uid: Element UID from take_snapshot

    Returns:
        (success, message) tuple
    """
    try:
        element = await get_element_info(ctx, uid)
    except ElementNotFoundError as e:
        return False, str(e)

    backend_node_id = element.get("backendNodeId")
    if not backend_node_id:
        return False, f"Element uid={uid} has no backendNodeId for CDP interaction"

    try:
        await scroll_element_into_view(ctx, backend_node_id)
        await asyncio.sleep(0.1)

        x, y = await get_element_center(ctx, backend_node_id)

        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
        )

        return True, f"Successfully hovered over element uid={uid}"

    except Exception as e:
        logger.warning("CDP hover failed for uid=%s: %s", uid, e)
        return False, f"Hover failed: {e}"


# --- Tool handlers ---


async def _click_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Click on an element by UID."""
    uid = args.get("uid") or args.get("selector")  # Legacy support

    if not uid:
        return [TextContent(type="text", text="Error: uid is required")]

    dbl_click = args.get("dblClick", False)
    success, message = await click_element(ctx, uid, double_click=dbl_click)

    return [TextContent(type="text", text=message if success else f"Error: {message}")]


click = register_tool(
    ToolDefinition(
        name="click",
        description="Click on an element. Use UID from take_snapshot or CSS selector.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["uid"],
        schema={
            **UID_SCHEMA,
            "dblClick": {
                "type": "boolean",
                "description": "Set to true for double clicks. Default is false.",
                "default": False,
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
            "selector": {
                "type": "string",
                "description": "(Deprecated) CSS selector. Use 'uid' instead.",
            },
        },
        handler=_click_handler,
    )
)


async def _fill_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Fill text into an input element."""
    uid = args.get("uid") or args.get("selector")
    value = args.get("value") or args.get("text", "")

    if not uid:
        return [TextContent(type="text", text="Error: uid is required")]

    clear_first = args.get("clear_first", True)
    success, message = await fill_element(ctx, uid, value, clear_first=clear_first)

    return [TextContent(type="text", text=message if success else f"Error: {message}")]


fill = register_tool(
    ToolDefinition(
        name="fill",
        description="Type text into an input, text area, or select from a <select>.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["uid", "value"],
        schema={
            **UID_SCHEMA,
            "value": {
                "type": "string",
                "description": "The value to fill in",
            },
            "clear_first": {
                "type": "boolean",
                "description": "Clear the input before filling. Default is true.",
                "default": True,
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
            "selector": {
                "type": "string",
                "description": "(Deprecated) CSS selector. Use 'uid' instead.",
            },
            "text": {
                "type": "string",
                "description": "(Deprecated) Text to type. Use 'value' instead.",
            },
        },
        handler=_fill_handler,
    )
)


async def _hover_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Hover over an element."""
    uid = args.get("uid")
    if not uid:
        return [TextContent(type="text", text="Error: uid is required")]

    success, message = await hover_element(ctx, uid)
    return [TextContent(type="text", text=message if success else f"Error: {message}")]


hover = register_tool(
    ToolDefinition(
        name="hover",
        description="Hover over an element.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["uid"],
        schema={
            **UID_SCHEMA,
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_hover_handler,
    )
)


async def _press_key_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Press a key or key combination."""
    key = args.get("key", "")
    if not key:
        return [TextContent(type="text", text="Error: key is required")]

    # Parse key combination (e.g., "Control+A", "Enter", "Shift+Tab")
    parts = key.split("+")
    main_key = parts[-1]
    modifiers = parts[:-1] if len(parts) > 1 else []

    try:
        # Press modifier keys
        for modifier in modifiers:
            await ctx.send_cdp(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": modifier},
            )

        # Press main key
        await ctx.send_cdp(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": main_key},
        )
        await ctx.send_cdp(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": main_key},
        )

        # Release modifier keys (reverse order)
        for modifier in reversed(modifiers):
            await ctx.send_cdp(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": modifier},
            )

        return [TextContent(type="text", text=f"Successfully pressed key: {key}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error pressing key: {e}")]


press_key = register_tool(
    ToolDefinition(
        name="press_key",
        description="Press a key or key combination (e.g., 'Enter', 'Control+A').",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["key"],
        schema={
            "key": {
                "type": "string",
                "description": "A key or combo (e.g., 'Enter', 'Control+A', 'Shift+Tab')",
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_press_key_handler,
    )
)


# --- drag ---
async def _drag_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Drag an element onto another element."""
    from_uid = args.get("from_uid")
    to_uid = args.get("to_uid")

    if not from_uid or not to_uid:
        return [TextContent(type="text", text="Error: from_uid and to_uid are required")]

    try:
        from_element = await get_element_info(ctx, from_uid)
        to_element = await get_element_info(ctx, to_uid)
    except ElementNotFoundError as e:
        return [TextContent(type="text", text=f"Error: {e}")]

    from_backend = from_element.get("backendNodeId")
    to_backend = to_element.get("backendNodeId")
    if not from_backend or not to_backend:
        return [TextContent(type="text", text="Error: elements have no backendNodeId")]

    try:
        await scroll_element_into_view(ctx, from_backend)
        await asyncio.sleep(0.1)

        from_x, from_y = await get_element_center(ctx, from_backend)
        to_x, to_y = await get_element_center(ctx, to_backend)

        # Mouse down at source
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": from_x, "y": from_y, "button": "left", "clickCount": 1},
        )
        await asyncio.sleep(0.05)

        # Move to destination
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": to_x, "y": to_y},
        )
        await asyncio.sleep(0.05)

        # Mouse up at destination
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": to_x, "y": to_y, "button": "left"},
        )

        return [TextContent(type="text", text=f"Dragged {from_uid} onto {to_uid}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error dragging: {e}")]


drag = register_tool(
    ToolDefinition(
        name="drag",
        description="Drag an element onto another element.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["from_uid", "to_uid"],
        schema={
            "from_uid": {
                "type": "string",
                "description": "The uid of the element to drag",
            },
            "to_uid": {
                "type": "string",
                "description": "The uid of the element to drop into",
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_drag_handler,
    )
)


# --- fill_form ---
async def _fill_form_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Fill out multiple form elements at once."""
    elements = args.get("elements", [])

    if not elements:
        return [TextContent(type="text", text="Error: elements array is required")]

    results: list[str] = []
    for item in elements:
        uid = item.get("uid")
        value = item.get("value", "")
        if not uid:
            results.append("Skipped: missing uid")
            continue

        success, message = await fill_element(ctx, uid, value)
        results.append(f"uid={uid}: {'OK' if success else message}")

    return [
        TextContent(type="text", text=f"Filled {len(elements)} elements:\n" + "\n".join(results))
    ]


fill_form = register_tool(
    ToolDefinition(
        name="fill_form",
        description="Fill out multiple form elements at once.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["elements"],
        schema={
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["uid", "value"],
                },
                "description": "Elements from snapshot to fill out.",
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_fill_form_handler,
    )
)


# --- upload_file ---
async def _upload_file_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Upload a file through a file input element."""
    uid = args.get("uid")
    file_path = args.get("filePath")

    if not uid or not file_path:
        return [TextContent(type="text", text="Error: uid and filePath are required")]

    try:
        element = await get_element_info(ctx, uid)
    except ElementNotFoundError as e:
        return [TextContent(type="text", text=f"Error: {e}")]

    backend_node_id = element.get("backendNodeId")
    if not backend_node_id:
        return [TextContent(type="text", text=f"Error: uid={uid} has no backendNodeId")]

    try:
        # Use DOM.setFileInputFiles CDP command
        await ctx.send_cdp(
            "DOM.setFileInputFiles",
            {"files": [file_path], "backendNodeId": backend_node_id},
        )
        return [TextContent(type="text", text=f"Uploaded {file_path} to element uid={uid}")]

    except Exception as e:
        # Fallback: click the element and try to use Page.handleFileChooser
        logger.warning("Direct upload failed for uid=%s: %s, trying click fallback", uid, e)
        try:
            await ctx.send_cdp("Page.setInterceptFileChooserDialog", {"enabled": True})
            await click_element(ctx, uid)
            await asyncio.sleep(0.5)
            await ctx.send_cdp(
                "Page.handleFileChooser",
                {"action": "accept", "files": [file_path]},
            )
            await ctx.send_cdp("Page.setInterceptFileChooserDialog", {"enabled": False})
            return [TextContent(type="text", text=f"Uploaded {file_path} via file chooser")]
        except Exception as e2:
            return [TextContent(type="text", text=f"Error uploading file: {e2}")]


upload_file = register_tool(
    ToolDefinition(
        name="upload_file",
        description="Upload a file through a provided element.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["uid", "filePath"],
        schema={
            **UID_SCHEMA,
            "filePath": {
                "type": "string",
                "description": "The local path of the file to upload",
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_upload_file_handler,
    )
)


# --- click_at ---
async def _click_at_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Click at specific coordinates."""
    x = args.get("x")
    y = args.get("y")

    if x is None or y is None:
        return [TextContent(type="text", text="Error: x and y are required")]

    dbl_click = args.get("dblClick", False)
    click_count = 2 if dbl_click else 1

    try:
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": click_count},
        )
        await ctx.send_cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": click_count},
        )

        action = "Double clicked" if dbl_click else "Clicked"
        return [TextContent(type="text", text=f"{action} at ({x}, {y})")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error clicking at coordinates: {e}")]


click_at = register_tool(
    ToolDefinition(
        name="click_at",
        description="Click at the provided coordinates.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["x", "y"],
        schema={
            "x": {
                "type": "number",
                "description": "The x coordinate",
            },
            "y": {
                "type": "number",
                "description": "The y coordinate",
            },
            "dblClick": {
                "type": "boolean",
                "description": "Set to true for double clicks. Default is false.",
                "default": False,
            },
            **INCLUDE_SNAPSHOT_SCHEMA,
        },
        handler=_click_at_handler,
    )
)
