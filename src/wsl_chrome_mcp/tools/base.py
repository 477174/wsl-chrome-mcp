"""Base tool definitions and registry for Chrome MCP tools.

This module provides the foundation for the modular tool system,
including ToolDefinition, ToolCategory, and the tool registry.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool

if TYPE_CHECKING:
    from ..chrome_pool import ChromeInstance, ChromePoolManager

logger = logging.getLogger(__name__)

# Type alias for tool return values
ContentResult = Sequence[TextContent | ImageContent | EmbeddedResource]


class ToolCategory(str, Enum):
    """Tool categories matching ChromeDevTools conventions."""

    NAVIGATION = "navigation"
    INPUT = "input"
    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"
    MONITORING = "monitoring"
    SCRIPT = "script"
    EMULATION = "emulation"
    PERFORMANCE = "performance"


class ToolContext(Protocol):
    """Protocol for tool execution context.

    Provides access to Chrome instance and pool manager.
    """

    @property
    def instance(self) -> ChromeInstance:
        """Current Chrome instance."""
        ...

    @property
    def pool(self) -> ChromePoolManager:
        """Chrome pool manager."""
        ...

    async def send_cdp(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a CDP command."""
        ...

    async def evaluate_js(self, expression: str) -> Any:
        """Evaluate JavaScript in the page context."""
        ...


# Type for tool handler functions
ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ContentResult]]


@dataclass
class ToolDefinition:
    """Definition for an MCP tool.

    Matches the structure used by ChromeDevTools/chrome-devtools-mcp.
    """

    name: str
    description: str
    category: ToolCategory
    schema: dict[str, Any]
    handler: ToolHandler
    read_only: bool = True
    required: list[str] = field(default_factory=list)

    def to_mcp_tool(self, session_id_property: dict[str, Any]) -> Tool:
        """Convert to MCP Tool definition with session_id property."""
        properties = {**self.schema, **session_id_property}
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": self.required,
            },
        )


# Global tool registry
_TOOL_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(tool: ToolDefinition) -> ToolDefinition:
    """Register a tool in the global registry."""
    if tool.name in _TOOL_REGISTRY:
        logger.warning("Tool %s already registered, overwriting", tool.name)
    _TOOL_REGISTRY[tool.name] = tool
    logger.debug("Registered tool: %s (%s)", tool.name, tool.category.value)
    return tool


def get_tool(name: str) -> ToolDefinition | None:
    """Get a tool by name."""
    return _TOOL_REGISTRY.get(name)


def get_all_tools() -> list[ToolDefinition]:
    """Get all registered tools sorted by name."""
    return sorted(_TOOL_REGISTRY.values(), key=lambda t: t.name)


def get_tools_by_category(category: ToolCategory) -> list[ToolDefinition]:
    """Get all tools in a category."""
    return [t for t in _TOOL_REGISTRY.values() if t.category == category]


# Common schema fragments
TIMEOUT_SCHEMA = {
    "timeout": {
        "type": "number",
        "description": "Maximum wait time in milliseconds. Default timeout used if not set.",
    }
}

INCLUDE_SNAPSHOT_SCHEMA = {
    "includeSnapshot": {
        "type": "boolean",
        "description": "Whether to include a snapshot in the response. Default is false.",
        "default": False,
    }
}

UID_SCHEMA = {
    "uid": {
        "type": "string",
        "description": "The uid of an element from the page content snapshot (take_snapshot)",
    }
}
