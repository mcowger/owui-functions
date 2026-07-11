"""Regression tests for Open WebUI-owned custom-function loops.

Each manifold must perform exactly one provider call per pipe invocation and
return provider output in a stream shape Open WebUI 0.10.2 can orchestrate.
Provider-native signed continuation data is carried through OWUI's
``reasoning_details`` extension and restored on the next invocation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))


def _load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SRC_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


responses_mod = _load_module(
    "owui_manifolds/providers/responses/pipe.py", "responses_native_loop_test"
)
gemini_mod = _load_module(
    "owui_manifolds/providers/gemini/pipe.py", "gemini_native_loop_test"
)
anthropic_mod = _load_module(
    "owui_manifolds/providers/anthropic/pipe.py", "anthropic_native_loop_test"
)

from google.genai import types as gemini_types  # noqa: E402


async def _collect(stream):
    return [item async for item in stream]


def test_pipe_entrypoints_delegate_tool_ownership_to_open_webui():
    responses_source = inspect.getsource(responses_mod.Pipe.pipe)
    gemini_source = inspect.getsource(gemini_mod.Pipe.pipe)
    anthropic_source = inspect.getsource(anthropic_mod.Pipe.pipe)

    assert "prepare_context_for_pipe" in responses_source
    assert "engine.stream_single_turn" in responses_source
    assert "engine.run_streaming_turn" not in responses_source
    assert "prepare_context_for_pipe" in gemini_source
    assert "_stream_native_once" in gemini_source
    assert "_run_task_request" in gemini_source
    assert "tool_executor" not in gemini_source
    assert "prepare_context_for_pipe" in anthropic_source
    assert "_stream_native_once" in anthropic_source
    assert "_run_streaming_turn" not in anthropic_source


class _NoResponsesStore:
    def load_items(self, **kwargs):
        return {}

    def save_items(self, **kwargs):
        return []


class _ResponsesClient:
    def __init__(self):
        self.requests = []

    async def stream_responses(self, request, **kwargs):
        self.requests.append(request)
        reasoning = {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "signed-reasoning",
            "summary": [],
        }
        function_call = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"query":"x"}',
        }
        yield {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": reasoning,
        }
        yield {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": function_call,
        }
        yield {
            "type": "response.completed",
            "response": {"id": "resp_1", "output": [reasoning, function_call]},
        }

    async def create_response(self, request, **kwargs):
        return {}


def test_responses_engine_streams_one_native_call_and_preserves_reasoning():
    client = _ResponsesClient()
    engine = responses_mod.ResponsesEngine(
        client, responses_mod.HistoryManager(_NoResponsesStore())
    )
    ctx = responses_mod.TurnContext(
        runtime_config=responses_mod.PipeValves(),
        model_id="gpt-5.6-luna",
    )

    events = asyncio.run(_collect(engine.stream_single_turn({"input": []}, ctx)))

    assert len(client.requests) == 1
    completed = events[-1]["response"]["output"]
    reasoning = next(item for item in completed if item["type"] == "reasoning")
    assert reasoning["reasoning_details"][0]["type"] == "openai_responses"
    assert (
        reasoning["reasoning_details"][0]["item"]["encrypted_content"]
        == "signed-reasoning"
    )


def test_responses_single_stream_repairs_empty_terminal_output():
    class _EmptyTerminalClient(_ResponsesClient):
        async def stream_responses(self, request, **kwargs):
            self.requests.append(request)
            function_call = {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": "{}",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": function_call,
            }
            yield {
                "type": "response.completed",
                "response": {"id": "resp_1", "output": []},
            }
            raise AssertionError("terminal response should stop stream consumption")

    client = _EmptyTerminalClient()
    engine = responses_mod.ResponsesEngine(
        client, responses_mod.HistoryManager(_NoResponsesStore())
    )
    ctx = responses_mod.TurnContext(
        runtime_config=responses_mod.PipeValves(), model_id="gpt-5.6-luna"
    )

    events = asyncio.run(_collect(engine.stream_single_turn({"input": []}, ctx)))

    assert events[-1]["response"]["output"][0]["call_id"] == "call_1"


def test_responses_history_restores_owui_tool_exchange_and_native_reasoning():
    manager = responses_mod.HistoryManager(_NoResponsesStore())
    native_reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "signed-reasoning",
        "summary": [],
    }
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {
                    "type": "openai_responses",
                    "index": 0,
                    "item": native_reasoning,
                }
            ],
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"x"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    input_items, _ = manager.build_input_from_messages(
        messages=messages,
        chat_key=None,
        model_id="gpt-5.6-luna",
        openwebui_model_id=None,
    )

    assert [item["type"] for item in input_items] == [
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert input_items[0]["encrypted_content"] == "signed-reasoning"
    assert input_items[2]["output"] == "result"


class _NoGeminiStore:
    async def load_assistant_message_item_payloads(self, *args, **kwargs):
        return []

    async def load_items(self, *args, **kwargs):
        return {}

    async def save_items(self, *args, **kwargs):
        return []


class _GeminiClient:
    def __init__(self, chunks):
        self.calls = 0
        self.requests = []
        outer = self

        class _Models:
            async def generate_content_stream(inner, *, model, contents, config):
                outer.calls += 1
                outer.requests.append((model, contents, config))

                async def _stream():
                    for chunk in chunks:
                        yield chunk

                return _stream()

        self.models = _Models()


class _NoopEvents:
    async def source(self, data):
        pass

    async def notification(self, *args, **kwargs):
        pass

    async def status(self, *args, **kwargs):
        pass


def _gemini_chunk(*parts):
    return gemini_types.GenerateContentResponse(
        candidates=[
            gemini_types.Candidate(
                content=gemini_types.Content(role="model", parts=list(parts))
            )
        ]
    )


def test_gemini_streams_one_native_call_without_executing_tool(monkeypatch):
    function_part = gemini_types.Part(
        function_call=gemini_types.FunctionCall(
            id="call_g", name="search", args={"query": "x"}
        ),
        thought_signature=b"gemini-signature",
    )
    client = _GeminiClient([_gemini_chunk(function_part)])

    @asynccontextmanager
    async def _client_context(cfg):
        yield client

    monkeypatch.setattr(gemini_mod, "_gemini_client", _client_context)
    pipe = gemini_mod.Pipe()
    pipe.history_manager = gemini_mod.HistoryManager(_NoGeminiStore())
    cfg = gemini_mod.RuntimeConfig(API_KEY="test")
    executed = {"count": 0}

    async def should_not_run(**kwargs):
        executed["count"] += 1
        return "wrong owner"

    registry = gemini_mod.OpenWebUIToolRegistry(
        {
            "search": {
                "spec": {
                    "name": "search",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {}},
                },
                "callable": should_not_run,
            }
        }
    )
    stream = pipe._stream_native_once(
        body={"model": "gemini-3.5-flash", "messages": []},
        cfg=cfg,
        events=_NoopEvents(),
        tool_registry=registry,
        metadata={},
    )

    chunks = asyncio.run(_collect(stream))

    assert client.calls == 1
    assert executed["count"] == 0
    tool_delta = next(
        chunk
        for chunk in chunks
        if chunk.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
    )
    assert tool_delta["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_g"
    detail = next(
        chunk["choices"][0]["delta"]["reasoning_details"][0]
        for chunk in chunks
        if chunk.get("choices", [{}])[0].get("delta", {}).get("reasoning_details")
    )
    assert detail["type"] == "google_gemini"
    restored = gemini_types.Content.model_validate(detail["contents"][0])
    assert restored.parts[0].thought_signature == b"gemini-signature"


def test_gemini_history_restores_signed_content_and_tool_result():
    manager = gemini_mod.HistoryManager(_NoGeminiStore())
    native_content = gemini_types.Content(
        role="model",
        parts=[
            gemini_types.Part(
                function_call=gemini_types.FunctionCall(
                    id="call_g", name="search", args={"query": "x"}
                ),
                thought_signature=b"gemini-signature",
            )
        ],
    ).model_dump(mode="json", exclude_none=True)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {
                    "type": "google_gemini",
                    "index": 0,
                    "contents": [native_content],
                }
            ],
            "tool_calls": [
                {
                    "id": "call_g",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"x"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_g",
            "content": '{"items":[1]}',
        },
    ]

    contents, _ = asyncio.run(
        manager.build_contents_from_messages(
            messages=messages,
            chat_key=None,
            model_id="gemini-3.5-flash",
        )
    )

    function_part = contents[0].parts[0]
    assert function_part.function_call.name == "search"
    assert function_part.thought_signature == b"gemini-signature"
    response_part = contents[1].parts[0]
    assert response_part.function_response.id == "call_g"
    assert response_part.function_response.name == "search"
    assert response_part.function_response.response == {"items": [1]}


class _AnthropicBlock:
    def __init__(self, data):
        self._data = data
        for key, value in data.items():
            setattr(self, key, value)

    def model_dump(self, **kwargs):
        return dict(self._data)


class _AnthropicUsage:
    def model_dump(self, **kwargs):
        return {"input_tokens": 10, "output_tokens": 5}


class _AnthropicMessage:
    def __init__(self):
        self.content = [
            _AnthropicBlock(
                {
                    "type": "thinking",
                    "thinking": "private",
                    "signature": "anthropic-signature",
                }
            ),
            _AnthropicBlock(
                {
                    "type": "tool_use",
                    "id": "call_a",
                    "name": "search",
                    "input": {"query": "x"},
                }
            ),
        ]
        self.stop_reason = "tool_use"
        self.usage = _AnthropicUsage()


class _AnthropicStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        async def _events():
            if False:
                yield None

        return _events()

    async def get_final_message(self):
        return self.message


class _AnthropicClient:
    def __init__(self):
        self.calls = 0
        outer = self

        class _Messages:
            def stream(inner, **payload):
                outer.calls += 1
                return _AnthropicStream(_AnthropicMessage())

        self.messages = _Messages()


def test_anthropic_streams_one_native_call_and_sidecars_signed_blocks():
    pipe = anthropic_mod.Pipe()
    client = _AnthropicClient()
    pipe._make_client = lambda api_key: client

    chunks = asyncio.run(
        _collect(
            pipe._stream_native_once(
                payload={"model": "claude-test", "messages": []},
                client_tool_names={"search"},
                api_key="test",
                emit=lambda event: asyncio.sleep(0),
            )
        )
    )

    assert client.calls == 1
    detail = next(
        chunk["choices"][0]["delta"]["reasoning_details"][0]
        for chunk in chunks
        if chunk.get("choices", [{}])[0].get("delta", {}).get("reasoning_details")
    )
    assert detail["type"] == "anthropic"
    assert detail["content"][0]["signature"] == "anthropic-signature"
    tool_delta = next(
        chunk
        for chunk in chunks
        if chunk.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
    )
    assert tool_delta["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_a"


def test_anthropic_history_restores_signed_blocks_before_tool_result():
    pipe = anthropic_mod.Pipe()
    native_blocks = [
        {
            "type": "thinking",
            "thinking": "private",
            "signature": "anthropic-signature",
        },
        {
            "type": "tool_use",
            "id": "call_a",
            "name": "search",
            "input": {"query": "x"},
        },
    ]
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {
                    "type": "anthropic",
                    "index": 0,
                    "content": native_blocks,
                }
            ],
            "tool_calls": [
                {
                    "id": "call_a",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"x"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "result"},
    ]

    _, converted = asyncio.run(pipe._convert_messages(messages, memory_enabled=False))

    assert converted[0] == {"role": "assistant", "content": native_blocks}
    assert converted[1]["role"] == "user"
    assert converted[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_a",
        "content": "result",
    }
