import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from owui_manifolds.filters.context_window import ContextWindowMixin


class ContextWindowHarness(ContextWindowMixin):
    def __init__(self):
        self.valves = SimpleNamespace(
            enable_overflow_summary=True,
            summary_max_tokens=1_000,
            fold_summary_max_tokens=4_000,
        )
        self.summarized_messages = []

    def count_messages_tokens(self, messages):
        # The force path must fold even when the assembled candidate fits.
        return 0

    async def generate_overflow_summary(self, messages, *, target_tokens, max_output_tokens=None):
        self.summarized_messages = messages
        return "Compressed middle"


def test_user_approved_early_compression_ignores_recent_tail():
    """A confirmation must not become a no-op when defaults keep all history recent."""
    harness = ContextWindowHarness()
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]

    candidate, covered_upto, blocks, stats = asyncio.run(
        harness.build_context_window(
            history_messages=history,
            budget=100_000,
            anchor_n=2,
            recent_n=20,
            covered_upto=2,
            blocks=[],
            force=True,
            preserve_latest_unit=False,
        )
    )

    assert harness.summarized_messages == history[2:]
    assert covered_upto == len(history)
    assert blocks == [{"text": "Compressed middle"}]
    assert stats == {
        "kept": 2,
        "summarized_count": 2,
        "block_count": 1,
        "folded": True,
    }
    assert candidate == [
        *history[:2],
        {
            "role": "system",
            "content": "[Summary of earlier conversation]\nCompressed middle",
        },
    ]


def test_normal_window_still_keeps_the_configured_recent_tail():
    harness = ContextWindowHarness()
    history = [{"role": "user", "content": str(index)} for index in range(4)]

    candidate, covered_upto, blocks, stats = asyncio.run(
        harness.build_context_window(
            history_messages=history,
            budget=100_000,
            anchor_n=2,
            recent_n=20,
            covered_upto=2,
            blocks=[],
        )
    )

    assert harness.summarized_messages == []
    assert candidate == history
    assert covered_upto == 2
    assert blocks == []
    assert stats["folded"] is False
