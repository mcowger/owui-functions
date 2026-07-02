"""
Regression tests for Open WebUI 'direct' tool-server dispatch in
responses.py (OpenAI Responses manifold) and gemini.py (Gemini manifold).

Same root cause and fix as anthropic_function.py's
tests/test_direct_tool_dispatch.py: Open WebUI tool-server integrations
(e.g. Open Terminal's run_command/list_files/glob_search/etc.) register in
__tools__ with no local 'callable' — instead {'direct': True, 'server': {...}}.
Open WebUI's own native middleware executes these via a WebSocket round-trip:
__event_call__({'type': 'execute:tool', 'data': {...}}). Both manifolds'
OpenWebUIToolExecutor previously only recognized entries with a truthy
'callable', so direct tools always resolved to "Tool not found" even though
__event_call__ was already threaded into pipe().

Run:
    uv run --with openai --with pydantic --with google-genai --with pytest \\
        pytest tests/test_direct_tool_dispatch_responses_gemini.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


responses_mod = _load_module("responses.py", "responses_mod_for_tests")
gemini_mod = _load_module("gemini.py", "gemini_mod_for_tests")


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


# --- responses.py -----------------------------------------------------------

def test_responses_direct_tool_dispatches_via_event_call():
    ToolCall = responses_mod.ToolCall
    OpenWebUIToolExecutor = responses_mod.OpenWebUIToolExecutor

    captured_events: list[dict] = []

    async def fake_event_call(event: dict):
        captured_events.append(event)
        return {"entries": ["a.txt", "b.txt"]}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            parallel=True,
            event_call=fake_event_call,
            metadata={"session_id": "sess-123"},
        )
        call = ToolCall(call_id="call_1", name="list_files", arguments_json='{"directory": "/"}')
        results = await executor.execute([call])
        return results

    results = asyncio.run(run())

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["type"] == "execute:tool"
    assert event["data"]["name"] == "list_files"
    assert event["data"]["params"] == {"directory": "/"}
    assert event["data"]["server"] == DIRECT_TOOL_ENTRY["server"]
    assert event["data"]["session_id"] == "sess-123"

    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"
    assert "entries" in result.output
    assert "not found" not in result.output.lower()


def test_responses_direct_tool_without_event_call_reports_clear_error():
    ToolCall = responses_mod.ToolCall
    OpenWebUIToolExecutor = responses_mod.OpenWebUIToolExecutor

    async def run():
        executor = OpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            parallel=True,
            event_call=None,
            metadata={},
        )
        call = ToolCall(call_id="call_2", name="list_files", arguments_json='{"directory": "/"}')
        return await executor.execute([call])

    results = asyncio.run(run())
    assert len(results) == 1
    result = results[0]
    assert result.status == "error"
    assert "requires __event_call__" in result.output


def test_responses_non_direct_tool_still_uses_callable_path():
    ToolCall = responses_mod.ToolCall
    OpenWebUIToolExecutor = responses_mod.OpenWebUIToolExecutor

    async def fake_callable(**kwargs):
        return {"ok": True, "kwargs": kwargs}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"search_memories": {"callable": fake_callable, "spec": {"name": "search_memories"}}},
            parallel=True,
        )
        call = ToolCall(call_id="call_3", name="search_memories", arguments_json='{"query": "x"}')
        return await executor.execute([call])

    results = asyncio.run(run())
    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"
    assert "ok" in result.output


# --- gemini.py ---------------------------------------------------------------

def test_gemini_direct_tool_dispatches_via_event_call():
    ToolCall = gemini_mod.ToolCall
    OpenWebUIToolExecutor = gemini_mod.OpenWebUIToolExecutor

    captured_events: list[dict] = []

    async def fake_event_call(event: dict):
        captured_events.append(event)
        return {"entries": ["a.txt", "b.txt"]}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            parallel=True,
            event_call=fake_event_call,
            metadata={"session_id": "sess-456"},
        )
        call = ToolCall(call_id="call_1", name="list_files", arguments={"directory": "/"})
        results = await executor.execute([call])
        return results

    results = asyncio.run(run())

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["type"] == "execute:tool"
    assert event["data"]["name"] == "list_files"
    assert event["data"]["params"] == {"directory": "/"}
    assert event["data"]["server"] == DIRECT_TOOL_ENTRY["server"]
    assert event["data"]["session_id"] == "sess-456"

    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"
    assert "entries" in result.output_text
    assert "not found" not in result.output_text.lower()


def test_gemini_direct_tool_without_event_call_reports_clear_error():
    ToolCall = gemini_mod.ToolCall
    OpenWebUIToolExecutor = gemini_mod.OpenWebUIToolExecutor

    async def run():
        executor = OpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            parallel=True,
            event_call=None,
            metadata={},
        )
        call = ToolCall(call_id="call_2", name="list_files", arguments={"directory": "/"})
        return await executor.execute([call])

    results = asyncio.run(run())
    assert len(results) == 1
    result = results[0]
    assert result.status == "error"
    assert "requires __event_call__" in result.output_text


def test_gemini_non_direct_tool_still_uses_callable_path():
    ToolCall = gemini_mod.ToolCall
    OpenWebUIToolExecutor = gemini_mod.OpenWebUIToolExecutor

    async def fake_callable(**kwargs):
        return {"ok": True, "kwargs": kwargs}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"search_memories": {"callable": fake_callable, "spec": {"name": "search_memories"}}},
            parallel=True,
        )
        call = ToolCall(call_id="call_3", name="search_memories", arguments={"query": "x"})
        return await executor.execute([call])

    results = asyncio.run(run())
    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"
    assert "ok" in result.output_text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
