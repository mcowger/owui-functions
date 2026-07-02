"""
Unit tests for gemini.py streaming (Pipe._stream_response).

These verify the migration from a single blocking generate_content call to
real token streaming via generate_content_stream:

  * visible text is emitted as incremental deltas (live streaming), not one
    lump at the end;
  * thought parts are aggregated into a single <details type="reasoning">
    block and emitted before the following visible text;
  * function_call parts are passed through to the caller's tool loop and are
    preserved (with thought_signature) in the aggregated model Content so
    context survives across tool iterations;
  * grounding metadata chunks are captured for source emission.

Run:
    uv run --with google-genai --with pydantic --with pytest \\
        pytest tests/test_gemini_streaming.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


gemini_mod = _load_module("gemini.py", "gemini_mod_streaming_test")
from google.genai import types  # noqa: E402


class _CollectingEvents:
    def __init__(self):
        self.deltas: list[str] = []

    async def delta(self, content):
        self.deltas.append(content)

    async def status(self, *a, **k):
        pass

    async def source(self, data):
        pass

    async def chat_completion(self, data):
        pass

    async def notification(self, *a, **k):
        pass


def _chunk(*parts, grounding=None):
    candidate = types.Candidate(
        content=types.Content(role="model", parts=list(parts)),
        grounding_metadata=grounding,
    )
    return types.GenerateContentResponse(candidates=[candidate])


class _StubClient:
    """Mimics client.models.generate_content_stream (a coroutine returning an async iterator)."""

    def __init__(self, chunks):
        self._chunks = chunks

        class _Models:
            async def generate_content_stream(inner, *, model, contents, config):
                async def _gen():
                    for c in chunks:
                        yield c

                return _gen()

        self.models = _Models()


def _run_stream(chunks, *, emit=True, use_code_execution=False):
    pipe = gemini_mod.Pipe()
    events = _CollectingEvents()
    client = _StubClient(chunks)
    result = asyncio.run(
        pipe._stream_response(
            client=client,
            model_id="gemini-3.5-flash",
            contents=[],
            config=types.GenerateContentConfig(),
            events=events,
            emit=emit,
            use_code_execution=use_code_execution,
        )
    )
    return result, events


def test_visible_text_streams_incrementally():
    chunks = [
        _chunk(types.Part.from_text(text="Hello ")),
        _chunk(types.Part.from_text(text="world")),
    ]
    result, events = _run_stream(chunks)
    # Each visible text part was emitted as its own delta, in order.
    assert events.deltas == ["Hello ", "world"]
    assert result.final_text == "Hello world"
    assert result.final_text_emitted is True


def test_thoughts_aggregated_before_text():
    chunks = [
        _chunk(types.Part(text="reasoning bit one ", thought=True)),
        _chunk(types.Part(text="reasoning bit two", thought=True)),
        _chunk(types.Part.from_text(text="Answer")),
    ]
    result, events = _run_stream(chunks)
    # First delta is the aggregated reasoning <details> block, then the answer.
    assert len(events.deltas) == 2
    assert events.deltas[0].startswith('<details type="reasoning"')
    assert "reasoning bit one reasoning bit two" in events.deltas[0]
    assert events.deltas[1] == "Answer"
    assert result.final_text == "Answer"


def test_function_call_passthrough_and_signature_preserved():
    fc = types.Part(
        function_call=types.FunctionCall(name="do_it", args={"x": 1}, id="c1"),
        thought_signature=b"sig-abc",
    )
    chunks = [
        _chunk(types.Part.from_text(text="calling tool")),
        _chunk(fc),
    ]
    result, events = _run_stream(chunks)
    # The function call itself is NOT rendered as visible text.
    assert events.deltas == ["calling tool"]
    # It is exposed for the tool loop.
    assert [c.name for c in result.tool_calls] == ["do_it"]
    # The streamed chunk Content is kept verbatim (never merged), so the
    # function_call part retains its thought_signature for the next turn.
    all_parts = [p for c in result.model_contents for p in (c.parts or [])]
    fc_parts = [p for p in all_parts if p.function_call is not None]
    assert len(fc_parts) == 1
    assert fc_parts[0].thought_signature == b"sig-abc"
    # The chunk carrying the function_call must itself carry the signature
    # (i.e. we did not detach it by rebuilding a single Content).
    fc_chunk = next(c for c in result.model_contents if any(p.function_call for p in (c.parts or [])))
    sig_in_chunk = next(p for p in fc_chunk.parts if p.function_call is not None)
    assert sig_in_chunk.thought_signature == b"sig-abc"


def test_grounding_metadata_captured():
    grounding = types.GroundingMetadata(grounding_chunks=[])
    chunks = [_chunk(types.Part.from_text(text="grounded"), grounding=grounding)]
    result, _ = _run_stream(chunks)
    assert result.grounding_response is not None


def test_emit_false_suppresses_deltas_but_returns_text():
    chunks = [_chunk(types.Part.from_text(text="quiet"))]
    result, events = _run_stream(chunks, emit=False)
    assert events.deltas == []
    assert result.final_text == "quiet"
    assert result.final_text_emitted is False
