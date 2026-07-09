"""
Regression test for the "empty response.output on response.completed" bug.

Symptom (observed live): the upstream proxy (plexus) streams a function_call
item via response.output_item.added / .done events, but the terminal
response.completed event carries a response object with an EMPTY output array.

responses.py's _extract_tool_calls() reads response["output"], so it found
zero tool calls, never executed the tool, and emitted only marker
placeholders — nothing visible rendered in Open WebUI.

Fix: ResponsesEngine._stream_single_response backfills response["output"]
from the output_item.done items observed during the stream when the terminal
snapshot returns an empty/missing output array.

Run:
    uv run --with openai --with pydantic --with pytest \\
        pytest tests/test_empty_output_backfill.py -v
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


responses_mod = _load_module("owui_manifolds/providers/responses/pipe.py", "responses_mod_backfill_test")


def _make_buggy_events():
    """Reproduce the exact live event sequence: reasoning + function_call
    items streamed via output_item.done, then response.completed with an
    EMPTY output array (the proxy spec violation)."""
    parse = responses_mod.parse_responses_event
    fc_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_abc",
        "name": "list_files",
        "arguments": '{"directory": "/"}',
    }
    reasoning_item = {"type": "reasoning", "id": "rs_1", "summary": []}
    return [
        parse({"type": "response.output_item.added", "output_index": 0, "item": reasoning_item}),
        parse({"type": "response.output_item.added", "output_index": 1, "item": fc_item}),
        parse({"type": "response.output_item.done", "output_index": 0, "item": reasoning_item}),
        parse({"type": "response.output_item.done", "output_index": 1, "item": fc_item}),
        # BUG: terminal snapshot has empty output despite streamed items.
        parse({"type": "response.completed", "response": {"output": [], "usage": {}}}),
    ]


class _StubClient:
    def __init__(self, events):
        self._events = events

    async def stream_responses(self, request, *, base_url, api_key, max_retries=3):
        for ev in self._events:
            yield ev

    async def create_response(self, request, *, base_url, api_key, max_retries=3):
        return {}

    async def close(self):
        return None


class _RecordingExecutor:
    def __init__(self):
        self.executed = []

    async def execute(self, calls):
        self.executed.extend(calls)
        ToolResult = responses_mod.ToolResult
        return [
            ToolResult(
                call_id=c.call_id,
                output='{"entries": ["a.txt", "b.txt"]}',
                status="ok",
                error_message=None,
            )
            for c in calls
        ]


class _CollectingEvents(responses_mod.RuntimeEvents):
    def __init__(self):
        self.deltas = []
        self.completions = []
        self.citations = []

    async def status(self, description, *, done=False, **extra):
        pass

    async def delta(self, content):
        self.deltas.append(content)

    async def replace(self, content):
        pass

    async def citation(self, data):
        self.citations.append(data)

    async def source(self, data):
        pass

    async def chat_completion(self, data):
        self.completions.append(data)

    async def notification(self, content, *, level="info"):
        pass


def _build_ctx(cfg):
    TurnContext = responses_mod.TurnContext
    return TurnContext(
        runtime_config=cfg,
        model_id="gpt-5.1",
        metadata={"chat_id": None, "message_id": "m1", "owui_model_id": None},
    )


def _make_engine():
    ResponsesEngine = responses_mod.ResponsesEngine
    HistoryManager = responses_mod.HistoryManager

    class _NoStore:
        def load_items(self, **kw):
            return {}

        def save_items(self, **kw):
            return []

    return ResponsesEngine(_StubClient([]), HistoryManager(_NoStore()))


def test_backfills_output_and_executes_tool_when_completed_output_empty():
    ResponsesEngine = responses_mod.ResponsesEngine
    HistoryManager = responses_mod.HistoryManager

    class _NoStore:
        def load_items(self, **kw):
            return {}

        def save_items(self, **kw):
            return []

    cfg = responses_mod.PipeValves()
    # Only allow one tool-call loop + final response.
    engine = ResponsesEngine(_StubClient(_make_buggy_events()), HistoryManager(_NoStore()))

    # Second stream (after tool execution) returns a normal text answer so the
    # loop terminates cleanly.
    parse = responses_mod.parse_responses_event
    msg_item = {
        "type": "message",
        "id": "msg_1",
        "content": [{"type": "output_text", "text": "done"}],
    }
    second_events = [
        parse({"type": "response.output_item.added", "output_index": 0, "item": msg_item}),
        parse({"type": "response.output_text.delta", "output_index": 0, "delta": "done"}),
        parse({"type": "response.output_item.done", "output_index": 0, "item": msg_item}),
        parse({"type": "response.completed", "response": {"output": [msg_item], "usage": {}}}),
    ]

    # Build a client that returns the buggy events first, then the text answer.
    class _TwoStreamClient:
        def __init__(self):
            self._calls = 0

        async def stream_responses(self, request, *, base_url, api_key, max_retries=3):
            self._calls += 1
            evs = _make_buggy_events() if self._calls == 1 else second_events
            for ev in evs:
                yield ev

        async def create_response(self, request, *, base_url, api_key, max_retries=3):
            return {}

    engine = ResponsesEngine(_TwoStreamClient(), HistoryManager(_NoStore()))

    ctx = _build_ctx(cfg)
    events = _CollectingEvents()
    executor = _RecordingExecutor()
    request = {"model": "gpt-5.1", "input": [], "stream": True}

    result = asyncio.run(
        engine.run_streaming_turn(
            request=request,
            ctx=ctx,
            events=events,
            history_key={"chat_id": None, "pipe_id": "openai_responses"},
            tool_executor=executor,
        )
    )

    # The tool must have been executed despite the empty completed.output.
    assert len(executor.executed) == 1
    assert executor.executed[0].name == "list_files"
    assert executor.executed[0].call_id == "call_abc"

    # A tool_calls <details> block must have been emitted to the UI.
    assert any('type="tool_calls"' in d for d in events.deltas)

    # Final text answer should have streamed through.
    assert "done" in (result.text or "")

    # The terminal chat_completion event must not overwrite the message with
    # the raw visible buffer, which still contains internal placeholders until
    # persistence has cleaned them up.
    assert events.completions
    assert responses_mod.MARKER_PLACEHOLDER not in events.completions[-1]["content"]


def test_completed_event_terminates_stream_without_waiting_for_eof():
    """Some upstream proxies deliver response.completed but leave the stream
    transport open. The engine should treat the terminal event as sufficient
    and finish the turn instead of waiting forever for iterator EOF."""
    ResponsesEngine = responses_mod.ResponsesEngine
    HistoryManager = responses_mod.HistoryManager
    parse = responses_mod.parse_responses_event

    class _NoStore:
        def load_items(self, **kw):
            return {}

        def save_items(self, **kw):
            return []

    msg_item = {
        "type": "message",
        "id": "msg_done",
        "content": [{"type": "output_text", "text": "done"}],
    }

    class _HangingAfterCompletedClient:
        async def stream_responses(self, request, *, base_url, api_key, max_retries=3):
            yield parse({"type": "response.output_item.added", "output_index": 0, "item": msg_item})
            yield parse({"type": "response.output_text.delta", "output_index": 0, "delta": "done"})
            yield parse({"type": "response.output_item.done", "output_index": 0, "item": msg_item})
            yield parse({"type": "response.completed", "response": {"output": [msg_item], "usage": {}}})
            await asyncio.Event().wait()

        async def create_response(self, request, *, base_url, api_key, max_retries=3):
            return {}

    async def run():
        engine = ResponsesEngine(_HangingAfterCompletedClient(), HistoryManager(_NoStore()))
        return await engine.run_streaming_turn(
            request={"model": "gpt-5.1", "input": [], "stream": True},
            ctx=_build_ctx(responses_mod.PipeValves()),
            events=_CollectingEvents(),
            history_key={"chat_id": None, "pipe_id": "openai_responses"},
            tool_executor=_RecordingExecutor(),
        )

    result = asyncio.run(asyncio.wait_for(run(), timeout=1))
    assert "done" in (result.text or "")


def test_successful_turn_logs_are_not_emitted_as_visible_citations():
    session_id = "success-log-session"
    responses_mod.clear_session_logs(session_id)
    responses_mod.SESSION_LOGS[session_id].append("raw debug detail")

    cfg = responses_mod.PipeValves()
    ctx = responses_mod.TurnContext(
        runtime_config=cfg,
        model_id="gpt-5.1",
        metadata={"session_id": session_id},
    )
    state = responses_mod.TurnState(error_message=None)
    events = _CollectingEvents()

    asyncio.run(_make_engine()._emit_log_citation(ctx, state, events))

    assert events.citations == []
    assert state.citations == []
    assert responses_mod.get_session_logs(session_id) == []


def test_error_turn_logs_are_emitted_as_error_logs_citation():
    session_id = "error-log-session"
    responses_mod.clear_session_logs(session_id)
    responses_mod.SESSION_LOGS[session_id].append("error debug detail")

    cfg = responses_mod.PipeValves()
    ctx = responses_mod.TurnContext(
        runtime_config=cfg,
        model_id="gpt-5.1",
        metadata={"session_id": session_id},
    )
    state = responses_mod.TurnState(error_message="boom")
    events = _CollectingEvents()

    asyncio.run(_make_engine()._emit_log_citation(ctx, state, events))

    assert len(events.citations) == 1
    assert events.citations[0]["source"]["name"] == "Error Logs"
    assert "error debug detail" in events.citations[0]["document"][0]
    assert len(state.citations) == 1
    assert responses_mod.get_session_logs(session_id) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
