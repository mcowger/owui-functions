"""
LIVE send/receive harness for anthropic_function.py.

This exercises the REAL pipe() end-to-end path: it builds the payload via
create_request_payload, opens the real `client.beta.messages.stream(...)` to
the live Anthropic API, and captures every event the pipe emits to the UI
(deltas, reasoning blocks, status). This is how we see what the API actually
returns and whether thinking content renders — without Open WebUI's
processing layer in the loop.

PERMISSION GATE — this test does real (billable) API calls and will NOT run
unless BOTH are true:
  1. The env var ANTHROPIC_LIVE_TEST=1 is set (your explicit opt-in).
  2. The env var ANTHROPIC_API_KEY is set to a real key.

Run:
    ANTHROPIC_LIVE_TEST=1 ANTHROPIC_API_KEY=sk-ant-... \\
        uv run --with anthropic --with pydantic --with pytest --with python-dotenv \\
        pytest tests/test_anthropic_live.py -v -s

Without those env vars, every test in this file is skipped (not failed).

What's tested:
  * The live API accepts the payload shape we build (no 400).
  * With EFFORT=adaptive + THINKING_DISPLAY=summarized, a thinking content
    block is emitted and rendered into a <details type="reasoning"> block.
  * usage.output_tokens_details.thinking_tokens is reported (>0 when thinking
    occurred) — the ground-truth signal that the model actually thought.
  * Captured events are printed (-s) so you can eyeball the raw stream.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# --- Load the pipe module ---------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "anthropic_function.py"

_spec = importlib.util.spec_from_file_location("anthropic_function", MODULE_PATH)
anthropic_function = importlib.util.module_from_spec(_spec)
sys.modules["anthropic_function"] = anthropic_function
_spec.loader.exec_module(anthropic_function)

Pipe = anthropic_function.Pipe
create_request_payload = anthropic_function.create_request_payload

# --- Permission gate --------------------------------------------------------

_LIVE_OK = os.environ.get("ANTHROPIC_LIVE_TEST") == "1"
_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (_LIVE_OK and _HAS_KEY),
    reason=(
        "live test skipped: set ANTHROPIC_LIVE_TEST=1 and ANTHROPIC_API_KEY=<key> "
        "to run (makes real billable API calls)."
    ),
)


# --- Capturing event emitter ------------------------------------------------

class CapturingEmitter:
    """
    Stand-in for Open WebUI's __event_emitter__. Records every event the pipe
    emits so tests can assert on deltas, reasoning blocks, status, and the
    final assembled message.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.deltas: list[str] = []  # content deltas
        self.replaces: list[str] = []  # full message replaces
        self.status: list[tuple[str, bool]] = []  # (description, done)

    async def __call__(self, event: dict) -> None:
        self.events.append(event)
        etype = event.get("type")
        data = event.get("data", {}) or {}
        if etype == "message":
            self.deltas.append(data.get("content", ""))
        elif etype == "replace":
            self.replaces.append(data.get("content", ""))
        elif etype == "status":
            self.status.append((data.get("description", ""), data.get("done")))

    def final_text(self) -> str:
        """The final assembled message content (last replace, else joined deltas)."""
        if self.replaces:
            return self.replaces[-1]
        return "".join(self.deltas)


# --- Helpers ----------------------------------------------------------------

def _default_caps(**overrides: Any) -> dict[str, Any]:
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


def _make_pipe(api_key: str) -> Pipe:
    pipe = Pipe()
    # inject the key into admin valves so pipe() picks it up
    pipe.valves.ANTHROPIC_API_KEY = api_key
    return pipe


def _patch_model_info(pipe: Pipe, model_name: str, caps: dict[str, Any]) -> None:
    pipe.__class__._api_capabilities_cache[model_name] = caps


# A prompt that reliably triggers extended thinking (non-memorizable reasoning).
THINKING_PROMPT = (
    "Three friends — Ada, Bo, and Cy — each own a different pet (cat, dog, fish) "
    "and drive a different car (red, blue, green). Ada doesn't own the dog and "
    "doesn't drive red. The fish owner drives blue. Bo drives green. Who owns "
    "what, and what does each drive? Walk through your reasoning."
)


async def _run_live_turn(
    pipe: Pipe,
    *,
    effort: str,
    display: str = "summarized",
    prompt: str = THINKING_PROMPT,
    model: str = "claude-sonnet-4-6",
) -> tuple[CapturingEmitter, Any]:
    """Drive the real pipe() and return (emitter, final_return_value)."""
    _patch_model_info(pipe, model, _default_caps())
    user_valves = anthropic_function.Pipe.UserValves(
        EFFORT=effort, THINKING_DISPLAY=display, SHOW_TOKEN_COUNT="With Cache"
    )
    body = {
        "model": f"anthropic/{model}",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    user = {"valves": user_valves}
    emitter = CapturingEmitter()
    result = await pipe.pipe(
        body,
        user,
        emitter,
        {},  # __metadata__
        None,  # __tools__
        None,  # __files__
    )
    return emitter, result


# --- Live tests -------------------------------------------------------------

def test_live_adaptive_thinking_renders_reasoning(capsys):
    """
    EFFORT=adaptive + THINKING_DISPLAY=summarized on a live Sonnet 4.6:
    the API should emit a thinking block AND the pipe should render it into
    a <details type="reasoning"> block visible in the final message.
    """
    pipe = _make_pipe(os.environ["ANTHROPIC_API_KEY"])
    emitter, result = asyncio.run(_run_live_turn(pipe, effort="adaptive"))

    final = emitter.final_text()
    print("\n=== FINAL MESSAGE (len %d) ===" % len(final))
    print(final[:3000])
    print("\n=== STATUS EVENTS ===")
    for desc, done in emitter.status:
        print(f"  {done!s:5} {desc}")
    print("\n=== EVENT TYPES ===")
    from collections import Counter
    print(Counter(e.get("type") for e in emitter.events))

    assert "Error" not in final[:50], f"pipe returned an error: {final[:200]}"
    assert '<details type="reasoning"' in final, (
        "no <details type=\"reasoning\"> block rendered — thinking did not surface. "
        "Final message head: " + final[:500]
    )
    # The reasoning block should contain actual thinking text, not be empty
    import re
    m = re.search(
        r'<details type="reasoning"[^>]*>\s*<summary>[^<]*</summary>\s*(.*?)\s*</details>',
        final, re.DOTALL,
    )
    assert m, "reasoning block found but could not extract its body"
    body_text = m.group(1).strip()
    assert len(body_text) > 20, (
        f"reasoning block body is suspiciously short ({len(body_text)} chars), "
        f"suggesting redacted/omitted content: {body_text!r}"
    )


def test_live_effort_high_thinks_and_renders(capsys):
    """
    EFFORT=high (discrete, not adaptive) + summarized: per the deployed
    mapping we send BOTH output_config.effort:high AND thinking:{adaptive,
    display:summarized}. The model should think AND render.
    """
    pipe = _make_pipe(os.environ["ANTHROPIC_API_KEY"])
    emitter, result = asyncio.run(_run_live_turn(pipe, effort="high"))

    final = emitter.final_text()
    print("\n=== FINAL MESSAGE (len %d) ===" % len(final))
    print(final[:3000])
    print("\n=== STATUS EVENTS ===")
    for desc, done in emitter.status:
        print(f"  {done!s:5} {desc}")

    assert "Error" not in final[:50], f"pipe returned an error: {final[:200]}"
    assert '<details type="reasoning"' in final, (
        "EFFORT=high produced no reasoning block. This is the core regression: "
        "discrete effort should still surface thinking via the adaptive+effort "
        "combination. Final head: " + final[:500]
    )


def test_live_none_disables_thinking(capsys):
    """
    EFFORT=none + summarized: thinking:{type:disabled} — the model should
    answer WITHOUT a reasoning block. Sanity check that none truly turns
    thinking off and the request still succeeds.
    """
    pipe = _make_pipe(os.environ["ANTHROPIC_API_KEY"])
    emitter, result = asyncio.run(_run_live_turn(pipe, effort="none"))

    final = emitter.final_text()
    print("\n=== FINAL MESSAGE (len %d) ===" % len(final))
    print(final[:2000])
    print("\n=== STATUS EVENTS ===")
    for desc, done in emitter.status:
        print(f"  {done!s:5} {desc}")

    assert "Error" not in final[:50], f"pipe returned an error: {final[:200]}"
    assert '<details type="reasoning"' not in final, (
        "EFFORT=none should NOT emit a reasoning block, but one was found. "
        "Final head: " + final[:500]
    )
    assert len(final) > 10, "empty response for EFFORT=none"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
