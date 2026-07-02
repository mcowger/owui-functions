"""
Repro script: feed the exact SSE stream from the bug report through
anthropic_function.py's tool_use content_block handlers and print out
exactly what tools_buffer / final text looks like at every step.

Run:
    uv run --with anthropic --with pydantic python tests/repro_tool_use_delta.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic_function as af

# Real captured SSE stream for the buggy turn (2 tool_use blocks in parallel:
# search_memories, glob_search) — provided by the user in turn_response.json.
_REAL_TURN_PATH = Path(__file__).resolve().parent.parent / "turn_response.json"

RAW_SSE_SYNTHETIC = r"""event: message_start
data: {"type":"message_start","message":{"id":"msg_mr2vtsuf","type":"message","role":"assistant","model":"claude-sonnet-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}

event: ping
data: {"type":"ping"}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"I"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"'"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ll look"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" for"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" the open"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"-web"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ui source to"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" check"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" how t"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ool"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"/sk"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ill toggles"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" work"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_bdrk_01N68UifXwrob4PwgUezFZ5s","name":"list_files","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"directory\": \"/"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":73}}

event: message_stop
data: {"type":"message_stop"}
"""


def parse_sse(raw: str):
    events = []
    block_lines: list[str] = []

    def flush():
        if not block_lines:
            return
        ev_type = None
        data = None
        for bl in block_lines:
            if bl.startswith("event:"):
                ev_type = bl[len("event:"):].strip()
            elif bl.startswith("data:"):
                data = json.loads(bl[len("data:"):].strip())
        if ev_type and data is not None:
            events.append((ev_type, data))

    for line in raw.splitlines():
        if line.strip() == "":
            flush()
            block_lines.clear()
        else:
            block_lines.append(line)
    flush()
    return events


def to_ns(obj, *, _key=None):
    """Recursively convert dicts/lists into SimpleNamespace so getattr()
    behaves exactly like it does against the real anthropic SDK event
    objects that anthropic_function.py expects.

    The real Anthropic SDK's ToolUseBlock.input is typed as a plain dict
    (not a nested pydantic model) — content_block.input is used as a dict
    (e.g. `initial_input or {}`, json.dumps(initial_input)). So we leave
    the "input" key as a raw dict instead of recursing into SimpleNamespace.
    """
    if isinstance(obj, dict):
        return SimpleNamespace(
            **{
                k: (v if k == "input" else to_ns(v, _key=k))
                for k, v in obj.items()
            }
        )
    if isinstance(obj, list):
        return [to_ns(v) for v in obj]
    return obj


class FakeValves:
    ENABLE_BASH_TOOL = False
    ENABLE_TEXT_EDITOR_TOOL = False


class FakePipe:
    valves = FakeValves()

    @staticmethod
    async def _await_tool_task_result(tool_call_data, coro):
        result = await coro
        return tool_call_data, result, None


# Use the REAL block-formatting/append methods from Pipe (unbound classmethods
# are fine here — _format_tool_result_block/_append_block_to_text don't touch
# self in a way that requires full pipe construction... except
# _format_tool_result_block IS an instance method, so bind it to our FakePipe).
real_format_tool_result_block = af.Pipe._format_tool_result_block
real_append_block_to_text = af.Pipe._append_block_to_text


async def main() -> None:
    if _REAL_TURN_PATH.exists():
        print(f"Using REAL captured SSE stream from {_REAL_TURN_PATH}\n")
        raw_sse = _REAL_TURN_PATH.read_text()
    else:
        print("Real turn_response.json not found — using synthetic single-tool SSE\n")
        raw_sse = RAW_SSE_SYNTHETIC

    events = parse_sse(raw_sse)
    print(f"Parsed {len(events)} SSE events\n")

    chunk = ""
    chunk_count = 0
    tools_buffer = ""
    tool_input_buffer = ""
    tool_name = None
    tool_id_at_start = None
    tool_progress_blocks: dict[str, str] = {}
    final_message: list[str] = []

    def final_text() -> str:
        return "".join(final_message)

    async def emit_delta(content: str) -> None:
        final_message.append(content)

    async def emit_replace(content: str) -> None:
        final_message.clear()
        final_message.append(content)

    async def emit_event(_event: dict) -> None:
        return None

    pipe = FakePipe()
    running_tool_tasks: list[asyncio.Task] = []

    # POST-FIX: both search_memories and glob_search are real, callable tools
    # (e.g. Open Terminal tools registered in __tools__ with a 'callable').
    # There is no more "api_tool_names passthrough" special-case — every
    # tool call is dispatched for real execution via __tools__/builtin_tools,
    # exactly like OWUI's own native tool-calling contract expects.
    async def _fake_search_memories(**kwargs):
        return json.dumps({"results": []})

    async def _fake_glob_search(**kwargs):
        return json.dumps({"matches": ["open-webui/"]})

    fake_tools = {
        "search_memories": {"callable": _fake_search_memories},
        "glob_search": {"callable": _fake_glob_search},
    }

    for ev_type, data in events:
        event = to_ns(data)
        print(f"--- SSE event: {ev_type} ---")

        if ev_type == "content_block_start":
            content_block = event.content_block
            content_type = getattr(content_block, "type", None)
            if content_type == "text":
                chunk = af.handle_text_block_start(content_block, chunk)
            elif content_type == "tool_use":
                (
                    tool_name,
                    tool_id_at_start,
                    tools_buffer,
                    tool_input_buffer,
                    _cewf,
                    _cehu,
                ) = await af.handle_tool_use_block_start(
                    content_block,
                    in_code_execution=False,
                    code_exec_is_web_filtering=False,
                    code_exec_has_user_tools=False,
                    tool_progress_blocks=tool_progress_blocks,
                    final_text=final_text,
                    final_message=final_message,
                    append_block_to_text=lambda t, b: real_append_block_to_text(t, b),
                    format_tool_result_block=lambda *a, **k: real_format_tool_result_block(
                        pipe, *a, **k
                    ),
                    emit_replace=emit_replace,
                )
                print(f"  tool_name={tool_name!r} tool_id={tool_id_at_start!r}")
                print(f"  tools_buffer AFTER start = {tools_buffer!r}")

        elif ev_type == "content_block_delta":
            delta = event.delta
            delta_type = getattr(delta, "type", None)
            if delta_type == "text_delta":
                chunk, chunk_count = await af.handle_text_delta(
                    delta, chunk=chunk, chunk_count=chunk_count
                )
            elif delta_type == "input_json_delta":
                partial = getattr(delta, "partial_json", "")
                print(f"  partial_json chunk = {partial!r}")
                tools_buffer, tool_input_buffer = await af.handle_client_tool_input_delta(
                    partial,
                    tools_buffer=tools_buffer,
                    tool_input_buffer=tool_input_buffer,
                    in_code_execution=False,
                    tool_id_at_start=tool_id_at_start,
                    tool_name=tool_name,
                    tool_progress_blocks=tool_progress_blocks,
                    try_parse_partial_json=af.Pipe._try_parse_partial_json,
                    format_tool_result_block=lambda *a, **k: real_format_tool_result_block(
                        pipe, *a, **k
                    ),
                    final_text=final_text,
                    final_message=final_message,
                    emit_event=emit_event,
                )
                print(f"  tools_buffer AFTER delta = {tools_buffer!r}")

        elif ev_type == "content_block_stop":
            content_block = getattr(event, "content_block", None)
            content_type = getattr(content_block, "type", None) if content_block else None
            if content_type == "text":
                chunk, chunk_count, _pc = await af.handle_text_block_stop(
                    chunk=chunk,
                    chunk_count=chunk_count,
                    pending_citation_markers=[],
                    final_message=final_message,
                    final_text=final_text,
                    emit_delta=emit_delta,
                )
            elif tools_buffer:
                print(f"  tools_buffer BEFORE stop-close = {tools_buffer!r}")
                tools_buffer = await af.handle_tool_use_block_stop(
                    pipe=pipe,
                    tools_buffer=tools_buffer,
                    tools=fake_tools,
                    builtin_tools={},
                    running_tool_tasks=running_tool_tasks,
                    emit_delta=emit_delta,
                )
                print(f"  tools_buffer AFTER stop-close = {tools_buffer!r}")

    print("\n=== State after content_block_stop for tool_use ===")
    print("final_message text:", repr(final_text()))
    if not running_tool_tasks:
        print("NO running_tool_tasks were scheduled (tool never dispatched!)")
        return

    # --- Simulate the message_delta(stop_reason="tool_use") completion branch ---
    # This mirrors anthropic_function.py lines ~7369-7510: for each completed
    # tool task, format the "done" block and swap it in for the in-progress
    # block via tool_progress_blocks.pop(tool_use_id).
    for t in running_tool_tasks:
        tool_call_data, tool_result, task_error = await t
        tool_use_id = tool_call_data.get("id", "")
        tool_name_ = tool_call_data.get("name", "")
        tool_input_ = tool_call_data.get("input", {})
        print(f"\nResolved tool task -> id={tool_use_id!r} name={tool_name_!r} input={tool_input_!r}")
        print("Resolved tool task -> result:", tool_result)
        print("Resolved tool task -> error:", task_error)

        completed = real_format_tool_result_block(
            pipe,
            tool_use_id,
            tool_name_,
            tool_input_,
            str(tool_result),
            is_error=False,
            done=True,
            files=None,
            embeds=None,
        )
        old_block = tool_progress_blocks.pop(tool_use_id, None)
        print("old_block found in tool_progress_blocks:", bool(old_block))
        if old_block:
            text = final_text()
            text = text.replace(old_block, completed, 1)
            final_message.clear()
            final_message.append(text)
        else:
            text = real_append_block_to_text(final_text(), completed)
            final_message.clear()
            final_message.append(text)

    print("\n=== FINAL markup sent to Open WebUI (chat:message) ===")
    print(final_text())


if __name__ == "__main__":
    asyncio.run(main())
