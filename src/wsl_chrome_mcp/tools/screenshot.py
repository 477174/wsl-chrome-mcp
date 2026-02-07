"""Screenshot tools for Chrome MCP.

Includes: take_screenshot, generate_pdf
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent
from pydantic import AnyUrl

from .base import ContentResult, ToolCategory, ToolContext, ToolDefinition, register_tool

logger = logging.getLogger(__name__)


# --- take_screenshot ---
async def _take_screenshot_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Take a screenshot of the current page or a specific element."""
    full_page = args.get("fullPage", args.get("full_page", False))
    img_format = args.get("format", "png")
    quality = args.get("quality", 80)
    uid = args.get("uid")
    file_path = args.get("filePath")

    params: dict[str, Any] = {"format": img_format}

    if img_format == "jpeg":
        params["quality"] = quality

    if uid:
        # Element screenshot: get element bounds from snapshot cache
        cache = ctx.instance.snapshot_cache
        element = cache.get(uid)
        if not element:
            return [TextContent(type="text", text=f"Error: uid={uid} not found in snapshot")]
        backend_node_id = element.get("backendNodeId")
        if backend_node_id:
            try:
                box = await ctx.send_cdp("DOM.getBoxModel", {"backendNodeId": backend_node_id})
                content = box.get("model", {}).get("content", [])
                if len(content) >= 8:
                    x1, y1 = content[0], content[1]
                    x3, y3 = content[4], content[5]
                    params["clip"] = {
                        "x": x1,
                        "y": y1,
                        "width": x3 - x1,
                        "height": y3 - y1,
                        "scale": 1,
                    }
            except Exception as e:
                return [TextContent(type="text", text=f"Error getting element bounds: {e}")]
    elif full_page:
        layout = await ctx.send_cdp("Page.getLayoutMetrics")
        content_size = layout.get("contentSize", {})
        params["clip"] = {
            "x": 0,
            "y": 0,
            "width": content_size.get("width", 1920),
            "height": content_size.get("height", 1080),
            "scale": 1,
        }
        params["captureBeyondViewport"] = True

    result = await ctx.send_cdp("Page.captureScreenshot", params)
    image_data = result.get("data", "")

    # Save to file if filePath provided
    if file_path:
        try:
            raw_bytes = base64.b64decode(image_data)
            with open(file_path, "wb") as f:
                f.write(raw_bytes)
            return [TextContent(type="text", text=f"Screenshot saved to {file_path}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error saving screenshot: {e}")]

    return [
        ImageContent(
            type="image",
            data=image_data,
            mimeType=f"image/{img_format}",
        )
    ]


take_screenshot = register_tool(
    ToolDefinition(
        name="take_screenshot",
        description="Take a screenshot of the page or element. Returns the image.",
        category=ToolCategory.SCREENSHOT,
        read_only=False,
        schema={
            "fullPage": {
                "type": "boolean",
                "description": "Capture the full page (default: false)",
                "default": False,
            },
            "format": {
                "type": "string",
                "enum": ["png", "jpeg", "webp"],
                "description": "Image format (default: png)",
                "default": "png",
            },
            "quality": {
                "type": "number",
                "description": "JPEG quality 0-100 (default: 80)",
                "default": 80,
            },
            "uid": {
                "type": "string",
                "description": "Element uid to screenshot. Full page if omitted.",
            },
            "filePath": {
                "type": "string",
                "description": "Optional path to save the screenshot to a file.",
            },
            "full_page": {
                "type": "boolean",
                "description": "(Deprecated) Use 'fullPage' instead.",
            },
        },
        handler=_take_screenshot_handler,
    )
)


# --- generate_pdf ---
async def _generate_pdf_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Generate a PDF of the current page."""
    landscape = args.get("landscape", False)
    print_background = args.get("printBackground", args.get("print_background", True))
    file_path = args.get("filePath")

    result = await ctx.send_cdp(
        "Page.printToPDF",
        {
            "landscape": landscape,
            "printBackground": print_background,
            "preferCSSPageSize": True,
        },
    )
    pdf_data = result.get("data", "")

    # Save to file if filePath provided
    if file_path:
        try:
            raw_bytes = base64.b64decode(pdf_data)
            with open(file_path, "wb") as f:
                f.write(raw_bytes)
            return [TextContent(type="text", text=f"PDF saved to {file_path}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error saving PDF: {e}")]

    return [
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=AnyUrl("data:application/pdf;base64"),
                mimeType="application/pdf",
                blob=pdf_data,
            ),
        )
    ]


generate_pdf = register_tool(
    ToolDefinition(
        name="generate_pdf",
        description="Generate a PDF of the current page.",
        category=ToolCategory.SCREENSHOT,
        read_only=True,
        schema={
            "landscape": {
                "type": "boolean",
                "description": "Use landscape orientation (default: false)",
                "default": False,
            },
            "printBackground": {
                "type": "boolean",
                "description": "Print background graphics (default: true)",
                "default": True,
            },
            "filePath": {
                "type": "string",
                "description": "Optional path to save the PDF to a file.",
            },
            # Legacy support
            "print_background": {
                "type": "boolean",
                "description": "(Deprecated) Use 'printBackground' instead.",
            },
        },
        handler=_generate_pdf_handler,
    )
)
