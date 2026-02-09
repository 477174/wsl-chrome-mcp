"""Snapshot tools for Chrome MCP.

Includes: take_snapshot, wait_for

The snapshot provides a text representation of the page based on the
accessibility tree, with unique identifiers (uid) for each element.
Format matches ChromeDevTools/chrome-devtools-mcp.
"""

from __future__ import annotations

import asyncio
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

# Attributes to exclude from display
EXCLUDED_ATTRS = {
    "nodeId",
    "parentId",
    "childIds",
    "backendDOMNodeId",
    "frameId",
    "children",
    "role",
    "name",
    "value",
    "description",
    "ignored",
}

# Boolean property mappings (like ChromeDevTools)
BOOLEAN_PROPERTY_MAP = {
    "disabled": "disableable",
    "expanded": "expandable",
    "focused": "focusable",
    "selected": "selectable",
}


class SnapshotBuilder:
    """Builds a formatted accessibility tree snapshot.

    Generates stable UIDs in format {snapshot_id}_{counter} matching ChromeDevTools.
    """

    def __init__(self, snapshot_id: int = 1, verbose: bool = False) -> None:
        self.snapshot_id = snapshot_id
        self.verbose = verbose
        self._counter = 0
        self.uid_map: dict[str, dict[str, Any]] = {}

    def _next_uid(self) -> str:
        """Generate next UID in ChromeDevTools format."""
        uid = f"{self.snapshot_id}_{self._counter}"
        self._counter += 1
        return uid

    def _get_attr_value(self, node: dict[str, Any], key: str) -> Any:
        """Get attribute value from node (handles CDP format)."""
        val = node.get(key)
        if isinstance(val, dict) and "value" in val:
            return val["value"]
        return val

    def _format_attributes(self, node: dict[str, Any], uid: str) -> list[str]:
        """Format node attributes for display."""
        attrs = [f"uid={uid}"]

        # Role
        role = self._get_attr_value(node, "role")
        if role:
            if role == "none":
                attrs.append("ignored")
            else:
                attrs.append(role)

        # Name (quoted)
        name = self._get_attr_value(node, "name")
        if name:
            attrs.append(f'"{name}"')

        # Value
        value = self._get_attr_value(node, "value")
        if value:
            attrs.append(f'value="{value}"')

        # Description
        description = self._get_attr_value(node, "description")
        if description and self.verbose:
            attrs.append(f'description="{description}"')

        # Other attributes (verbose mode or important ones)
        for key in sorted(node.keys()):
            if key in EXCLUDED_ATTRS:
                continue

            val = self._get_attr_value(node, key)
            if val is None:
                continue

            # Handle boolean properties
            if key in BOOLEAN_PROPERTY_MAP and val is True:
                attrs.append(BOOLEAN_PROPERTY_MAP[key])
                attrs.append(key)
            elif val is True:
                attrs.append(key)
            elif isinstance(val, (str, int, float)) and self.verbose:
                attrs.append(f'{key}="{val}"')

        return attrs

    def format_node(self, node: dict[str, Any], depth: int = 0) -> str:
        """Format a single node and its children.

        Returns formatted text string.
        """
        lines = []

        # Skip ignored nodes in non-verbose mode
        if node.get("ignored") and not self.verbose:
            for child in node.get("children", []):
                child_text = self.format_node(child, depth)
                if child_text:
                    lines.append(child_text)
            return "\n".join(filter(None, lines))

        # Generate UID and store mapping
        uid = self._next_uid()
        backend_node_id = node.get("backendDOMNodeId")

        self.uid_map[uid] = {
            "role": self._get_attr_value(node, "role"),
            "name": self._get_attr_value(node, "name"),
            "value": self._get_attr_value(node, "value"),
            "backendNodeId": backend_node_id,
            "node": node,
        }

        # Format this node's line
        attrs = self._format_attributes(node, uid)
        indent = " " * (depth * 2)
        lines.append(f"{indent}{' '.join(attrs)}")

        # Process children
        for child in node.get("children", []):
            child_text = self.format_node(child, depth + 1)
            if child_text:
                lines.append(child_text)

        return "\n".join(lines)

    def build_tree(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build tree structure from flat CDP node list."""
        node_map = {n.get("nodeId"): n for n in nodes}
        root_nodes = []

        for node in nodes:
            parent_id = node.get("parentId")
            if parent_id is None:
                root_nodes.append(node)
            else:
                parent = node_map.get(parent_id)
                if parent:
                    if "children" not in parent:
                        parent["children"] = []
                    parent["children"].append(node)

        return root_nodes


async def capture_snapshot(ctx: ToolContext, verbose: bool = False) -> str:
    """Capture an accessibility snapshot and update the instance cache.

    This is the core snapshot logic shared by take_snapshot, includeSnapshot,
    and auto-snapshot-after-navigation.

    Returns:
        Formatted snapshot text with element count summary.
    """
    await ctx.send_cdp("Accessibility.enable")

    result = await ctx.send_cdp("Accessibility.getFullAXTree")
    nodes = result.get("nodes", [])

    if not nodes:
        return "No accessibility tree available."

    snapshot_id = getattr(ctx.instance, "_snapshot_counter", 0) + 1
    ctx.instance._snapshot_counter = snapshot_id  # type: ignore[attr-defined]

    builder = SnapshotBuilder(snapshot_id=snapshot_id, verbose=verbose)
    root_nodes = builder.build_tree(nodes)

    lines = []
    for root in root_nodes:
        text = builder.format_node(root, 0)
        if text:
            lines.append(text)

    ctx.instance.snapshot_cache = builder.uid_map
    ctx.instance.snapshot_node_ids = {
        uid: info["backendNodeId"]
        for uid, info in builder.uid_map.items()
        if info.get("backendNodeId") is not None
    }

    snapshot_text = "\n".join(lines)
    element_count = len(builder.uid_map)
    return f"{snapshot_text}\n\n[{element_count} elements]"


async def maybe_include_snapshot(
    args: dict[str, Any], ctx: ToolContext, action_result: ContentResult
) -> ContentResult:
    """Append a snapshot to the action result if includeSnapshot is true."""
    if not args.get("includeSnapshot", False):
        return action_result

    try:
        snapshot_text = await capture_snapshot(ctx)
        return [
            *action_result,
            TextContent(type="text", text=f"\n--- Page Snapshot ---\n{snapshot_text}"),
        ]
    except Exception as e:
        logger.warning("Failed to capture post-action snapshot: %s", e)
        return action_result


async def _take_snapshot_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Take an accessibility tree snapshot of the page."""
    verbose = args.get("verbose", False)

    full_text = await capture_snapshot(ctx, verbose=verbose)

    file_path = args.get("filePath")
    if file_path:
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(full_text)
            return [TextContent(type="text", text=f"Snapshot saved to {file_path}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error saving snapshot: {e}")]

    return [TextContent(type="text", text=full_text)]


take_snapshot = register_tool(
    ToolDefinition(
        name="take_snapshot",
        description=(
            "Capture the page's accessibility tree as text with unique element "
            "identifiers (uid). UIDs are REQUIRED by click, fill, hover, drag, "
            "and other input tools. Always call this before interacting with "
            "elements. Prefer this over take_screenshot for understanding page content."
        ),
        category=ToolCategory.SNAPSHOT,
        read_only=False,  # Not read-only due to filePath option
        schema={
            "verbose": {
                "type": "boolean",
                "description": "Include all a11y tree info. Default is false.",
                "default": False,
            },
            "filePath": {
                "type": "string",
                "description": "Optional path to save snapshot to file.",
            },
        },
        handler=_take_snapshot_handler,
    )
)


# --- wait_for ---
async def _wait_for_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Wait for specified text to appear on the page."""
    text = args.get("text", "")
    timeout = args.get("timeout", 10000)  # Default 10s in ms

    if not text:
        return [TextContent(type="text", text="Error: text is required")]

    timeout_s = timeout / 1000 if timeout > 100 else timeout  # Handle ms or s
    poll_interval = 0.5
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        # Check if text exists in page
        js = f"document.body.innerText.includes({repr(text)})"
        found = await ctx.evaluate_js(js)
        if found:
            return [
                TextContent(
                    type="text",
                    text=f'Element with text "{text}" found.',
                )
            ]
        await asyncio.sleep(poll_interval)

    return [
        TextContent(
            type="text",
            text=f'Timeout: Text "{text}" not found after {timeout_s}s',
        )
    ]


wait_for = register_tool(
    ToolDefinition(
        name="wait_for",
        description="Wait for specified text to appear on the selected page.",
        category=ToolCategory.NAVIGATION,
        read_only=True,
        required=["text"],
        schema={
            "text": {
                "type": "string",
                "description": "Text to appear on the page",
            },
            **TIMEOUT_SCHEMA,
        },
        handler=_wait_for_handler,
    )
)
