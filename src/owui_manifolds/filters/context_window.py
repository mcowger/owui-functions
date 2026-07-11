"""Context window selection and early-compression prompting."""

from typing import Any, Callable, Dict, List, Tuple

from owui_manifolds.filters.context_tooling import (
    atomic_message_units,
    tool_safe_window_boundaries,
)


class ContextWindowMixin:
    async def _ask_compress_now(
        self, event_call: Callable, history_tokens: int, budget: int
    ) -> bool:
        """Blocks (via the client websocket) until the user answers a Yes/Cancel
        confirmation dialog, or times out. Any non-`True` result (decline,
        timeout, disconnected session) is treated as "don't compress yet"."""
        percent_used = int(history_tokens / budget * 100) if budget else 100
        try:
            result = await event_call(
                {
                    "type": "confirmation",
                    "data": {
                        "title": "Context usage warning",
                        "message": (
                            f"Conversation history is at {history_tokens:,} tokens "
                            f"({percent_used}% of your {budget:,}-token budget). Compress "
                            "older messages now to free up space, or continue as-is?"
                        ),
                    },
                }
            )
        except Exception as e:
            self.debug_log(1, f"Warning-threshold confirmation call failed: {e}", "⚠️")
            return False
        return result is True

    async def build_context_window(
        self,
        history_messages: List[dict],
        budget: int,
        anchor_n: int,
        recent_n: int,
        covered_upto: int,
        blocks: List[Dict[str, str]],
        force: bool = False,
        preserve_latest_unit: bool = True,
    ) -> Tuple[List[dict], int, List[Dict[str, str]], Dict[str, Any]]:
        """Assemble anchor + persisted summary blocks + verbatim "pending" middle +
        recent messages, and compare THAT candidate to budget - not the raw
        history size, which never shrinks and would re-trigger this on every turn.

        Only when the candidate still doesn't fit do we fold all of `pending`
        into one new, permanently-frozen summary block. Blocks are generated
        exactly once and never re-summarized, so a long conversation accumulates
        a short, append-only list of summaries instead of repeatedly
        re-summarizing (and degrading) the same content.

        `force=True` folds `pending` immediately regardless of whether the
        candidate fits the budget - used when the user opts to compress early
        via the warning-threshold prompt, ahead of the hard cap.

        NOTE: `blocks` is intentionally left unbounded here - it is never capped
        or merged. For a pathologically long-lived chat (many fold events) this
        list would itself keep growing the token cost of the "blocks" section.
        Deferred for now per explicit decision; revisit with e.g. a
        max-block-count valve that merges the oldest two blocks if this ever
        becomes a real problem in practice.
        """
        n = len(history_messages)
        if n == 0:
            return (
                [],
                covered_upto,
                blocks,
                {
                    "kept": 0,
                    "summarized_count": 0,
                    "block_count": len(blocks),
                    "folded": False,
                },
            )

        anchor_n, recent_start = tool_safe_window_boundaries(
            history_messages,
            anchor_count=anchor_n,
            recent_count=recent_n,
        )
        covered_upto = max(anchor_n, min(covered_upto, recent_start))
        covered_upto = max(
            anchor_n,
            tool_safe_window_boundaries(
                history_messages,
                anchor_count=covered_upto,
                recent_count=n - recent_start,
            )[0],
        )

        anchor = history_messages[:anchor_n]
        pending = history_messages[covered_upto:recent_start]
        recent = history_messages[recent_start:]

        def block_messages(block_list: List[Dict[str, str]]) -> List[dict]:
            return [
                {
                    "role": "system",
                    "content": f"[Summary of earlier conversation]\n{b['text']}",
                }
                for b in block_list
            ]

        candidate = anchor + block_messages(blocks) + pending + recent
        folded = False
        over_budget = self.count_messages_tokens(candidate) > budget

        if self.valves.enable_overflow_summary and (force or over_budget):
            # Old state can contain one map summary per former 24K chunk. Merge
            # those blocks first so persisted summaries cannot dominate context.
            existing_block_messages = block_messages(blocks)
            if existing_block_messages and (
                len(blocks) > 1
                or self.count_messages_tokens(existing_block_messages)
                > int(self.valves.summary_max_tokens)
            ):
                base_tokens = self.count_messages_tokens(anchor + pending + recent)
                block_target = max(
                    64,
                    min(
                        int(self.valves.summary_max_tokens),
                        max(64, budget - base_tokens),
                    ),
                )
                merged = await self.generate_overflow_summary(
                    existing_block_messages,
                    target_tokens=block_target,
                )
                if merged:
                    blocks = [{"text": merged}]
                    folded = True

            excluded = list(pending)
            fold_end = recent_start
            remaining_recent = list(recent)

            # Recent-message retention is a preference, not a hard guarantee.
            # Move the oldest complete recent exchanges into this fold until a
            # bounded summary plus the remaining request can fit. Always retain
            # the newest atomic exchange for the provider continuation.
            units = atomic_message_units(remaining_recent)
            minimum_units = 1 if preserve_latest_unit else 0
            while len(units) > minimum_units:
                base = anchor + block_messages(blocks) + remaining_recent
                projected = self.count_messages_tokens(base) + int(
                    self.valves.summary_max_tokens
                )
                if projected <= budget:
                    break
                start, end = units[0]
                excluded.extend(remaining_recent[start:end])
                fold_end += end - start
                remaining_recent = remaining_recent[end:]
                units = atomic_message_units(remaining_recent)

            if excluded:
                base_tokens = self.count_messages_tokens(
                    anchor + block_messages(blocks) + remaining_recent
                )
                summary_target = max(
                    64,
                    min(
                        int(self.valves.summary_max_tokens),
                        max(64, budget - base_tokens),
                    ),
                )
                summary_text = await self.generate_overflow_summary(
                    excluded,
                    target_tokens=summary_target,
                )
                if summary_text:
                    blocks = blocks + [{"text": summary_text}]
                    covered_upto = fold_end
                    folded = True
                    pending = []
                    recent = remaining_recent

            candidate = anchor + block_messages(blocks) + pending + recent

        stats = {
            "kept": len(anchor) + len(pending) + len(recent),
            "summarized_count": covered_upto - anchor_n,
            "block_count": len(blocks),
            "folded": folded,
        }
        return candidate, covered_upto, blocks, stats
