"""Performance tracing tools for Chrome MCP.

Includes: performance_start_trace, performance_stop_trace, performance_analyze_insight

Provides performance trace recording via CDP Tracing domain.
"""

from __future__ import annotations

import asyncio
import json
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

# Trace categories matching ChromeDevTools/Lighthouse
TRACE_CATEGORIES = [
    "-*",
    "blink.console",
    "blink.user_timing",
    "devtools.timeline",
    "disabled-by-default-devtools.screenshot",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.timeline.invalidationTracking",
    "disabled-by-default-devtools.timeline.frame",
    "disabled-by-default-devtools.timeline.stack",
    "disabled-by-default-v8.cpu_profiler",
    "disabled-by-default-v8.cpu_profiler.hires",
    "latencyInfo",
    "loading",
    "disabled-by-default-lighthouse",
    "v8.execute",
    "v8",
]


async def _start_trace_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Start a performance trace recording."""
    if ctx.instance.trace_active:
        return [
            TextContent(
                type="text",
                text=(
                    "Error: a performance trace is already running. "
                    "Use performance_stop_trace to stop it."
                ),
            )
        ]

    reload_page = args.get("reload", False)
    auto_stop = args.get("autoStop", False)

    # Start tracing
    await ctx.send_cdp(
        "Tracing.start",
        {
            "categories": ",".join(TRACE_CATEGORIES),
            "transferMode": "ReturnAsStream",
        },
    )
    ctx.instance.trace_active = True
    ctx.instance.trace_events = []

    if reload_page:
        await ctx.send_cdp("Page.reload")
        await asyncio.sleep(2)  # Wait for reload

    if auto_stop:
        await asyncio.sleep(5)  # Record for 5 seconds
        return await _do_stop_trace(ctx, args.get("filePath"))

    return [
        TextContent(
            type="text",
            text="Performance trace recording started. Use performance_stop_trace to stop.",
        )
    ]


async def _do_stop_trace(ctx: ToolContext, file_path: str | None = None) -> ContentResult:
    """Internal: stop trace and return results."""
    try:
        # Stop tracing
        await ctx.send_cdp("Tracing.end")

        # Wait briefly for trace data
        await asyncio.sleep(1)

        ctx.instance.trace_active = False

        # Get collected trace events
        events = ctx.instance.trace_events
        ctx.instance.trace_events = []

        if not events:
            return [
                TextContent(
                    type="text",
                    text="Performance trace stopped. No events collected.",
                )
            ]

        # Basic trace analysis
        summary = _analyze_trace(events)

        lines = ["Performance trace stopped.", ""]
        lines.append(f"Collected {len(events)} trace events.")
        lines.append("")
        lines.extend(summary)

        if file_path:
            # Save raw trace data
            try:
                trace_data = json.dumps({"traceEvents": events})
                with open(file_path, "w") as f:
                    f.write(trace_data)
                lines.append(f"\nRaw trace saved to {file_path}")
            except Exception as e:
                lines.append(f"\nFailed to save trace: {e}")

        return [TextContent(type="text", text="\n".join(lines))]

    except Exception as e:
        ctx.instance.trace_active = False
        return [TextContent(type="text", text=f"Error stopping trace: {e}")]


def _analyze_trace(events: list[dict[str, Any]]) -> list[str]:
    """Basic trace event analysis."""
    lines = ["## Trace Summary"]

    # Count event categories
    categories: dict[str, int] = {}
    for event in events:
        cat = event.get("cat", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    # Look for key metrics
    metrics: dict[str, Any] = {}
    for event in events:
        name = event.get("name", "")
        # LCP
        if name == "largestContentfulPaint::Candidate":
            ts = event.get("ts", 0)
            if ts:
                metrics["LCP"] = ts
        # FCP
        elif name == "firstContentfulPaint":
            ts = event.get("ts", 0)
            if ts:
                metrics["FCP"] = ts
        # Layout shifts
        elif name == "LayoutShift":
            metrics["CLS_events"] = metrics.get("CLS_events", 0) + 1

    if metrics:
        lines.append("### Key Metrics Found")
        for key, value in metrics.items():
            lines.append(f"  - {key}: {value}")

    # Top categories
    lines.append("### Event Categories")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"  - {cat}: {count} events")

    return lines


async def _stop_trace_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Stop the active performance trace."""
    if not ctx.instance.trace_active:
        return [
            TextContent(
                type="text",
                text="No active performance trace. Start one first.",
            )
        ]

    return await _do_stop_trace(ctx, args.get("filePath"))


performance_start_trace = register_tool(
    ToolDefinition(
        name="performance_start_trace",
        description=(
            "Start a performance trace recording on the selected page. "
            "Reports Core Web Vitals and performance insights."
        ),
        category=ToolCategory.PERFORMANCE,
        read_only=False,
        required=["reload", "autoStop"],
        schema={
            "reload": {
                "type": "boolean",
                "description": "Reload the page after starting the trace.",
            },
            "autoStop": {
                "type": "boolean",
                "description": "Auto-stop the trace after ~5 seconds.",
            },
            "filePath": {
                "type": "string",
                "description": "Path to save trace data (e.g., trace.json).",
            },
        },
        handler=_start_trace_handler,
    )
)


performance_stop_trace = register_tool(
    ToolDefinition(
        name="performance_stop_trace",
        description="Stop the active performance trace recording.",
        category=ToolCategory.PERFORMANCE,
        read_only=False,
        schema={
            "filePath": {
                "type": "string",
                "description": "Path to save trace data (e.g., trace.json).",
            },
        },
        handler=_stop_trace_handler,
    )
)


# --- Insight analysis ---

# Known insight names and what trace events they relate to
INSIGHT_EXTRACTORS: dict[str, list[str]] = {
    "LCPBreakdown": ["largestContentfulPaint::Candidate", "LargestContentfulPaint"],
    "DocumentLatency": ["ResourceSendRequest", "ResourceReceiveResponse", "ResourceFinish"],
    "RenderBlocking": ["ResourceSendRequest"],
    "CLSContributors": ["LayoutShift"],
    "LongTasks": ["RunTask"],
    "NetworkRequests": ["ResourceSendRequest", "ResourceFinish"],
    "InteractionToNextPaint": ["EventTiming"],
}


def _extract_insight(events: list[dict[str, Any]], insight_name: str) -> list[str]:
    """Extract insight data from trace events."""
    lines = [f"## Insight: {insight_name}"]

    extractors = INSIGHT_EXTRACTORS.get(insight_name)
    if not extractors:
        lines.append(f"Unknown insight: {insight_name}")
        lines.append(f"Available insights: {', '.join(sorted(INSIGHT_EXTRACTORS))}")
        return lines

    relevant = [e for e in events if e.get("name") in extractors]
    lines.append(f"Found {len(relevant)} related trace events.")

    if insight_name == "LCPBreakdown":
        for ev in relevant:
            ts = ev.get("ts", 0)
            data = ev.get("args", {}).get("data", {})
            size = data.get("size", "?")
            lines.append(f"  LCP candidate: ts={ts}, size={size}")

    elif insight_name == "CLSContributors":
        total_score = 0.0
        for ev in relevant:
            score = ev.get("args", {}).get("data", {}).get("score", 0)
            total_score += score
        lines.append(f"  Total CLS score: {total_score:.4f}")
        lines.append(f"  Layout shift events: {len(relevant)}")

    elif insight_name == "LongTasks":
        long_tasks = [
            e
            for e in relevant
            if (e.get("dur", 0) / 1000) > 50  # > 50ms
        ]
        lines.append(f"  Long tasks (>50ms): {len(long_tasks)}")
        for task in long_tasks[:10]:
            dur_ms = task.get("dur", 0) / 1000
            lines.append(f"    Duration: {dur_ms:.1f}ms")

    else:
        # Generic: show first 10 events
        for ev in relevant[:10]:
            name = ev.get("name", "?")
            ts = ev.get("ts", 0)
            dur = ev.get("dur", 0)
            lines.append(f"  {name}: ts={ts}, dur={dur / 1000:.1f}ms")

    return lines


async def _analyze_insight_handler(args: dict[str, Any], ctx: ToolContext) -> ContentResult:
    """Analyze a specific performance insight from the last trace."""
    insight_name = args.get("insightName", "")

    if not insight_name:
        return [TextContent(type="text", text="Error: insightName is required")]

    events = ctx.instance.trace_events
    if not events:
        return [
            TextContent(
                type="text",
                text="No trace data available. Run performance_start_trace first.",
            )
        ]

    lines = _extract_insight(events, insight_name)
    return [TextContent(type="text", text="\n".join(lines))]


performance_analyze_insight = register_tool(
    ToolDefinition(
        name="performance_analyze_insight",
        description=(
            "Provides more detailed information on a specific Performance "
            "Insight from a trace recording."
        ),
        category=ToolCategory.PERFORMANCE,
        read_only=True,
        required=["insightName"],
        schema={
            "insightSetId": {
                "type": "string",
                "description": "The id for the specific insight set.",
            },
            "insightName": {
                "type": "string",
                "description": 'The insight name (e.g., "LCPBreakdown", "CLSContributors").',
            },
        },
        handler=_analyze_insight_handler,
    )
)
