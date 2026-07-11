"""Shared Context Manager library and recursive-pipe regression tests."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_ROOT))

from owui_manifolds.filters import context_summary as context_summary_module
from owui_manifolds.filters.context import ContextManager, Filter
from owui_manifolds.filters.context_marker import (
    context_is_prepared,
    mark_context_prepared,
)
from owui_manifolds.filters.context_runtime import prepare_context_for_pipe
from owui_manifolds.filters.context_tooling import (
    ContextBudgetExceededError,
    tool_safe_window_boundaries,
)
from owui_manifolds.filters.context_valves import ContextUserValves


def _parallel_tool_messages(result_chars: int = 20_000) -> list[dict]:
    calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": "search", "arguments": "{}"},
        }
        for index in range(4)
    ]
    return [
        {"role": "user", "content": "Research the issue."},
        {"role": "assistant", "content": "", "tool_calls": calls},
        *[
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "content": chr(ord("a") + index) * result_chars,
            }
            for index in range(4)
        ],
    ]


def test_idempotence_marker_is_bound_to_structured_messages():
    assert issubclass(Filter, ContextManager)
    body = {"messages": _parallel_tool_messages(10)}
    mark_context_prepared(body)

    assert "_owui_context_prepared" not in body
    assert context_is_prepared(body)
    body["messages"].append({"role": "assistant", "content": "new iteration"})
    assert not context_is_prepared(body)

    mark_context_prepared(body)
    body["tools"] = [{"type": "function", "function": {"name": "search"}}]
    assert not context_is_prepared(body)


def test_pipe_adapter_marks_the_original_recursive_form_data_object():
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    metadata: dict = {}

    prepared = asyncio.run(
        prepare_context_for_pipe(
            body,
            model_id="test-model",
            user=None,
            chat_id=None,
            metadata=metadata,
        )
    )

    assert prepared is body
    assert "metadata" not in body
    assert context_is_prepared(body, metadata)


def test_recent_window_never_splits_parallel_tool_exchange():
    messages = _parallel_tool_messages(10)

    anchor_end, recent_start = tool_safe_window_boundaries(
        messages, anchor_count=0, recent_count=2
    )

    assert anchor_end == 0
    assert recent_start == 1  # assistant call plus all four tool results


def test_filter_compacts_oversized_tool_results_and_is_idempotent():
    manager = ContextManager()
    manager.valves = manager.valves.model_copy(
        update={
            "model_token_table": "*,1000,100,80",
            "min_target_tokens": 100,
            "response_buffer_percent": 1,
            "response_buffer_min": 0,
            "response_buffer_max": 0,
            "enable_warning_prompt": False,
            "api_key": "test",
        }
    )
    summarized_inputs: list[str] = []

    async def summarize(message: dict, target_tokens=None) -> str:
        content = manager.extract_text_from_content(message.get("content", ""))
        summarized_inputs.append(content)
        return f"Summary preserving result prefix {content[:1]}."

    manager.generate_tool_result_summary = summarize

    async def summarize_history(messages, target_tokens=None):
        return "Summary of the original user instruction."

    manager.generate_overflow_summary = summarize_history
    user = {
        "valves": ContextUserValves(
            context_target_percent=100,
            anchor_message_count=0,
            recent_message_count=20,
        )
    }
    body = {"model": "test-model", "messages": _parallel_tool_messages()}

    first = asyncio.run(
        manager.inlet(
            body,
            user=user,
            __model__={"info": {"base_model_id": "test-model"}},
        )
    )
    first_call_count = len(summarized_inputs)
    second = asyncio.run(
        manager.inlet(
            first,
            user=user,
            __model__={"info": {"base_model_id": "test-model"}},
        )
    )

    assert first_call_count > 0
    assert all(len(content) == 20_000 for content in summarized_inputs)
    assert len(summarized_inputs) == first_call_count
    assert second is first
    tool_messages = [
        message for message in first["messages"] if message["role"] == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_0",
        "call_1",
        "call_2",
        "call_3",
    ]
    assert any(
        message["content"].startswith("[Semantically compacted tool result")
        for message in tool_messages
    )
    # Hard-budget folding may replace the original user prompt with a system
    # summary, but it must keep the active assistant/tool exchange contiguous.
    roles = [message["role"] for message in first["messages"]]
    assistant_index = roles.index("assistant")
    assert roles[assistant_index + 1 :] == ["tool", "tool", "tool", "tool"]

    # OWUI carries the old top-level marker into its recursive request, but the
    # changed message fingerprint must cause the new tool batch to be budgeted.
    first["messages"].extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_next",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_next",
                "content": "z" * 20_000,
            },
        ]
    )
    recursive = asyncio.run(
        manager.inlet(
            first,
            user=user,
            __model__={"info": {"base_model_id": "test-model"}},
        )
    )
    assert len(summarized_inputs) > first_call_count
    assert context_is_prepared(recursive)


def test_live_sized_parallel_results_are_semantically_compacted_to_model_budget():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    summarized_sizes: list[int] = []

    async def summarize(message: dict, target_tokens=None) -> str:
        content = manager.extract_text_from_content(message.get("content", ""))
        summarized_sizes.append(len(content))
        return f"Summary of {len(content):,} source characters."

    manager.generate_tool_result_summary = summarize
    messages = _parallel_tool_messages(850_000)

    compacted, _, stats = asyncio.run(
        manager.compact_tool_results_to_budget(messages, budget=460_000)
    )

    assert summarized_sizes == [850_000, 850_000]
    assert stats["compacted"] == 2
    assert stats["final_tokens"] <= 460_000
    assert [
        message.get("tool_call_id")
        for message in compacted
        if message["role"] == "tool"
    ] == [
        "call_0",
        "call_1",
        "call_2",
        "call_3",
    ]


def test_legacy_rendered_details_are_removed_before_budgeting():
    manager = ContextManager()
    manager.valves = manager.valves.model_copy(
        update={
            "model_token_table": "*,1000,100,80",
            "response_buffer_min": 0,
            "response_buffer_max": 0,
        }
    )
    body = {
        "model": "test",
        "messages": [
            {
                "role": "assistant",
                "content": (
                    "visible before"
                    + '<details type="tool_calls">'
                    + ("x" * 1_000_000)
                    + "</details>visible after"
                ),
            },
            {"role": "user", "content": "continue"},
        ],
    }

    prepared = asyncio.run(
        manager.inlet(
            body,
            user={"valves": ContextUserValves(context_target_percent=100)},
            __model__={"info": {"base_model_id": "test"}},
        )
    )

    assert prepared["messages"][0]["content"] == "visible beforevisible after"
    assert manager._summary_calls_used == 0


def test_hard_budget_can_fold_recent_messages_and_converge():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    manager.valves = manager.valves.model_copy(
        update={
            "model_token_table": "*,2000,100,80",
            "response_buffer_min": 0,
            "response_buffer_max": 0,
            "summary_max_tokens": 100,
        }
    )
    folded_sizes = []

    async def summarize(messages, target_tokens=None):
        folded_sizes.append((len(messages), target_tokens))
        return "bounded history summary"

    manager.generate_overflow_summary = summarize
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 1000}
        for index in range(12)
    ]
    body = {
        "model": "test",
        "messages": [*history, {"role": "user", "content": "current"}],
    }

    prepared = asyncio.run(
        manager.inlet(
            body,
            user={
                "valves": ContextUserValves(
                    context_target_percent=100,
                    anchor_message_count=0,
                    recent_message_count=20,
                )
            },
            __model__={"info": {"base_model_id": "test"}},
        )
    )

    assert folded_sizes
    assert any(message["role"] == "system" for message in prepared["messages"])
    assert manager.count_messages_tokens(prepared["messages"]) <= 2000


def test_uncompacted_request_is_refused_before_provider_call():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    manager.valves = manager.valves.model_copy(
        update={
            "model_token_table": "*,1000,100,80",
            "response_buffer_min": 0,
            "response_buffer_max": 0,
        }
    )
    body = {
        "model": "test",
        "messages": [{"role": "user", "content": "x" * 10_000}],
    }

    with pytest.raises(ContextBudgetExceededError, match="could not fit"):
        asyncio.run(
            manager.inlet(
                body,
                user={"valves": ContextUserValves(context_target_percent=100)},
                __model__={"info": {"base_model_id": "test"}},
            )
        )


def test_summary_backend_loads_credentials_from_installed_pipe(monkeypatch):
    open_webui = types.ModuleType("open_webui")
    models = types.ModuleType("open_webui.models")
    functions = types.ModuleType("open_webui.models.functions")
    open_webui.__path__ = []
    models.__path__ = []

    class _Functions:
        @staticmethod
        async def get_function_valves_by_id(function_id):
            assert function_id == "openai_responses_manifold"
            return {"BASE_URL": "https://provider.invalid/v1", "API_KEY": "secret"}

    functions.Functions = _Functions
    open_webui.models = models
    models.functions = functions
    monkeypatch.setitem(sys.modules, "open_webui", open_webui)
    monkeypatch.setitem(sys.modules, "open_webui.models", models)
    monkeypatch.setitem(sys.modules, "open_webui.models.functions", functions)

    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(context_summary_module, "AsyncOpenAI", fake_client)
    manager = ContextManager()
    manager.valves = manager.valves.model_copy(
        update={
            "api_key": "",
            "summary_provider_function_id": "openai_responses_manifold",
        }
    )

    client = asyncio.run(manager.get_api_client())

    assert client is not None
    assert captured["base_url"] == "https://provider.invalid/v1"
    assert captured["api_key"] == "secret"


def test_native_responses_summary_backend():
    manager = ContextManager()
    manager.valves = manager.valves.model_copy(
        update={
            "api_key": "test",
            "summary_api_style": "responses",
            "summary_model": "gpt-test",
        }
    )
    requests: list[dict] = []

    class _Responses:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(output_text="native response summary")

    manager.get_api_client = lambda: SimpleNamespace(responses=_Responses())

    summary = asyncio.run(
        manager._summarize_text(
            "source text",
            system_prompt="Summarize everything.",
            call_name="test native summary",
        )
    )

    assert summary == "native response summary"
    assert requests[0]["model"] == "gpt-test"
    assert requests[0]["instructions"] == "Summarize everything."
    assert requests[0]["input"].endswith("source text")
    assert requests[0]["store"] is False


def test_chunk_safe_summary_processes_all_source_chunks():
    manager = ContextManager()
    manager.valves = manager.valves.model_copy(
        update={
            "api_key": "test",
            "summary_max_tokens": 50,
            "summary_input_max_tokens": 60_000,
        }
    )
    calls: list[str] = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs["messages"][1]["content"])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"summary-{len(calls)}")
                    )
                ]
            )

    manager.get_api_client = lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    manager.token_calculator.get_encoding = lambda: None
    source = "a" * (60_000 * 4) + "UNIQUE_TAIL"

    summary = asyncio.run(
        manager._summarize_text(
            source,
            system_prompt="Summarize everything.",
            call_name="test summary",
        )
    )

    assert len(calls) == 5
    assert "UNIQUE_TAIL" in calls[-1]
    assert summary == "\n\n".join(f"summary-{index}" for index in range(1, 6))


def test_default_summary_chunk_size_is_250k_tokens():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    source = "x" * 500_001

    chunks = manager._summary_input_chunks(source)

    assert list(map(len, chunks)) == [250_000, 250_000, 1]


def test_map_summaries_are_reduced_to_assigned_target():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    manager.valves = manager.valves.model_copy(
        update={
            "api_key": "test",
            "summary_input_max_tokens": 1000,
            "summary_max_tokens": 100,
        }
    )
    calls = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) <= 3:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content="m" * 300))
                    ]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="reduced"))]
            )

    manager.get_api_client = lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )

    summary = asyncio.run(
        manager._summarize_text(
            "x" * 2500,
            system_prompt="Summarize.",
            call_name="reduction test",
            target_tokens=100,
        )
    )

    assert summary == "reduced"
    assert len(calls) == 4
    assert all(call["max_tokens"] == 100 for call in calls)


def test_summary_call_safety_budget_stops_unbounded_fanout():
    manager = ContextManager()
    manager.token_calculator.get_encoding = lambda: None
    manager.valves = manager.valves.model_copy(
        update={
            "api_key": "test",
            "summary_input_max_tokens": 1000,
            "max_summary_calls_per_request": 2,
        }
    )
    calls = []

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
            )

    manager.get_api_client = lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )

    summary = asyncio.run(
        manager._summarize_text(
            "x" * 2500,
            system_prompt="Summarize.",
            call_name="safety test",
        )
    )

    assert summary is None
    assert len(calls) == 2
    assert manager._summary_calls_used == 2
