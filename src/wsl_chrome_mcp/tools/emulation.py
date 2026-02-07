"""Emulation tools for Chrome MCP.

Includes: emulate

Provides network throttling, CPU throttling, geolocation, user agent,
viewport, and color scheme emulation matching ChromeDevTools.
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

# Predefined network conditions matching Puppeteer/ChromeDevTools
NETWORK_CONDITIONS = {
    "Slow 3G": {
        "offline": False,
        "downloadThroughput": 50000,  # 50 KB/s
        "uploadThroughput": 50000,
        "latency": 2000,
    },
    "Fast 3G": {
        "offline": False,
        "downloadThroughput": 187500,  # 1.5 Mbps
        "uploadThroughput": 93750,
        "latency": 562,
    },
    "Slow 4G": {
        "offline": False,
        "downloadThroughput": 500000,  # 4 Mbps
        "uploadThroughput": 375000,
        "latency": 170,
    },
    "Fast 4G": {
        "offline": False,
        "downloadThroughput": 1875000,  # 15 Mbps
        "uploadThroughput": 937500,
        "latency": 40,
    },
    "Offline": {
        "offline": True,
        "downloadThroughput": 0,
        "uploadThroughput": 0,
        "latency": 0,
    },
}


async def _emulate_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Apply emulation settings to the current page."""
    applied: list[str] = []

    # Network conditions
    network = args.get("networkConditions")
    if network:
        if network == "No emulation":
            await ctx.send_cdp(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "downloadThroughput": -1,
                    "uploadThroughput": -1,
                    "latency": 0,
                },
            )
            applied.append("Network emulation disabled")
        elif network in NETWORK_CONDITIONS:
            conditions = NETWORK_CONDITIONS[network]
            await ctx.send_cdp("Network.enable")
            await ctx.send_cdp("Network.emulateNetworkConditions", conditions)
            applied.append(f"Network: {network}")

    # CPU throttling
    cpu_rate = args.get("cpuThrottlingRate")
    if cpu_rate is not None:
        await ctx.send_cdp("Emulation.setCPUThrottlingRate", {"rate": cpu_rate})
        if cpu_rate <= 1:
            applied.append("CPU throttling disabled")
        else:
            applied.append(f"CPU: {cpu_rate}x slowdown")

    # Geolocation
    geolocation = args.get("geolocation")
    if geolocation is not None:
        if geolocation is False or geolocation == "null":
            await ctx.send_cdp("Emulation.clearGeolocationOverride")
            applied.append("Geolocation cleared")
        elif isinstance(geolocation, dict):
            await ctx.send_cdp(
                "Emulation.setGeolocationOverride",
                {
                    "latitude": geolocation.get("latitude", 0),
                    "longitude": geolocation.get("longitude", 0),
                    "accuracy": 1,
                },
            )
            lat = geolocation.get("latitude", 0)
            lng = geolocation.get("longitude", 0)
            applied.append(f"Geolocation: {lat}, {lng}")

    # User agent
    user_agent = args.get("userAgent")
    if user_agent is not None:
        if not user_agent:
            await ctx.send_cdp("Emulation.setUserAgentOverride", {"userAgent": ""})
            applied.append("User agent cleared")
        else:
            await ctx.send_cdp("Emulation.setUserAgentOverride", {"userAgent": user_agent})
            applied.append(f"User agent: {user_agent[:50]}...")

    # Color scheme
    color_scheme = args.get("colorScheme")
    if color_scheme:
        if color_scheme == "auto":
            await ctx.send_cdp(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-color-scheme", "value": ""}]},
            )
            applied.append("Color scheme reset to auto")
        else:
            await ctx.send_cdp(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-color-scheme", "value": color_scheme}]},
            )
            applied.append(f"Color scheme: {color_scheme}")

    # Viewport
    viewport = args.get("viewport")
    if viewport is not None:
        if not viewport:
            await ctx.send_cdp("Emulation.clearDeviceMetricsOverride")
            applied.append("Viewport reset")
        elif isinstance(viewport, dict):
            await ctx.send_cdp(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": viewport.get("width", 1280),
                    "height": viewport.get("height", 720),
                    "deviceScaleFactor": viewport.get("deviceScaleFactor", 1),
                    "mobile": viewport.get("isMobile", False),
                },
            )
            w = viewport.get("width", 1280)
            h = viewport.get("height", 720)
            applied.append(f"Viewport: {w}x{h}")

    if not applied:
        return [TextContent(type="text", text="No emulation settings changed.")]

    return [TextContent(type="text", text="Applied:\n" + "\n".join(f"  - {a}" for a in applied))]


emulate = register_tool(
    ToolDefinition(
        name="emulate",
        description="Emulate various features on the selected page.",
        category=ToolCategory.EMULATION,
        read_only=False,
        schema={
            "networkConditions": {
                "type": "string",
                "enum": [
                    "No emulation",
                    "Offline",
                    "Slow 3G",
                    "Fast 3G",
                    "Slow 4G",
                    "Fast 4G",
                ],
                "description": "Throttle network. 'No emulation' to disable.",
            },
            "cpuThrottlingRate": {
                "type": "number",
                "description": "CPU slowdown factor (1-20). 1 disables.",
            },
            "geolocation": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "description": "Lat -90 to 90"},
                    "longitude": {"type": "number", "description": "Lng -180 to 180"},
                },
                "description": "Geolocation to emulate. null to clear.",
            },
            "userAgent": {
                "type": "string",
                "description": "User agent string. null to clear.",
            },
            "colorScheme": {
                "type": "string",
                "enum": ["dark", "light", "auto"],
                "description": "Emulate dark or light mode. 'auto' to reset.",
            },
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "number", "description": "Width in pixels"},
                    "height": {"type": "number", "description": "Height in pixels"},
                    "deviceScaleFactor": {"type": "number"},
                    "isMobile": {"type": "boolean"},
                },
                "description": "Viewport to emulate. null to reset.",
            },
        },
        handler=_emulate_handler,
    )
)
