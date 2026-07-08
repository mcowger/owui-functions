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
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


def _load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SRC_ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


responses_mod = _load_module("owui_manifolds/providers/responses/pipe.py", "responses_mod_for_tests")
gemini_mod = _load_module("owui_manifolds/providers/gemini/pipe.py", "gemini_mod_for_tests")


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

def test_responses_direct_tool_with_typeless_params_is_not_strictified():
    """Open Terminal exposes some optional parameters as description-only
    schema fragments. Responses strict mode must not wrap those into invalid
    strict anyOf branches."""
    cfg = responses_mod.PipeValves()
    registry = responses_mod.OpenWebUIToolRegistry(
        {
            "run_command": {
                "spec": {
                    "name": "run_command",
                    "description": "Run a shell command.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "wait": {
                                "description": (
                                    "Seconds to wait before returning. Null to return immediately."
                                )
                            },
                        },
                        "required": ["command"],
                    },
                },
                "direct": True,
                "server": {"url": "http://192.168.0.2:8889"},
            }
        }
    )

    tools = responses_mod.ToolPolicy.build_responses_tools(
        model_id="gpt-5.1",
        cfg=cfg,
        registry=registry,
        body_tools=[],
        extra_tools=[],
        mcp_tools=[],
        web_search_tools=[],
    )

    run_command = next(tool for tool in tools if tool["name"] == "run_command")
    assert run_command["strict"] is False
    assert run_command["parameters"]["properties"]["wait"] == {
        "description": "Seconds to wait before returning. Null to return immediately."
    }


def test_responses_typed_direct_tool_can_still_use_strict_mode():
    cfg = responses_mod.PipeValves()
    registry = responses_mod.OpenWebUIToolRegistry({"list_files": DIRECT_TOOL_ENTRY})

    tools = responses_mod.ToolPolicy.build_responses_tools(
        model_id="gpt-5.1",
        cfg=cfg,
        registry=registry,
        body_tools=[],
        extra_tools=[],
        mcp_tools=[],
        web_search_tools=[],
    )

    list_files = next(tool for tool in tools if tool["name"] == "list_files")
    assert list_files["strict"] is True
    assert list_files["parameters"]["required"] == ["directory"]
    assert list_files["parameters"]["additionalProperties"] is False


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


def test_gemini_namespaced_call_name_resolves_to_bare_tool():
    """Gemini/proxies may surface calls as 'default_api:list_files'. The
    executor must strip the namespace and still find the registered tool."""
    ToolCall = gemini_mod.ToolCall
    OpenWebUIToolExecutor = gemini_mod.OpenWebUIToolExecutor

    captured_events: list[dict] = []

    async def fake_event_call(event: dict):
        captured_events.append(event)
        return {"entries": ["a.txt"]}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"list_files": DIRECT_TOOL_ENTRY},
            parallel=True,
            event_call=fake_event_call,
            metadata={"session_id": "sess-789"},
        )
        results = []
        for name in ("default_api:list_files", "default_api.list_files"):
            call = ToolCall(call_id="c", name=name, arguments={"directory": "/"})
            results.extend(await executor.execute([call]))
        return results

    results = asyncio.run(run())
    assert len(results) == 2
    for result in results:
        assert result.status == "ok", result.output_text
        assert "not found" not in result.output_text.lower()
    assert len(captured_events) == 2


def test_gemini_namespaced_callable_tool_resolves():
    ToolCall = gemini_mod.ToolCall
    OpenWebUIToolExecutor = gemini_mod.OpenWebUIToolExecutor

    async def fake_callable(**kwargs):
        return {"ok": True, "kwargs": kwargs}

    async def run():
        executor = OpenWebUIToolExecutor(
            {"search_memories": {"callable": fake_callable, "spec": {"name": "search_memories"}}},
            parallel=True,
        )
        call = ToolCall(call_id="c", name="default_api:search_memories", arguments={"query": "x"})
        return await executor.execute([call])

    results = asyncio.run(run())
    assert results[0].status == "ok"
    assert "ok" in results[0].output_text


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
