"""Tool-aware boundaries and semantic compaction for context requests."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, List, Tuple

from owui_manifolds.shared.content import strip_details_blocks

_TOOL_SUMMARY_PREFIX = "[Semantically compacted tool result"
_TOOL_SUMMARY_CACHE: "OrderedDict[str, str]" = OrderedDict()
_TOOL_SUMMARY_CACHE_SIZE = 128


class ContextBudgetExceededError(RuntimeError):
    """Raised instead of sending an oversized request to a provider."""


def strip_legacy_details_from_messages(
    messages: List[dict],
) -> tuple[List[dict], int]:
    """Remove old pipe-rendered UI blocks from assistant request history."""

    normalized = deepcopy(messages)
    removed_chars = 0
    for message in normalized:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            stripped = strip_details_blocks(content)
            removed_chars += len(content) - len(stripped)
            message["content"] = stripped
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {
                    "text",
                    "input_text",
                    "output_text",
                }:
                    continue
                text = str(block.get("text", "") or "")
                stripped = strip_details_blocks(text)
                removed_chars += len(text) - len(stripped)
                block["text"] = stripped
    return normalized, removed_chars


def atomic_message_units(messages: List[dict]) -> List[tuple[int, int]]:
    """Return half-open message ranges that cannot be split safely."""

    units: List[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        end = index + 1
        call_ids = _tool_call_ids(messages[index])
        if call_ids:
            while (
                end < len(messages)
                and messages[end].get("role") == "tool"
                and str(messages[end].get("tool_call_id") or "") in call_ids
            ):
                end += 1
        units.append((index, end))
        index = end
    return units


def _tool_call_ids(message: dict) -> set[str]:
    return {
        str(call.get("id"))
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }


def _tool_group_start(messages: List[dict], index: int) -> int:
    """Move a boundary before the assistant call owning adjacent tool results."""

    if not (0 <= index < len(messages)) or messages[index].get("role") != "tool":
        return index
    result_ids: set[str] = set()
    cursor = index
    while cursor >= 0 and messages[cursor].get("role") == "tool":
        call_id = messages[cursor].get("tool_call_id")
        if call_id:
            result_ids.add(str(call_id))
        cursor -= 1
    if cursor >= 0 and _tool_call_ids(messages[cursor]) & result_ids:
        return cursor
    return index


def tool_safe_window_boundaries(
    messages: List[dict], anchor_count: int, recent_count: int
) -> Tuple[int, int]:
    """Return anchor end/recent start without splitting tool exchanges."""

    count = len(messages)
    anchor_end = max(0, min(anchor_count, count))
    recent_start = max(anchor_end, count - max(0, min(recent_count, count)))
    recent_start = max(anchor_end, _tool_group_start(messages, recent_start))

    if 0 < anchor_end < count:
        group_start = _tool_group_start(messages, anchor_end)
        if group_start < anchor_end:
            anchor_end = group_start
        elif _tool_call_ids(messages[anchor_end - 1]):
            call_ids = _tool_call_ids(messages[anchor_end - 1])
            while (
                anchor_end < count
                and messages[anchor_end].get("role") == "tool"
                and str(messages[anchor_end].get("tool_call_id") or "") in call_ids
            ):
                anchor_end += 1

    recent_start = max(anchor_end, recent_start)
    return anchor_end, recent_start


def _cache_key(message: dict, content: str, target_tokens: int) -> str:
    identity = (
        f"{message.get('tool_call_id', '')}\0{message.get('name', '')}\0"
        f"{target_tokens}\0{content}"
    )
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()


def _remember_summary(key: str, summary: str) -> None:
    _TOOL_SUMMARY_CACHE[key] = summary
    _TOOL_SUMMARY_CACHE.move_to_end(key)
    while len(_TOOL_SUMMARY_CACHE) > _TOOL_SUMMARY_CACHE_SIZE:
        _TOOL_SUMMARY_CACHE.popitem(last=False)


class ContextToolCompactionMixin:
    async def compact_tool_results_to_budget(
        self,
        messages: List[dict],
        budget: int,
        persisted_cache: Dict[str, str] | None = None,
    ) -> tuple[List[dict], Dict[str, str], Dict[str, Any]]:
        """Semantically reduce tool messages only when the candidate is over budget.

        The tool message and its ``tool_call_id`` remain intact. Full content is
        never sliced: oversized source is chunked by the summary backend first.
        """

        candidate = deepcopy(messages)
        cache = dict(persisted_cache or {})
        original_tokens = self.count_messages_tokens(candidate)
        if original_tokens <= budget:
            return (
                candidate,
                cache,
                {
                    "compacted": 0,
                    "original_tokens": original_tokens,
                    "final_tokens": original_tokens,
                },
            )

        tool_indexes = [
            index
            for index, message in enumerate(candidate)
            if message.get("role") == "tool"
            and self.extract_text_from_content(message.get("content", "")).strip()
            and not self.extract_text_from_content(
                message.get("content", "")
            ).startswith(_TOOL_SUMMARY_PREFIX)
        ]
        # Reduce the largest results first; stable ordering means equally-sized
        # earlier results compact before newer ones.
        tool_indexes.sort(
            key=lambda index: self.count_message_tokens(candidate[index]), reverse=True
        )

        compacted = 0
        for index in tool_indexes:
            if self.count_messages_tokens(candidate) <= budget:
                break
            message = candidate[index]
            content = self.extract_text_from_content(message.get("content", ""))
            message_tokens = self.count_message_tokens(message)
            other_tokens = self.count_messages_tokens(candidate) - message_tokens
            target_tokens = max(
                64,
                min(
                    int(self.valves.summary_max_tokens),
                    max(64, budget - other_tokens),
                ),
            )
            key = _cache_key(message, content, target_tokens)
            summary = cache.get(key) or _TOOL_SUMMARY_CACHE.get(key)
            if not summary:
                summary = await self.generate_tool_result_summary(
                    message,
                    target_tokens=target_tokens,
                )
            if not summary:
                continue

            _remember_summary(key, summary)
            cache[key] = summary
            source_tokens = self.count_tokens(content)
            if self.count_tokens(summary) >= source_tokens:
                continue
            message["content"] = (
                f"{_TOOL_SUMMARY_PREFIX}; original≈{source_tokens:,} tokens; "
                f"content_hash={key[:12]}]\n{summary}"
            )
            compacted += 1

        # Keep persisted chat metadata bounded. The process-local LRU still
        # covers temporary chats and repeated requests in the current worker.
        if len(cache) > _TOOL_SUMMARY_CACHE_SIZE:
            cache = dict(list(cache.items())[-_TOOL_SUMMARY_CACHE_SIZE:])
        return (
            candidate,
            cache,
            {
                "compacted": compacted,
                "original_tokens": original_tokens,
                "final_tokens": self.count_messages_tokens(candidate),
            },
        )
