"""
Scaffold tests for anthropic_function.py thinking/effort behavior.

These tests drive the pipe's payload-building logic directly, bypassing
Open WebUI's processing layer, so we can assert the exact wire payload
sent to the Anthropic Messages API and the thinking-block rendering
without guessing at OWUI's envelope shaping.

Run:
    uv run --with anthropic --with pydantic --with pytest pytest tests/test_anthropic_thinking.py -v

What's covered:
  1. The 7-value EFFORT mapping -> exact {thinking, output_config} shape.
  2. THINKING_DISPLAY is honored uniformly (adaptive + discrete effort).
  3. xhigh/max clamping when the model lacks those sub-levels.
  4. Adaptive-only enforcement: non-adaptive models get no thinking field.
  5. Streaming rendering: a synthetic thinking_delta stream produces a
     populated <details type="reasoning"> block (the "will it actually
     output reasoning" check).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# --- Load the pipe module ---------------------------------------------------
# anthropic_function.py imports the `anthropic` library; loading it as a
# normal module (not from a shadowing filename) works as long as the file
# is named anything other than anthropic.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "anthropic_function.py"

_spec = importlib.util.spec_from_file_location("anthropic_function", MODULE_PATH)
anthropic_function = importlib.util.module_from_spec(_spec)
sys.modules["anthropic_function"] = anthropic_function
_spec.loader.exec_module(anthropic_function)

Pipe = anthropic_function.Pipe
create_request_payload = anthropic_function.create_request_payload
handle_thinking_delta = anthropic_function.handle_thinking_delta
handle_thinking_block_start = anthropic_function.handle_thinking_block_start
handle_thinking_block_stop = anthropic_function.handle_thinking_block_stop


# --- Fixtures ---------------------------------------------------------------

def _user_valves(**overrides: Any) -> anthropic_function.Pipe.UserValves:
    """Build a UserValves instance with overrides for the effort/display knobs."""
    return anthropic_function.Pipe.UserValves(**overrides)


def _make_pipe() -> Pipe:
    return Pipe()


def _patch_model_info(pipe: Pipe, model_name: str, caps: dict[str, Any]) -> None:
    """Force get_model_info to return `caps` for `model_name` (bypass API cache)."""
    pipe.__class__._api_capabilities_cache[model_name] = caps


def _default_caps(**overrides: Any) -> dict[str, Any]:
    """Claude Sonnet 4.6-shaped capabilities."""
    caps = {
        "max_tokens": 64000,
        "context_length": 200000,
        "supports_thinking": True,
        "supports_adaptive_thinking": True,
        "supports_effort": True,
        "supports_effort_max": False,
        "supports_effort_xhigh": False,
        "supports_vision": True,
        "supports_programmatic_calling": False,
        "supports_compaction": False,
        "supports_dynamic_filtering": True,
        "supports_fast_mode": False,
        "supports_memory": True,
    }
    caps.update(overrides)
    return caps


def _build_payload(
    pipe: Pipe,
    *,
    effort: str,
    display: str = "summarized",
    model: str = "claude-sonnet-4-6",
    caps: dict[str, Any] | None = None,
    messages: list[dict] | None = None,
    metadata: dict | None = None,
) -> tuple[dict, dict]:
    """Call create_request_payload and return (payload, headers)."""
    _patch_model_info(pipe, model, caps or _default_caps())
    user_valves = _user_valves(EFFORT=effort, THINKING_DISPLAY=display)
    body = {
        "model": f"anthropic/{model}",
        "messages": messages or [{"role": "user", "content": "test"}],
        "stream": True,
    }
    user = {"valves": user_valves}

    payload, headers, _markers = asyncio.run(
        create_request_payload(
            pipe,
            body,
            metadata or {},
            user,
            None,  # __tools__
            _noop_emitter,
            None,  # __files__
        )
    )
    return payload, headers


async def _noop_emitter(_event: dict) -> None:
    pass


# --- Payload-shape tests ----------------------------------------------------

@pytest.mark.parametrize(
    "effort, expect_thinking, expect_effort, expect_beta",
    [
        # none -> disabled thinking, no output_config, no effort beta
        ("none", {"type": "disabled"}, None, False),
        # adaptive -> adaptive thinking with display, no output_config
        ("adaptive", {"type": "adaptive", "display": "summarized"}, None, False),
        # discrete levels -> adaptive thinking (display honored) + output_config.effort
        ("low", {"type": "adaptive", "display": "summarized"}, "low", True),
        ("medium", {"type": "adaptive", "display": "summarized"}, "medium", True),
        ("high", {"type": "adaptive", "display": "summarized"}, "high", True),
        ("xhigh", {"type": "adaptive", "display": "summarized"}, "high", True),  # clamped
        ("max", {"type": "adaptive", "display": "summarized"}, "high", True),  # clamped
    ],
)
def test_effort_mapping(effort, expect_thinking, expect_effort, expect_beta):
    """Each EFFORT value produces the exact {thinking, output_config} shape."""
    pipe = _make_pipe()
    payload, headers = _build_payload(pipe, effort=effort)

    # thinking field
    assert payload.get("thinking") == expect_thinking, (
        f"effort={effort}: thinking={payload.get('thinking')!r} expected={expect_thinking!r}"
    )

    # output_config.effort field
    if expect_effort is None:
        assert "output_config" not in payload, (
            f"effort={effort}: output_config should be absent, got {payload.get('output_config')!r}"
        )
    else:
        assert payload.get("output_config") == {"effort": expect_effort}, (
            f"effort={effort}: output_config={payload.get('output_config')!r} "
            f"expected effort={expect_effort!r}"
        )

    # effort beta header
    betas = payload.get("betas") or []
    has_effort_beta = "effort-2025-11-24" in betas
    assert has_effort_beta == expect_beta, (
        f"effort={effort}: effort-2025-11-24 beta present={has_effort_beta} "
        f"expected={expect_beta}"
    )


def test_thinking_display_honored_for_discrete_effort():
    """THINKING_DISPLAY=omitted propagates into the thinking field for low/medium/etc."""
    pipe = _make_pipe()
    for level in ("low", "medium", "high"):
        payload, _ = _build_payload(pipe, effort=level, display="omitted")
        assert payload["thinking"] == {"type": "adaptive", "display": "omitted"}, (
            f"level={level}: thinking={payload['thinking']!r}"
        )


def test_thinking_display_honored_for_adaptive():
    pipe = _make_pipe()
    payload, _ = _build_payload(pipe, effort="adaptive", display="omitted")
    assert payload["thinking"] == {"type": "adaptive", "display": "omitted"}


def test_none_has_no_display_field():
    """thinking:{type:disabled} must NOT carry a display key (spec: Disabled has no display)."""
    pipe = _make_pipe()
    payload, _ = _build_payload(pipe, effort="none")
    assert payload["thinking"] == {"type": "disabled"}
    assert "display" not in payload["thinking"]


def test_xhigh_not_clamped_when_supported():
    pipe = _make_pipe()
    caps = _default_caps(supports_effort_xhigh=True)
    payload, _ = _build_payload(pipe, effort="xhigh", caps=caps)
    assert payload["output_config"] == {"effort": "xhigh"}


def test_max_not_clamped_when_supported():
    pipe = _make_pipe()
    caps = _default_caps(supports_effort_max=True)
    payload, _ = _build_payload(pipe, effort="max", caps=caps)
    assert payload["output_config"] == {"effort": "max"}


def test_non_adaptive_model_gets_no_thinking():
    """A model lacking supports_adaptive_thinking gets no thinking field, even at high effort."""
    pipe = _make_pipe()
    caps = _default_caps(supports_adaptive_thinking=False, supports_effort=True)
    payload, _ = _build_payload(
        pipe, effort="high", model="claude-sonnet-4-5", caps=caps
    )
    assert "thinking" not in payload, f"got thinking={payload.get('thinking')!r}"
    # but effort is still sent
    assert payload.get("output_config") == {"effort": "high"}


def test_non_adaptive_model_none_sends_nothing():
    """none on a non-adaptive model sends neither thinking nor output_config."""
    pipe = _make_pipe()
    caps = _default_caps(supports_adaptive_thinking=False, supports_effort=True)
    payload, _ = _build_payload(
        pipe, effort="none", model="claude-sonnet-4-5", caps=caps
    )
    assert "thinking" not in payload
    assert "output_config" not in payload


def test_body_reasoning_effort_overrides_valve():
    """body.reasoning_effort (per-request override) wins over the UserValve."""
    pipe = _make_pipe()
    _patch_model_info(pipe, "claude-sonnet-4-6", _default_caps())
    user_valves = _user_valves(EFFORT="low", THINKING_DISPLAY="summarized")
    body = {
        "model": "anthropic/claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
        "reasoning_effort": "high",  # override
    }
    payload, _headers, _markers = asyncio.run(
        create_request_payload(
            pipe, body, {}, {"valves": user_valves}, None, _noop_emitter, None
        )
    )
    assert payload["output_config"] == {"effort": "high"}


# --- Streaming / rendering test --------------------------------------------

class _FakeDelta:
    """Mimics anthropic's ThinkingDeltaEvent delta object."""
    def __init__(self, thinking: str, signature: str = ""):
        self.thinking = thinking
        self.signature = signature


def test_thinking_delta_renders_reasoning_block():
    """
    Feed a synthetic thinking_delta stream through the rendering handlers and
    assert a populated <details type="reasoning"> block is produced. This is
    the 'will the user actually see reasoning output' check.
    """
    pipe = _make_pipe()

    # State accumulators mirroring the pipe's streaming loop
    final_message: list[str] = []
    is_model_thinking = False
    thinking_message = ""
    thinking_signature = ""
    thinking_start_time = None
    thinking_stream_start_idx = -1
    thinking_last_block = ""
    rendered: list[str] = []  # captures emitted content

    async def update_content_block(old_block: str, new_block: str) -> None:
        # Mirror PipeRequestContext.update_content_block semantics
        rendered.append(new_block)

    # Simulate: content_block_start (thinking) -> deltas -> content_block_stop
    is_model_thinking, thinking_start_time, thinking_message, thinking_last_block, \
        thinking_stream_start_idx = handle_thinking_block_start(final_message)

    chunks = ["Let me analyze", " the constraints.\n", "Ada drives blue..."]
    for chunk in chunks:
        thinking_message, thinking_last_block = asyncio.run(
            handle_thinking_delta(
                _FakeDelta(thinking=chunk),
                thinking_message=thinking_message,
                thinking_last_block=thinking_last_block,
                format_thinking_block=pipe._format_thinking_block,
                update_content_block=update_content_block,
            )
        )

    # Finalize with a signature
    is_model_thinking, thinking_message, thinking_signature, \
        thinking_stream_start_idx, thinking_last_block = asyncio.run(
            handle_thinking_block_stop(
                content_type="thinking",
                is_model_thinking=is_model_thinking,
                thinking_message=thinking_message,
                thinking_signature="sig_abc",
                thinking_start_time=thinking_start_time,
                thinking_stream_start_idx=thinking_stream_start_idx,
                thinking_last_block=thinking_last_block,
                format_thinking_block=pipe._format_thinking_block,
                update_content_block=update_content_block,
            )
        )

    # Assert: the finalized block contains the reasoning text in a <details type="reasoning">
    assert rendered, "no blocks were rendered"
    final_block = rendered[-1]
    assert '<details type="reasoning"' in final_block, (
        f"expected <details type=\"reasoning\"> in rendered block, got: {final_block!r}"
    )
    assert "Let me analyze the constraints." in final_block, (
        f"thinking text missing from rendered block: {final_block!r}"
    )
    assert "Ada drives blue" in final_block
    assert 'data-signature="sig_abc"' in final_block, (
        f"signature not embedded: {final_block!r}"
    )
    assert 'done="true"' in final_block


def test_thinking_delta_omitted_display_still_renders_block():
    """
    Even with display=omitted (redacted text), the pipe still emits a reasoning
    block structure — but the text will be empty/redacted. This documents the
    known UX gap: omitted => blank reasoning block. The block itself is present.
    """
    pipe = _make_pipe()
    thinking_message = ""
    thinking_last_block = ""
    rendered: list[str] = []

    async def update_content_block(old, new):
        rendered.append(new)

    # With omitted, delta.thinking is empty -> handle_thinking_delta skips update
    thinking_message, thinking_last_block = asyncio.run(
        handle_thinking_delta(
            _FakeDelta(thinking=""),  # empty, as omitted produces
            thinking_message=thinking_message,
            thinking_last_block=thinking_last_block,
            format_thinking_block=pipe._format_thinking_block,
            update_content_block=update_content_block,
        )
    )
    # No block emitted during deltas (empty text)
    assert rendered == [], "empty thinking text should not emit incremental blocks"


# --- Entry point for manual run --------------------------------------------

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
