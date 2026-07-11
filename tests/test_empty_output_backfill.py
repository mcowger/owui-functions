"""
Regression test for the "empty response.output on response.completed" bug.

Symptom (observed live): the upstream proxy (plexus) streams a function_call
item via response.output_item.added / .done events, but the terminal
response.completed event carries a response object with an EMPTY output array.

Open WebUI's Responses stream handler treats response.completed.output as
authoritative. If that array is empty, it discards function calls previously
seen in output_item.done events and cannot execute them.

Fix: ResponsesEngine.stream_single_turn backfills response["output"] from
output_item.done items before returning the terminal event to Open WebUI.

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


responses_mod = _load_module(
    "owui_manifolds/providers/responses/pipe.py", "responses_mod_backfill_test"
)


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
        parse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": reasoning_item,
            }
        ),
        parse(
            {"type": "response.output_item.added", "output_index": 1, "item": fc_item}
        ),
        parse(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": reasoning_item,
            }
        ),
        parse(
            {"type": "response.output_item.done", "output_index": 1, "item": fc_item}
        ),
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
            yield parse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": msg_item,
                }
            )
            yield parse(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "done",
                }
            )
            yield parse(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": msg_item,
                }
            )
            yield parse(
                {
                    "type": "response.completed",
                    "response": {"output": [msg_item], "usage": {}},
                }
            )
            await asyncio.Event().wait()

        async def create_response(self, request, *, base_url, api_key, max_retries=3):
            return {}

    async def run():
        engine = ResponsesEngine(
            _HangingAfterCompletedClient(), HistoryManager(_NoStore())
        )
        return [
            event
            async for event in engine.stream_single_turn(
                request={"model": "gpt-5.1", "input": [], "stream": True},
                ctx=_build_ctx(responses_mod.PipeValves()),
            )
        ]

    events = asyncio.run(asyncio.wait_for(run(), timeout=1))
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["id"] == "msg_done"


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
