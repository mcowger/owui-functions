"""
Regression tests for Open WebUI 'direct' tool-server dispatch
(anthropic_function.py Pipe._dispatch_direct_tool / handle_tool_use_block_stop).

Background: Open Terminal (and other OWUI tool-server integrations) register
tools like run_command/list_files/glob_search with no local 'callable' —
instead each entry carries {'direct': True, 'server': {...}}. Open WebUI's own
native middleware (utils/middleware.py tool_call_handler) executes these via
a WebSocket round-trip: __event_call__({'type': 'execute:tool', 'data': {...}}).

Prior to this fix, anthropic_function.py's pipe() never declared/accepted
__event_call__ at all, so 'direct' tools always fell through to "Tool '...'
is not available" — even though they were present in __tools__ with a valid
spec, and were correctly included in the outgoing Anthropic tools payload.

Run:
    uv run --with anthropic --with pydantic --with pytest --with fastapi \\
        pytest tests/test_direct_tool_dispatch.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "anthropic_function.py"

_spec = importlib.util.spec_from_file_location("anthropic_function", MODULE_PATH)
anthropic_function = importlib.util.module_from_spec(_spec)
sys.modules["anthropic_function"] = anthropic_function
_spec.loader.exec_module(anthropic_function)

Pipe = anthropic_function.Pipe
handle_tool_use_block_stop = anthropic_function.handle_tool_use_block_stop


def _make_pipe() -> Pipe:
    return Pipe()


# A direct tool-server entry shaped exactly like Open Terminal's registration
# in __tools__ (no 'callable' key at all — see middleware.py direct_tool_servers
# handling: {'spec': tool, 'direct': True, 'server': tool_server}).
DIRECT_TOOL_ENTRY = {
    "spec": {
        "name": "list_files",
        "description": "Return a structured listing of files and directories.",
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": [],
        },
    },
    "direct": True,
    "server": {"url": "http://192.168.0.2:8889", "openapi": {"openapi": "3.1.0"}},
}


def test_direct_tool_dispatches_via_event_call():
    """A 'direct' tool entry with no callable is executed via __event_call__,
    not misrouted into the 'Tool not found' fallback."""
    pipe = _make_pipe()
    captured_events: list[dict] = []

    async def fake_event_call(event: dict) -> dict:
        captured_events.append(event)
        return {"entries": ["a.txt", "b.txt"]}

    async def run():
        tools_buffer = (
            '{"type": "tool_use", "id": "toolu_1", "name": "list_files", '
            '"input": {"directory": "/"}}'
        )
        running_tool_tasks: list[asyncio.Task] = []

        async def emit_delta(_content: str) -> None:
            return None

        result = await handle_tool_use_block_stop(
            pipe=pipe,
            tools_buffer=tools_buffer,
            tools={"list_files": DIRECT_TOOL_ENTRY},
            builtin_tools={},
            running_tool_tasks=running_tool_tasks,
            emit_delta=emit_delta,
            event_call=fake_event_call,
            metadata={"session_id": "sess-123"},
        )
        assert result == ""
        assert len(running_tool_tasks) == 1
        tool_call_data, tool_result, task_error = await running_tool_tasks[0]
        return tool_call_data, tool_result, task_error

    tool_call_data, tool_result, task_error = asyncio.run(run())

    # The event_call round-trip must have been invoked with the OWUI-native
    # 'execute:tool' envelope shape.
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["type"] == "execute:tool"
    assert event["data"]["name"] == "list_files"
    assert event["data"]["params"] == {"directory": "/"}
    assert event["data"]["server"] == DIRECT_TOOL_ENTRY["server"]
    assert event["data"]["session_id"] == "sess-123"

    # The tool must have actually "succeeded" (no dispatch error), and the
    # result must be the real tool output — not a "not available" error and
    # not the raw input JSON dumped back out.
    assert task_error is None
    assert "entries" in tool_result
    assert "not available" not in tool_result
    assert tool_call_data["name"] == "list_files"


def test_direct_tool_without_event_call_reports_clear_error():
    """When __event_call__ isn't available (e.g. API-key callers without a
    live browser session), the direct tool fails with an explicit, honest
    error instead of silently doing nothing or leaking raw JSON."""
    pipe = _make_pipe()

    async def run():
        tools_buffer = (
            '{"type": "tool_use", "id": "toolu_2", "name": "list_files", '
            '"input": {"directory": "/"}}'
        )
        running_tool_tasks: list[asyncio.Task] = []

        async def emit_delta(_content: str) -> None:
            return None

        await handle_tool_use_block_stop(
            pipe=pipe,
            tools_buffer=tools_buffer,
            tools={"list_files": DIRECT_TOOL_ENTRY},
            builtin_tools={},
            running_tool_tasks=running_tool_tasks,
            emit_delta=emit_delta,
            event_call=None,
            metadata={},
        )
        assert len(running_tool_tasks) == 1
        return await running_tool_tasks[0]

    tool_call_data, tool_result, task_error = asyncio.run(run())
    assert task_error is None
    assert "requires __event_call__" in tool_result


def test_non_direct_tool_still_uses_callable_path():
    """Sanity check: a normal callable user tool (no 'direct' flag) is
    unaffected by the new direct-dispatch branch."""
    pipe = _make_pipe()

    async def fake_callable(**kwargs):
        return {"ok": True, "kwargs": kwargs}

    tools = {"search_memories": {"callable": fake_callable}}

    async def run():
        tools_buffer = (
            '{"type": "tool_use", "id": "toolu_3", "name": "search_memories", '
            '"input": {"query": "x"}}'
        )
        running_tool_tasks: list[asyncio.Task] = []

        async def emit_delta(_content: str) -> None:
            return None

        await handle_tool_use_block_stop(
            pipe=pipe,
            tools_buffer=tools_buffer,
            tools=tools,
            builtin_tools={},
            running_tool_tasks=running_tool_tasks,
            emit_delta=emit_delta,
            event_call=None,
            metadata={},
        )
        assert len(running_tool_tasks) == 1
        return await running_tool_tasks[0]

    tool_call_data, tool_result, task_error = asyncio.run(run())
    assert task_error is None
    assert "ok" in tool_result


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
