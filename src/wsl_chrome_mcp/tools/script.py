"""Script tools for Chrome MCP.

Includes: evaluate, get_html
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.types import TextContent

from .base import ContentResult, ToolCategory, ToolContext, ToolDefinition, register_tool

logger = logging.getLogger(__name__)


# --- evaluate ---
async def _evaluate_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Evaluate JavaScript in the page context.

    Supports two modes:
    1. expression mode: evaluate a JS expression directly
    2. function mode: call a JS function with optional element handle args
    """
    expression = args.get("expression", "")
    function = args.get("function", "")
    fn_args = args.get("args", [])

    if not expression and not function:
        return [TextContent(type="text", text="Error: expression or function is required")]

    try:
        if function and fn_args:
            # Resolve element UIDs to objectIds
            object_ids = []
            for arg in fn_args:
                uid = arg.get("uid") if isinstance(arg, dict) else None
                if not uid:
                    continue
                cache = ctx.instance.snapshot_cache
                element = cache.get(uid)
                if not element or not element.get("backendNodeId"):
                    return [
                        TextContent(type="text", text=f"Error: uid={uid} not found in snapshot")
                    ]
                resolved = await ctx.send_cdp(
                    "DOM.resolveNode", {"backendNodeId": element["backendNodeId"]}
                )
                oid = resolved.get("object", {}).get("objectId")
                if oid:
                    object_ids.append({"objectId": oid})

            # Evaluate the function with resolved element handles
            result = await ctx.send_cdp(
                "Runtime.evaluate",
                {
                    "expression": f"({function})",
                    "returnByValue": False,
                    "awaitPromise": True,
                },
            )
            fn_object_id = result.get("result", {}).get("objectId")
            if fn_object_id:
                call_result = await ctx.send_cdp(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": fn_object_id,
                        "functionDeclaration": "function(...args) { return this(...args); }",
                        "arguments": object_ids,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                value = call_result.get("result", {}).get("value")
                return [TextContent(type="text", text=json.dumps(value, indent=2, default=str))]

            return [TextContent(type="text", text="Error: could not create function handle")]

        elif function:
            # Function without args — just evaluate it
            result = await ctx.evaluate_js(f"({function})()")
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        else:
            # Standard expression mode
            result = await ctx.evaluate_js(expression)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


evaluate = register_tool(
    ToolDefinition(
        name="evaluate",
        description=(
            "Execute JavaScript in the page context and return the result. "
            "Supports expression or function with element handle args."
        ),
        category=ToolCategory.SCRIPT,
        read_only=False,
        schema={
            "expression": {
                "type": "string",
                "description": "JavaScript expression to evaluate",
            },
            "function": {
                "type": "string",
                "description": "A JS function declaration, e.g. '() => document.title'",
            },
            "args": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"uid": {"type": "string"}},
                },
                "description": "Element handles to pass as function arguments.",
            },
        },
        handler=_evaluate_handler,
    )
)


# --- get_html ---
async def _get_html_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Get HTML content of the page or a specific element."""
    selector = args.get("selector")

    if selector:
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return {{ error: 'Element not found: {selector}' }};
            return {{ html: el.outerHTML }};
        }})()
        """
        result = await ctx.evaluate_js(js)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]
        html = result.get("html", "") if isinstance(result, dict) else ""
    else:
        await ctx.send_cdp("DOM.enable")
        doc = await ctx.send_cdp("DOM.getDocument", {"depth": -1})
        root_id = doc["root"]["nodeId"]
        result = await ctx.send_cdp("DOM.getOuterHTML", {"nodeId": root_id})
        html = result["outerHTML"]

    return [TextContent(type="text", text=html)]


get_html = register_tool(
    ToolDefinition(
        name="get_html",
        description="Get the HTML content of the current page or a specific element.",
        category=ToolCategory.SCRIPT,
        read_only=True,
        schema={
            "selector": {
                "type": "string",
                "description": "CSS selector for element (optional, full page if omitted)",
            },
        },
        handler=_get_html_handler,
    )
)


# --- scroll ---
async def _scroll_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Scroll the page or an element."""
    direction = args.get("direction", "down")
    amount = args.get("amount", 500)
    selector = args.get("selector")

    target = f"document.querySelector({json.dumps(selector)})" if selector else "window"

    scroll_code = {
        "up": f"{target}.scrollBy(0, -{amount})",
        "down": f"{target}.scrollBy(0, {amount})",
        "left": f"{target}.scrollBy(-{amount}, 0)",
        "right": f"{target}.scrollBy({amount}, 0)",
        "top": f"{target}.scrollTo(0, 0)",
        "bottom": f"{target}.scrollTo(0, document.body.scrollHeight)",
    }

    js = scroll_code.get(direction, f"{target}.scrollBy(0, {amount})")
    await ctx.evaluate_js(js)

    return [TextContent(type="text", text=f"Scrolled {direction}")]


scroll = register_tool(
    ToolDefinition(
        name="scroll",
        description="Scroll the page or an element.",
        category=ToolCategory.INPUT,
        read_only=False,
        required=["direction"],
        schema={
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right", "top", "bottom"],
                "description": "Direction to scroll",
            },
            "amount": {
                "type": "number",
                "description": "Pixels to scroll (default: 500)",
                "default": 500,
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for element to scroll (optional)",
            },
        },
        handler=_scroll_handler,
    )
)
