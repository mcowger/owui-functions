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

        NOTE: `blocks` is never hard-capped in count, but each fold merges the
        oldest two blocks into one before adding a new one, so the list grows
        by at most one net entry per fold and old summaries get progressively
        coarser rather than re-summarized in full every time.
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

        effective_recent_n = recent_n
        if force:
            units = atomic_message_units(history_messages)
            effective_recent_n = (
                n - units[-1][0] if preserve_latest_unit and units else 0
            )

        anchor_n, recent_start = tool_safe_window_boundaries(
            history_messages,
            anchor_count=anchor_n,
            recent_count=effective_recent_n,
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
            fold_ceiling = int(self.valves.fold_summary_max_tokens)

            # Merge only the oldest two blocks (not the whole list) so a
            # long-lived chat's summaries get progressively coarser instead of
            # being fully re-summarized - and re-degraded - on every fold.
            if len(blocks) > 1:
                oldest_two_messages = block_messages(blocks[:2])
                base_tokens = self.count_messages_tokens(anchor + pending + recent)
                block_target = max(
                    64,
                    min(fold_ceiling, max(64, budget - base_tokens)),
                )
                merged = await self.generate_overflow_summary(
                    oldest_two_messages,
                    target_tokens=block_target,
                    max_output_tokens=fold_ceiling,
                )
                if merged:
                    blocks = [{"text": merged}] + blocks[2:]
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
                projected = self.count_messages_tokens(base) + fold_ceiling
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
                    min(fold_ceiling, max(64, budget - base_tokens)),
                )
                summary_text = await self.generate_overflow_summary(
                    excluded,
                    target_tokens=summary_target,
                    max_output_tokens=fold_ceiling,
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
