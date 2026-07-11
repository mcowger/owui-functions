"""
Regression tests for anthropic_function.py tool-result bloat mitigations.

Background: chat 98e30e94-34e0-4a4f-9af8-20e1ddeeebca grew to 5.5MB because
_format_tool_result_block embedded full (unbounded) tool output into the
rendered <details type="tool_calls" result="..."> attribute, and
_parse_assistant_tool_calls resent that full text back to the API on every
subsequent turn forever. The legacy compatibility fix adds:

  1. Explicit preview limits for old rendered blocks.
  2. PERSIST_TOOL_RESULTS (default True): gates side-table persistence.
  3. ToolResultStore: full results are written to
     chat.chat["anthropic_pipe"]["items"][ulid] via a fake Chats model, and
     referenced from the rendered block via a "ref" attribute so full
     fidelity can be restored when reconstructing history, without
     resending the full text on every turn.

Run:
    uv run --with anthropic --with pydantic --with pytest \\
        pytest tests/test_tool_result_persistence.py -v
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
MODULE_PATH = SRC_ROOT / "owui_manifolds/providers/anthropic/pipe.py"

_spec = importlib.util.spec_from_file_location("anthropic_function", MODULE_PATH)
anthropic_function = importlib.util.module_from_spec(_spec)
sys.modules["anthropic_function"] = anthropic_function
_spec.loader.exec_module(anthropic_function)

Pipe = anthropic_function.Pipe


def _make_pipe() -> Pipe:
    return Pipe()


class _FakeChatModel:
    """Mimics the shape of open_webui.models.chats.ChatModel enough for the
    store: a `.chat` dict attribute holding the persisted JSON blob."""

    def __init__(self, chat: dict):
        self.chat = chat


class _FakeChats:
    """In-memory stand-in for open_webui.models.chats.Chats, async like the
    live model (get_chat_by_id/update_chat_by_id are awaited there)."""

    def __init__(self):
        self._chats: dict[str, dict] = {}

    async def get_chat_by_id(self, chat_id: str):
        if chat_id not in self._chats:
            self._chats[chat_id] = {}
        return _FakeChatModel(self._chats[chat_id])

    async def update_chat_by_id(self, chat_id: str, chat: dict):
        self._chats[chat_id] = chat


def test_format_tool_result_block_truncates_long_output():
    """An explicitly bounded legacy preview records that it was truncated."""
    huge_output = "x" * 10000
    rendered = Pipe._format_tool_result_block(
        "toolu_1", "ha_get_history", {}, huge_output, max_chars=100
    )
    assert "truncated" in rendered
    # Only a 100-char preview of 'x' plus the truncation suffix is embedded,
    # not the full 10000-char output.
    assert "x" * 10000 not in rendered
    assert "x" * 101 not in rendered


def test_format_tool_result_block_untruncated_when_within_limit():
    rendered = Pipe._format_tool_result_block(
        "toolu_1", "search", {}, "short result", max_chars=4000
    )
    assert "truncated" not in rendered
    assert "short result" in rendered


def test_format_tool_result_block_embeds_ref_attribute():
    rendered = Pipe._format_tool_result_block(
        "toolu_1", "search", {}, "result", ref="01ABCDEFGH"
    )
    assert 'ref="01ABCDEFGH"' in rendered


def test_format_tool_result_block_omits_ref_when_absent():
    rendered = Pipe._format_tool_result_block("toolu_1", "search", {}, "result")
    assert "ref=" not in rendered


def test_legacy_store_persists_full_result_and_truncates_visible_preview():
    """End-to-end: a tool producing a huge result gets (a) a truncated
    visible block and (b) the full payload stored in the side-table under
    chat.chat['anthropic_pipe']['items'], referenced by the block's ref."""
    pipe = _make_pipe()
    preview_limit = 50
    fake_chats = _FakeChats()
    pipe.tool_result_store = anthropic_function.ToolResultStore(chats_model=fake_chats)

    huge_output = "y" * 5000
    call = {"id": "toolu_1", "name": "ha_get_history", "input": {}}

    async def run():
        ulid = anthropic_function.generate_ulid()
        saved = await pipe.tool_result_store.save(
            "chat-1",
            ulid,
            {
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
                "output": huge_output,
                "is_error": False,
            },
        )
        assert saved is True
        return ulid

    ulid = asyncio.run(run())

    rendered = Pipe._format_tool_result_block(
        call["id"],
        call["name"],
        call["input"],
        huge_output,
        max_chars=preview_limit,
        ref=ulid,
    )
    assert "y" * 5000 not in rendered
    assert f'ref="{ulid}"' in rendered

    stored_chat = fake_chats._chats["chat-1"]
    items = stored_chat["anthropic_pipe"]["items"]
    assert items[ulid]["payload"]["output"] == huge_output


def test_parse_assistant_tool_calls_restores_full_result_from_side_table():
    """History reconstruction: when a rendered block's 'result' attribute is
    a truncated preview but carries a 'ref', the full-fidelity output is
    pulled from the side-table instead of resending the truncated text."""
    pipe = _make_pipe()
    fake_chats = _FakeChats()
    pipe.tool_result_store = anthropic_function.ToolResultStore(chats_model=fake_chats)
    full_output = "z" * 5000

    async def run():
        ulid = anthropic_function.generate_ulid()
        await pipe.tool_result_store.save(
            "chat-1",
            ulid,
            {
                "id": "toolu_1",
                "name": "ha_get_history",
                "input": {},
                "output": full_output,
                "is_error": False,
            },
        )
        rendered = pipe._format_tool_result_block(
            "toolu_1", "ha_get_history", {}, full_output, max_chars=50, ref=ulid
        )
        return await pipe._parse_assistant_tool_calls(rendered, "chat-1")

    messages = asyncio.run(run())

    tool_result_msgs = [m for m in messages if m["role"] == "user"]
    assert tool_result_msgs
    result_block = tool_result_msgs[0]["content"][0]
    assert result_block["content"] == full_output


def test_parse_assistant_tool_calls_falls_back_to_preview_without_persist():
    """When PERSIST_TOOL_RESULTS is disabled, history reconstruction uses
    only the (possibly truncated) preview text — no side-table lookup."""
    pipe = _make_pipe()
    pipe.valves.PERSIST_TOOL_RESULTS = False
    fake_chats = _FakeChats()
    pipe.tool_result_store = anthropic_function.ToolResultStore(chats_model=fake_chats)
    full_output = "w" * 5000

    async def run():
        ulid = anthropic_function.generate_ulid()
        await pipe.tool_result_store.save(
            "chat-1",
            ulid,
            {
                "id": "toolu_1",
                "name": "ha_get_history",
                "input": {},
                "output": full_output,
                "is_error": False,
            },
        )
        rendered = pipe._format_tool_result_block(
            "toolu_1", "ha_get_history", {}, full_output, max_chars=50, ref=ulid
        )
        return await pipe._parse_assistant_tool_calls(rendered, "chat-1")

    messages = asyncio.run(run())
    tool_result_msgs = [m for m in messages if m["role"] == "user"]
    result_block = tool_result_msgs[0]["content"][0]
    assert result_block["content"] != full_output
    assert len(result_block["content"]) < len(full_output)


def test_tool_result_store_load_returns_none_for_missing_ulid():
    pipe = _make_pipe()
    fake_chats = _FakeChats()
    store = anthropic_function.ToolResultStore(chats_model=fake_chats)

    async def run():
        return await store.load("chat-1", "does-not-exist")

    assert asyncio.run(run()) is None


def test_tool_result_store_save_noop_without_chat_id():
    fake_chats = _FakeChats()
    store = anthropic_function.ToolResultStore(chats_model=fake_chats)

    async def run():
        return await store.save(None, "ULID", {"output": "x"})

    assert asyncio.run(run()) is False


class _FakeToolUseBlock:
    """Mimics an SDK ToolUseBlock with a partially-parsed `.input` (the state
    left behind when jiter aborts mid-delta on malformed JSON)."""

    type = "tool_use"

    def __init__(self, block_id: str, name: str, input_: dict):
        self.id = block_id
        self.name = name
        self.input = input_

    def model_dump(self, exclude_none=True, mode="json"):
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


class _FakeFinalMessage:
    def __init__(self, content: list, stop_reason: str = "tool_use"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None
        self.stop_sequence = None


class _FakeMessageStream:
    """Mimics client.messages.stream(): async-iterates a few events, then
    raises ValueError partway through (as the real SDK does when jiter hits
    syntactically invalid partial JSON), but still answers
    get_final_message() with whatever was accumulated so far."""

    def __init__(self, final_message):
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        if False:  # pragma: no cover - makes this an async generator
            yield None
        raise ValueError("expected value at line 1 column 48")

    async def get_final_message(self):
        return self._final_message


class _FakeClient:
    """Mimics AsyncAnthropic enough for the native single-response adapter."""

    def __init__(self, stream_fn):
        self._stream_fn = stream_fn

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def stream(self, **kwargs):
            return self._outer._stream_fn(**kwargs)

    @property
    def messages(self):
        return _FakeClient._Messages(self)


class _CollectingEvents:
    def __init__(self):
        self.deltas: list[str] = []
        self.notifications: list[dict] = []

    async def delta(self, content):
        self.deltas.append(content)

    async def replace(self, content):
        pass

    async def status(self, *a, **k):
        pass

    async def emit(self, event):
        if isinstance(event, dict) and event.get("type") == "notification":
            self.notifications.append(event)


def test_native_stream_recovers_malformed_tool_input_without_executing_it():
    """Malformed partial tool JSON still returns a tool call to Open WebUI.

    The Anthropic adapter owns only one native provider response. It must use
    the SDK's final partial snapshot, emit the recovered tool call, and leave
    execution to Open WebUI rather than invoking the callable itself.
    """
    pipe = _make_pipe()
    executed_with: dict = {}

    async def fake_tool(domain_filter=None, area_filter=None):
        executed_with["domain_filter"] = domain_filter
        executed_with["area_filter"] = area_filter
        return "ok"

    tool_block = _FakeToolUseBlock(
        "toolu_1", "ha_ha_search", {"domain_filter": "automation"}
    )
    final_message = _FakeFinalMessage([tool_block], stop_reason="tool_use")
    calls = {"count": 0}

    def stream_fn(**kwargs):
        calls["count"] += 1
        return _FakeMessageStream(final_message)

    pipe._make_client = lambda api_key: _FakeClient(stream_fn)
    events = _CollectingEvents()

    async def run():
        return [
            chunk
            async for chunk in pipe._stream_native_once(
                payload={"model": "claude-test", "messages": []},
                client_tool_names={"ha_ha_search"},
                api_key="test-key",
                emit=events.emit,
            )
        ]

    chunks = asyncio.run(run())

    assert calls["count"] == 1
    assert executed_with == {}
    tool_chunk = next(
        chunk
        for chunk in chunks
        if chunk.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
    )
    tool_call = tool_chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tool_call["id"] == "toolu_1"
    assert '"domain_filter": "automation"' in tool_call["function"]["arguments"]
    assert not events.notifications


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
