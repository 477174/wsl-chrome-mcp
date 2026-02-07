"""Tests for the modular tools system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from wsl_chrome_mcp.chrome_pool import ConsoleMessage, NetworkRequest
from wsl_chrome_mcp.tools.base import (
    ToolCategory,
    ToolDefinition,
    get_all_tools,
    get_tool,
    get_tools_by_category,
)
from wsl_chrome_mcp.tools.snapshot import SnapshotBuilder

# --- Mock ToolContext ---


class MockToolContext:
    """Mock ToolContext for testing tool handlers."""

    def __init__(self) -> None:
        self._instance = MockChromeInstance()
        self._pool = MagicMock()
        self._cdp_responses: dict[str, Any] = {}
        self._js_responses: dict[str, Any] = {}
        self._cdp_calls: list[tuple[str, Any]] = []

    @property
    def instance(self) -> MockChromeInstance:
        return self._instance

    @property
    def pool(self) -> MagicMock:
        return self._pool

    def set_cdp_response(self, method: str, response: Any) -> None:
        """Set a mock response for a CDP method."""
        self._cdp_responses[method] = response

    def set_js_response(self, expression: str, response: Any) -> None:
        """Set a mock response for a JS expression."""
        self._js_responses[expression] = response

    async def send_cdp(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._cdp_calls.append((method, params))
        if method in self._cdp_responses:
            resp = self._cdp_responses[method]
            if callable(resp):
                return resp(params)
            return resp
        return {}

    async def evaluate_js(self, expression: str) -> Any:
        for key, val in self._js_responses.items():
            if key in expression:
                return val
        return None


@dataclass
class MockChromeInstance:
    """Mock ChromeInstance for testing."""

    session_id: str = "test_session"
    port: int = 9222
    pid: int = 1234
    current_target_id: str = "T1"
    is_connected: bool = True
    console_messages: list[ConsoleMessage] = field(default_factory=list)
    network_requests: dict[str, NetworkRequest] = field(default_factory=dict)
    snapshot_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot_node_ids: dict[str, int] = field(default_factory=dict)
    pending_dialog: Any = None
    trace_active: bool = False
    trace_events: list[dict[str, Any]] = field(default_factory=list)


# --- Tool Registry Tests ---


class TestToolRegistry:
    """Tests for the tool registry system."""

    def test_get_all_tools_returns_sorted(self) -> None:
        """Should return tools sorted by name."""
        tools = get_all_tools()
        names = [t.name for t in tools]
        assert names == sorted(names)

    def test_get_all_tools_not_empty(self) -> None:
        """Registry should have registered tools."""
        tools = get_all_tools()
        assert len(tools) >= 20

    def test_get_tool_by_name(self) -> None:
        """Should find a tool by exact name."""
        tool = get_tool("click")
        assert tool is not None
        assert tool.name == "click"
        assert tool.category == ToolCategory.INPUT

    def test_get_tool_unknown_returns_none(self) -> None:
        """Should return None for unknown tools."""
        assert get_tool("nonexistent_tool") is None

    def test_get_tools_by_category(self) -> None:
        """Should filter tools by category."""
        input_tools = get_tools_by_category(ToolCategory.INPUT)
        assert len(input_tools) >= 4
        for tool in input_tools:
            assert tool.category == ToolCategory.INPUT

    def test_tool_definition_to_mcp_tool(self) -> None:
        """Should convert to MCP Tool with session_id."""
        tool_def = ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.SCRIPT,
            schema={"foo": {"type": "string"}},
            handler=AsyncMock(),
            required=["foo"],
        )
        session_prop = {"session_id": {"type": "string"}}
        mcp_tool = tool_def.to_mcp_tool(session_prop)

        assert mcp_tool.name == "test_tool"
        assert mcp_tool.description == "A test tool"
        assert "foo" in mcp_tool.inputSchema["properties"]
        assert "session_id" in mcp_tool.inputSchema["properties"]
        assert mcp_tool.inputSchema["required"] == ["foo"]

    def test_expected_tools_registered(self) -> None:
        """All expected tools should be registered."""
        expected = [
            "click",
            "fill",
            "hover",
            "press_key",
            "scroll",
            "navigate_page",
            "list_pages",
            "select_page",
            "new_page",
            "close_page",
            "wait_for",
            "take_snapshot",
            "take_screenshot",
            "generate_pdf",
            "get_console",
            "get_network",
            "evaluate",
            "get_html",
            "handle_dialog",
            "resize_page",
            "emulate",
            "performance_start_trace",
            "performance_stop_trace",
            "chrome_session_start",
            "chrome_session_list",
            "chrome_session_end",
        ]
        for name in expected:
            assert get_tool(name) is not None, f"Tool {name} not registered"


# --- SnapshotBuilder Tests ---


class TestSnapshotBuilder:
    """Tests for the accessibility snapshot builder."""

    def test_generates_uids(self) -> None:
        """Should generate UIDs in format snapshot_id_counter."""
        builder = SnapshotBuilder(snapshot_id=1)
        uid = builder._next_uid()
        assert uid == "1_0"
        uid2 = builder._next_uid()
        assert uid2 == "1_1"

    def test_format_node_simple(self) -> None:
        """Should format a simple node with role and name."""
        builder = SnapshotBuilder(snapshot_id=1)
        node = {
            "role": {"value": "button"},
            "name": {"value": "Submit"},
            "backendDOMNodeId": 42,
        }
        text = builder.format_node(node, depth=0)
        assert "uid=1_0" in text
        assert "button" in text
        assert '"Submit"' in text

    def test_format_node_with_children(self) -> None:
        """Should format children with proper indentation."""
        builder = SnapshotBuilder(snapshot_id=1)
        node = {
            "role": {"value": "list"},
            "name": {"value": "Menu"},
            "children": [
                {
                    "role": {"value": "listitem"},
                    "name": {"value": "Item 1"},
                },
                {
                    "role": {"value": "listitem"},
                    "name": {"value": "Item 2"},
                },
            ],
        }
        text = builder.format_node(node, depth=0)
        lines = text.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("uid=")  # Root at depth 0
        assert lines[1].startswith("  uid=")  # Child at depth 1

    def test_skips_ignored_nodes(self) -> None:
        """Should skip ignored nodes in non-verbose mode."""
        builder = SnapshotBuilder(snapshot_id=1, verbose=False)
        node = {
            "role": {"value": "generic"},
            "ignored": True,
            "children": [
                {
                    "role": {"value": "button"},
                    "name": {"value": "Click me"},
                },
            ],
        }
        text = builder.format_node(node, depth=0)
        # Ignored node itself should not appear but child should
        assert "button" in text
        assert "generic" not in text

    def test_uid_map_built(self) -> None:
        """Should build uid_map with backendNodeId."""
        builder = SnapshotBuilder(snapshot_id=2)
        node = {
            "role": {"value": "button"},
            "name": {"value": "OK"},
            "backendDOMNodeId": 99,
        }
        builder.format_node(node, depth=0)
        assert "2_0" in builder.uid_map
        assert builder.uid_map["2_0"]["backendNodeId"] == 99
        assert builder.uid_map["2_0"]["name"] == "OK"

    def test_build_tree_from_flat_list(self) -> None:
        """Should build tree from flat CDP node list."""
        builder = SnapshotBuilder(snapshot_id=1)
        flat_nodes = [
            {"nodeId": "n1", "role": {"value": "document"}, "name": {"value": "Page"}},
            {
                "nodeId": "n2",
                "parentId": "n1",
                "role": {"value": "heading"},
                "name": {"value": "Title"},
            },
            {
                "nodeId": "n3",
                "parentId": "n1",
                "role": {"value": "paragraph"},
                "name": {"value": "Body"},
            },
        ]
        roots = builder.build_tree(flat_nodes)
        assert len(roots) == 1
        assert len(roots[0]["children"]) == 2


# --- Tool Handler Tests ---


class TestNavigationTools:
    """Tests for navigation tool handlers."""

    @pytest.mark.asyncio
    async def test_navigate_page_url(self) -> None:
        """Should navigate to URL and return title."""
        from wsl_chrome_mcp.tools.navigation import navigate_page

        ctx = MockToolContext()
        ctx.set_cdp_response("Page.enable", {})
        ctx.set_cdp_response("Page.navigate", {"frameId": "F1"})
        ctx.set_js_response("document.title", "Test Page")

        result = await navigate_page.handler({"type": "url", "url": "https://example.com"}, ctx)

        assert len(result) == 1
        assert "example.com" in result[0].text

    @pytest.mark.asyncio
    async def test_navigate_page_requires_url(self) -> None:
        """Should error when type=url but no URL provided."""
        from wsl_chrome_mcp.tools.navigation import navigate_page

        ctx = MockToolContext()
        ctx.set_cdp_response("Page.enable", {})

        result = await navigate_page.handler({"type": "url"}, ctx)
        assert "Error" in result[0].text or "required" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_list_pages(self) -> None:
        """Should list open pages."""
        from wsl_chrome_mcp.tools.navigation import list_pages

        ctx = MockToolContext()
        ctx.pool.list_tabs = AsyncMock(
            return_value=[
                {"id": "T1", "title": "Tab 1", "url": "https://example.com", "is_current": True},
            ]
        )

        result = await list_pages.handler({}, ctx)
        assert "Tab 1" in result[0].text

    @pytest.mark.asyncio
    async def test_resize_page(self) -> None:
        """Should send resize CDP command."""
        from wsl_chrome_mcp.tools.navigation import resize_page

        ctx = MockToolContext()
        ctx.set_cdp_response("Emulation.setDeviceMetricsOverride", {})

        result = await resize_page.handler({"width": 800, "height": 600}, ctx)
        assert "800x600" in result[0].text


class TestInputTools:
    """Tests for input tool handlers."""

    @pytest.mark.asyncio
    async def test_click_requires_uid(self) -> None:
        """Should error when no uid provided."""
        from wsl_chrome_mcp.tools.input import click

        ctx = MockToolContext()
        result = await click.handler({}, ctx)
        assert "uid is required" in result[0].text

    @pytest.mark.asyncio
    async def test_click_with_uid_from_snapshot(self) -> None:
        """Should click element found in snapshot cache."""
        from wsl_chrome_mcp.tools.input import click

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {
            "1_0": {
                "role": "button",
                "name": "Submit",
                "backendNodeId": 42,
                "node": {},
            }
        }
        ctx.set_cdp_response("DOM.resolveNode", {"object": {"objectId": "obj1"}})
        ctx.set_cdp_response("Runtime.callFunctionOn", {"result": {"value": None}})
        ctx.set_cdp_response(
            "DOM.getBoxModel",
            {"model": {"content": [10, 10, 50, 10, 50, 30, 10, 30]}},
        )
        ctx.set_cdp_response("Input.dispatchMouseEvent", {})

        result = await click.handler({"uid": "1_0"}, ctx)
        assert "Successfully" in result[0].text

    @pytest.mark.asyncio
    async def test_click_uid_not_in_snapshot(self) -> None:
        """Should error when UID not found in snapshot."""
        from wsl_chrome_mcp.tools.input import click

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {}

        result = await click.handler({"uid": "999_0"}, ctx)
        assert "not found" in result[0].text

    @pytest.mark.asyncio
    async def test_press_key_simple(self) -> None:
        """Should dispatch key events."""
        from wsl_chrome_mcp.tools.input import press_key

        ctx = MockToolContext()
        ctx.set_cdp_response("Input.dispatchKeyEvent", {})

        result = await press_key.handler({"key": "Enter"}, ctx)
        assert "Enter" in result[0].text

    @pytest.mark.asyncio
    async def test_press_key_combination(self) -> None:
        """Should dispatch modifier + key events."""
        from wsl_chrome_mcp.tools.input import press_key

        ctx = MockToolContext()
        ctx.set_cdp_response("Input.dispatchKeyEvent", {})

        result = await press_key.handler({"key": "Control+A"}, ctx)
        assert "Control+A" in result[0].text
        # Should have: keyDown Control, keyDown A, keyUp A, keyUp Control = 4
        key_events = [c for c in ctx._cdp_calls if c[0] == "Input.dispatchKeyEvent"]
        assert len(key_events) == 4


class TestMonitoringTools:
    """Tests for monitoring tool handlers."""

    @pytest.mark.asyncio
    async def test_get_console_empty(self) -> None:
        """Should handle empty console messages."""
        from wsl_chrome_mcp.tools.monitoring import get_console

        ctx = MockToolContext()
        result = await get_console.handler({}, ctx)
        assert "No console messages" in result[0].text

    @pytest.mark.asyncio
    async def test_get_console_with_messages(self) -> None:
        """Should return formatted console messages."""
        from wsl_chrome_mcp.tools.monitoring import get_console

        ctx = MockToolContext()
        ctx.instance.console_messages = [
            ConsoleMessage(type="log", text="Hello", timestamp=1.0),
            ConsoleMessage(type="error", text="Oops", timestamp=2.0),
        ]

        result = await get_console.handler({}, ctx)
        assert "Hello" in result[0].text
        assert "Oops" in result[0].text

    @pytest.mark.asyncio
    async def test_get_console_clear(self) -> None:
        """Should clear messages when clear=True."""
        from wsl_chrome_mcp.tools.monitoring import get_console

        ctx = MockToolContext()
        ctx.instance.console_messages = [
            ConsoleMessage(type="log", text="msg", timestamp=1.0),
        ]

        await get_console.handler({"clear": True}, ctx)
        assert len(ctx.instance.console_messages) == 0

    @pytest.mark.asyncio
    async def test_get_console_filter_types(self) -> None:
        """Should filter by message type."""
        from wsl_chrome_mcp.tools.monitoring import get_console

        ctx = MockToolContext()
        ctx.instance.console_messages = [
            ConsoleMessage(type="log", text="info", timestamp=1.0),
            ConsoleMessage(type="error", text="err", timestamp=2.0),
        ]

        result = await get_console.handler({"types": ["error"]}, ctx)
        assert "err" in result[0].text
        assert "info" not in result[0].text

    @pytest.mark.asyncio
    async def test_get_network_empty(self) -> None:
        """Should handle empty network requests."""
        from wsl_chrome_mcp.tools.monitoring import get_network

        ctx = MockToolContext()
        result = await get_network.handler({}, ctx)
        assert "No network requests" in result[0].text

    @pytest.mark.asyncio
    async def test_get_network_with_requests(self) -> None:
        """Should return formatted network requests."""
        from wsl_chrome_mcp.tools.monitoring import get_network

        ctx = MockToolContext()
        ctx.instance.network_requests = {
            "r1": NetworkRequest(
                request_id="r1",
                url="https://example.com/api",
                method="GET",
                response={"status": 200},
            ),
        }

        result = await get_network.handler({}, ctx)
        assert "example.com" in result[0].text
        assert "200" in result[0].text


class TestScriptTools:
    """Tests for script tool handlers."""

    @pytest.mark.asyncio
    async def test_evaluate(self) -> None:
        """Should evaluate JS and return result."""
        from wsl_chrome_mcp.tools.script import evaluate

        ctx = MockToolContext()
        ctx.set_js_response("1+1", 2)

        result = await evaluate.handler({"expression": "1+1"}, ctx)
        assert "2" in result[0].text

    @pytest.mark.asyncio
    async def test_evaluate_requires_expression(self) -> None:
        """Should error when no expression or function provided."""
        from wsl_chrome_mcp.tools.script import evaluate

        ctx = MockToolContext()
        result = await evaluate.handler({}, ctx)
        assert "Error" in result[0].text

    @pytest.mark.asyncio
    async def test_evaluate_function_mode(self) -> None:
        """Should evaluate function without args."""
        from wsl_chrome_mcp.tools.script import evaluate

        ctx = MockToolContext()
        ctx.set_js_response("document.title", "My Page")

        result = await evaluate.handler({"function": "() => document.title"}, ctx)
        # function mode calls evaluate_js("(() => document.title)()")
        assert result[0].text is not None


class TestEmulationTools:
    """Tests for emulation tool handlers."""

    @pytest.mark.asyncio
    async def test_emulate_network(self) -> None:
        """Should set network conditions."""
        from wsl_chrome_mcp.tools.emulation import emulate

        ctx = MockToolContext()
        ctx.set_cdp_response("Network.enable", {})
        ctx.set_cdp_response("Network.emulateNetworkConditions", {})

        result = await emulate.handler({"networkConditions": "Slow 3G"}, ctx)
        assert "Slow 3G" in result[0].text

    @pytest.mark.asyncio
    async def test_emulate_color_scheme(self) -> None:
        """Should set color scheme."""
        from wsl_chrome_mcp.tools.emulation import emulate

        ctx = MockToolContext()
        ctx.set_cdp_response("Emulation.setEmulatedMedia", {})

        result = await emulate.handler({"colorScheme": "dark"}, ctx)
        assert "dark" in result[0].text

    @pytest.mark.asyncio
    async def test_emulate_no_changes(self) -> None:
        """Should report no changes when no args."""
        from wsl_chrome_mcp.tools.emulation import emulate

        ctx = MockToolContext()
        result = await emulate.handler({}, ctx)
        assert "No emulation settings changed" in result[0].text


class TestPerformanceTools:
    """Tests for performance tool handlers."""

    @pytest.mark.asyncio
    async def test_start_trace(self) -> None:
        """Should start trace recording."""
        from wsl_chrome_mcp.tools.performance import performance_start_trace

        ctx = MockToolContext()
        ctx.set_cdp_response("Tracing.start", {})

        result = await performance_start_trace.handler({"reload": False, "autoStop": False}, ctx)
        assert "recording started" in result[0].text
        assert ctx.instance.trace_active is True

    @pytest.mark.asyncio
    async def test_start_trace_already_running(self) -> None:
        """Should error if trace already active."""
        from wsl_chrome_mcp.tools.performance import performance_start_trace

        ctx = MockToolContext()
        ctx.instance.trace_active = True

        result = await performance_start_trace.handler({"reload": False, "autoStop": False}, ctx)
        assert "already running" in result[0].text

    @pytest.mark.asyncio
    async def test_stop_trace_not_running(self) -> None:
        """Should error if no trace active."""
        from wsl_chrome_mcp.tools.performance import performance_stop_trace

        ctx = MockToolContext()
        result = await performance_stop_trace.handler({}, ctx)
        assert "No active" in result[0].text

    @pytest.mark.asyncio
    async def test_analyze_insight_no_data(self) -> None:
        """Should error when no trace data available."""
        from wsl_chrome_mcp.tools.performance import performance_analyze_insight

        ctx = MockToolContext()
        result = await performance_analyze_insight.handler({"insightName": "LCPBreakdown"}, ctx)
        assert "No trace data" in result[0].text

    @pytest.mark.asyncio
    async def test_analyze_insight_with_events(self) -> None:
        """Should analyze trace events for known insight."""
        from wsl_chrome_mcp.tools.performance import performance_analyze_insight

        ctx = MockToolContext()
        ctx.instance.trace_events = [
            {"name": "LayoutShift", "args": {"data": {"score": 0.05}}},
            {"name": "LayoutShift", "args": {"data": {"score": 0.02}}},
        ]

        result = await performance_analyze_insight.handler({"insightName": "CLSContributors"}, ctx)
        assert "CLS" in result[0].text
        assert "0.0700" in result[0].text


class TestNewInputTools:
    """Tests for the new input tools (drag, fill_form, upload_file, click_at)."""

    @pytest.mark.asyncio
    async def test_drag_requires_both_uids(self) -> None:
        """Should error when missing from_uid or to_uid."""
        from wsl_chrome_mcp.tools.input import drag

        ctx = MockToolContext()
        result = await drag.handler({"from_uid": "1_0"}, ctx)
        assert "required" in result[0].text

    @pytest.mark.asyncio
    async def test_drag_element_not_found(self) -> None:
        """Should error when element not in snapshot."""
        from wsl_chrome_mcp.tools.input import drag

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {}
        result = await drag.handler({"from_uid": "1_0", "to_uid": "1_1"}, ctx)
        assert "not found" in result[0].text

    @pytest.mark.asyncio
    async def test_drag_success(self) -> None:
        """Should drag between two elements."""
        from wsl_chrome_mcp.tools.input import drag

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {
            "1_0": {"role": "item", "name": "A", "backendNodeId": 10, "node": {}},
            "1_1": {"role": "zone", "name": "B", "backendNodeId": 20, "node": {}},
        }
        ctx.set_cdp_response("DOM.resolveNode", {"object": {"objectId": "o1"}})
        ctx.set_cdp_response("Runtime.callFunctionOn", {"result": {"value": None}})
        ctx.set_cdp_response(
            "DOM.getBoxModel", {"model": {"content": [0, 0, 50, 0, 50, 50, 0, 50]}}
        )
        ctx.set_cdp_response("Input.dispatchMouseEvent", {})

        result = await drag.handler({"from_uid": "1_0", "to_uid": "1_1"}, ctx)
        assert "Dragged" in result[0].text

    @pytest.mark.asyncio
    async def test_fill_form_batch(self) -> None:
        """Should fill multiple form elements."""
        from wsl_chrome_mcp.tools.input import fill_form

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {
            "1_0": {"role": "textbox", "name": "Name", "backendNodeId": 10, "node": {}},
            "1_1": {"role": "textbox", "name": "Email", "backendNodeId": 20, "node": {}},
        }
        ctx.set_cdp_response("DOM.focus", {})
        ctx.set_cdp_response("DOM.resolveNode", {"object": {"objectId": "o1"}})
        ctx.set_cdp_response("Runtime.callFunctionOn", {"result": {"value": None}})

        result = await fill_form.handler(
            {"elements": [{"uid": "1_0", "value": "John"}, {"uid": "1_1", "value": "j@e.com"}]},
            ctx,
        )
        assert "Filled 2 elements" in result[0].text
        assert "OK" in result[0].text

    @pytest.mark.asyncio
    async def test_fill_form_empty(self) -> None:
        """Should error when elements array is empty."""
        from wsl_chrome_mcp.tools.input import fill_form

        ctx = MockToolContext()
        result = await fill_form.handler({"elements": []}, ctx)
        assert "required" in result[0].text

    @pytest.mark.asyncio
    async def test_upload_file_requires_params(self) -> None:
        """Should error when uid or filePath missing."""
        from wsl_chrome_mcp.tools.input import upload_file

        ctx = MockToolContext()
        result = await upload_file.handler({"uid": "1_0"}, ctx)
        assert "required" in result[0].text

    @pytest.mark.asyncio
    async def test_upload_file_success(self) -> None:
        """Should upload file via DOM.setFileInputFiles."""
        from wsl_chrome_mcp.tools.input import upload_file

        ctx = MockToolContext()
        ctx.instance.snapshot_cache = {
            "1_0": {"role": "input", "name": "File", "backendNodeId": 10, "node": {}},
        }
        ctx.set_cdp_response("DOM.setFileInputFiles", {})

        result = await upload_file.handler({"uid": "1_0", "filePath": "/tmp/test.txt"}, ctx)
        assert "Uploaded" in result[0].text

    @pytest.mark.asyncio
    async def test_click_at_requires_coords(self) -> None:
        """Should error when x or y missing."""
        from wsl_chrome_mcp.tools.input import click_at

        ctx = MockToolContext()
        result = await click_at.handler({"x": 100}, ctx)
        assert "required" in result[0].text

    @pytest.mark.asyncio
    async def test_click_at_success(self) -> None:
        """Should click at coordinates."""
        from wsl_chrome_mcp.tools.input import click_at

        ctx = MockToolContext()
        ctx.set_cdp_response("Input.dispatchMouseEvent", {})

        result = await click_at.handler({"x": 100, "y": 200}, ctx)
        assert "Clicked at (100, 200)" in result[0].text


class TestNewMonitoringTools:
    """Tests for the new monitoring detail tools."""

    @pytest.mark.asyncio
    async def test_get_console_message_by_id(self) -> None:
        """Should return a specific console message."""
        from wsl_chrome_mcp.tools.monitoring import get_console_message

        ctx = MockToolContext()
        ctx.instance.console_messages = [
            ConsoleMessage(type="log", text="First", timestamp=1.0),
            ConsoleMessage(
                type="error",
                text="Second",
                timestamp=2.0,
                stack_trace=[
                    {"url": "test.js", "lineNumber": 10, "columnNumber": 5, "functionName": "foo"},
                ],
            ),
        ]

        result = await get_console_message.handler({"msgid": 1}, ctx)
        assert "Second" in result[0].text
        assert "error" in result[0].text
        assert "test.js" in result[0].text

    @pytest.mark.asyncio
    async def test_get_console_message_out_of_range(self) -> None:
        """Should error when msgid out of range."""
        from wsl_chrome_mcp.tools.monitoring import get_console_message

        ctx = MockToolContext()
        ctx.instance.console_messages = [
            ConsoleMessage(type="log", text="Only one", timestamp=1.0),
        ]

        result = await get_console_message.handler({"msgid": 5}, ctx)
        assert "out of range" in result[0].text

    @pytest.mark.asyncio
    async def test_get_network_request_detail(self) -> None:
        """Should return detailed network request info."""
        from wsl_chrome_mcp.tools.monitoring import get_network_request

        ctx = MockToolContext()
        ctx.instance.network_requests = {
            "r1": NetworkRequest(
                request_id="r1",
                url="https://example.com/api/data",
                method="POST",
                headers={"Content-Type": "application/json"},
                response={"status": 201, "headers": {"X-Custom": "val"}},
            ),
        }
        # Mock Network.getResponseBody to fail (common case)

        result = await get_network_request.handler({"reqid": "r1"}, ctx)
        assert "example.com" in result[0].text
        assert "POST" in result[0].text
        assert "201" in result[0].text
        assert "Content-Type" in result[0].text

    @pytest.mark.asyncio
    async def test_get_network_request_not_found(self) -> None:
        """Should error when request not found."""
        from wsl_chrome_mcp.tools.monitoring import get_network_request

        ctx = MockToolContext()
        result = await get_network_request.handler({"reqid": "nonexistent"}, ctx)
        assert "not found" in result[0].text


class TestRegistryCompleteness:
    """Verify all expected tools are registered."""

    def test_total_tool_count(self) -> None:
        """Should have 33 tools registered."""
        tools = get_all_tools()
        assert len(tools) == 33

    def test_all_new_tools_registered(self) -> None:
        """All new tools from the implementation plan should exist."""
        new_tools = [
            "drag",
            "fill_form",
            "upload_file",
            "click_at",
            "get_console_message",
            "get_network_request",
            "performance_analyze_insight",
        ]
        for name in new_tools:
            assert get_tool(name) is not None, f"Tool {name} not registered"
