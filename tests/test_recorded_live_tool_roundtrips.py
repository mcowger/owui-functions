"""Network-free regressions derived from sanitized live OWUI tool-loop chats.

The fixtures contain only the second-invocation assistant/tool message shape.
They intentionally retain opaque provider continuation fields so adapters must
round-trip the same structures observed in production.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "live_tool_roundtrips"
sys.path.insert(0, str(SRC_ROOT))

from google.genai import types as gemini_types  # noqa: E402
from owui_manifolds.providers.anthropic.pipe import Pipe as AnthropicPipe  # noqa: E402
from owui_manifolds.providers.gemini.pipe import (
    HistoryManager as GeminiHistoryManager,
)  # noqa: E402
from owui_manifolds.providers.responses.pipe import (
    HistoryManager as ResponsesHistoryManager,
)  # noqa: E402


def _fixture(provider: str) -> dict:
    return json.loads((FIXTURE_ROOT / f"{provider}.json").read_text())


class _NoResponsesStore:
    def load_items(self, **kwargs):
        return {}

    def save_items(self, **kwargs):
        return []


class _NoGeminiStore:
    async def load_assistant_message_item_payloads(self, *args, **kwargs):
        return []

    async def load_items(self, *args, **kwargs):
        return {}

    async def save_items(self, *args, **kwargs):
        return []


def test_recorded_responses_exchange_rebuilds_native_input_items():
    fixture = _fixture("responses")
    manager = ResponsesHistoryManager(_NoResponsesStore())

    items, instructions = manager.build_input_from_messages(
        messages=fixture["messages"],
        chat_key=None,
        model_id="gpt-5.6-luna",
        openwebui_model_id=fixture["provenance"]["model"],
    )

    assert instructions is None
    assert [item["type"] for item in items] == [
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert items[0] == fixture["messages"][0]["reasoning_details"][0]["item"]
    assert items[1]["call_id"] == fixture["expected"]["call_id"]
    assert items[1]["name"] == fixture["expected"]["name"]
    assert json.loads(items[1]["arguments"]) == fixture["expected"]["arguments"]
    assert fixture["expected"]["tool_result_contains"] in items[2]["output"]


def test_recorded_gemini_exchange_restores_signed_contents_and_function_response():
    fixture = _fixture("gemini")
    manager = GeminiHistoryManager(_NoGeminiStore())

    contents, system = asyncio.run(
        manager.build_contents_from_messages(
            messages=fixture["messages"],
            chat_key=None,
            model_id="gemini-3.5-flash",
        )
    )

    assert system is None
    native_payloads = fixture["messages"][0]["reasoning_details"][0]["contents"]
    assert len(contents) == len(native_payloads) + 1
    original = gemini_types.Content.model_validate(native_payloads[0])
    restored_call = contents[0].parts[0]
    assert restored_call.function_call.id == fixture["expected"]["call_id"]
    assert restored_call.function_call.name == fixture["expected"]["name"]
    assert restored_call.thought_signature == original.parts[0].thought_signature

    function_response = contents[-1].parts[0].function_response
    assert function_response.id == fixture["expected"]["call_id"]
    assert function_response.name == fixture["expected"]["name"]
    assert fixture["expected"]["tool_result_contains"] in json.dumps(
        function_response.response
    )


def test_recorded_anthropic_exchange_restores_signed_blocks_before_tool_result():
    fixture = _fixture("anthropic")
    pipe = AnthropicPipe()

    system, messages = asyncio.run(
        pipe._convert_messages(fixture["messages"], memory_enabled=False)
    )

    native_blocks = fixture["messages"][0]["reasoning_details"][0]["content"]
    assert system == []
    assert messages[0] == {"role": "assistant", "content": native_blocks}
    assert messages[0]["content"][0]["signature"] == native_blocks[0]["signature"]
    assert messages[0]["content"][1]["id"] == fixture["expected"]["call_id"]
    assert messages[1]["role"] == "user"
    tool_result = messages[1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == fixture["expected"]["call_id"]
    assert fixture["expected"]["tool_result_contains"] in tool_result["content"]
