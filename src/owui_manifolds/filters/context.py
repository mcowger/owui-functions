"""
title: 🚀 Context Window Manager (Simplified)
id: context_window_manager_simplified
version: 3.2.0
license: MIT
required_open_webui_version: 0.10.2
requirements: openai~=2.41, tiktoken>=0.7, pydantic~=2.13
description: Importable context-compaction library with a thin Open WebUI filter adapter.
    It keeps conversations inside the model context window, preserves atomic tool
    exchanges, and semantically compacts oversized tool results during recursive pipe
    calls without fixed provider-specific result limits.
"""

import json
import traceback
from typing import Any, Callable, Dict, List, Optional

from owui_manifolds.filters.context_constants import (
    NO_TABLE_ROWS_FALLBACK_WARNING_PERCENT,
)
from owui_manifolds.filters.context_marker import (
    context_is_prepared,
    mark_context_prepared,
)
from owui_manifolds.filters.context_matching import ModelMatcher
from owui_manifolds.filters.context_modeling import ContextModelingMixin
from owui_manifolds.filters.context_persistence import ContextPersistenceMixin
from owui_manifolds.filters.context_summary import ContextSummaryMixin
from owui_manifolds.filters.context_tokens import ContextTokenMixin, TokenCalculator
from owui_manifolds.filters.context_tooling import (
    ContextBudgetExceededError,
    ContextToolCompactionMixin,
    strip_legacy_details_from_messages,
)
from owui_manifolds.filters.context_valves import ContextUserValves, ContextValves
from owui_manifolds.filters.context_window import ContextWindowMixin


class ContextManager(
    ContextModelingMixin,
    ContextTokenMixin,
    ContextSummaryMixin,
    ContextToolCompactionMixin,
    ContextPersistenceMixin,
    ContextWindowMixin,
):
    Valves = ContextValves
    UserValves = ContextUserValves

    def __init__(self):
        self.valves = self.Valves()
        self.model_matcher = ModelMatcher()
        self.token_calculator = TokenCalculator()
        self._current_model_name = ""
        self._current_model_info: Dict[str, Any] = {}
        self._summary_calls_used = 0

    def debug_log(self, level: int, message: str, emoji: str = "🔧"):
        if self.valves.debug_level >= level:
            print(f"{emoji} {message}")

    async def inlet(
        self,
        body: dict,
        user: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
        __event_call__: Optional[Callable] = None,
        __user__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        **kwargs,
    ) -> dict:
        if context_is_prepared(body):
            return body
        processed = await self._process_body(
            body,
            user=user,
            __event_emitter__=__event_emitter__,
            __event_call__=__event_call__,
            __user__=__user__,
            __model__=__model__,
            __chat_id__=__chat_id__,
            **kwargs,
        )
        return mark_context_prepared(processed)

    async def _process_body(
        self,
        body: dict,
        user: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
        __event_call__: Optional[Callable] = None,
        __user__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        **kwargs,
    ) -> dict:
        if not self.valves.enable_processing:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        self._summary_calls_used = 0
        messages, legacy_details_removed = strip_legacy_details_from_messages(messages)
        body["messages"] = messages

        model_name = body.get("model", "unknown")
        base_model_id = isinstance(__model__, dict) and __model__.get("info", {}).get(
            "base_model_id"
        )
        if base_model_id:
            model_name = base_model_id
        if self.is_model_excluded(model_name):
            return body

        self._current_model_name = model_name

        user_dict = (
            user
            if isinstance(user, dict)
            else (__user__ if isinstance(__user__, dict) else None)
        )
        user_valves = None
        if isinstance(user_dict, dict):
            raw_uv = user_dict.get("valves")
            if isinstance(raw_uv, self.UserValves):
                user_valves = raw_uv
            elif isinstance(raw_uv, dict):
                try:
                    user_valves = self.UserValves(**raw_uv)
                except Exception:
                    user_valves = None
        if user_valves is None:
            user_valves = self.UserValves()

        anchor_n = (
            user_valves.anchor_message_count
            if user_valves.anchor_message_count is not None
            else self.valves.anchor_message_count_default
        )
        recent_n = (
            user_valves.recent_message_count
            if user_valves.recent_message_count is not None
            else self.valves.recent_message_count_default
        )

        # Only the trailing user message is the active prompt. During OWUI's
        # recursive tool loop, assistant/tool messages follow that user turn;
        # extracting and re-appending the older user message would corrupt the
        # function-call/result ordering.
        current_user_message = (
            messages[-1] if messages[-1].get("role") == "user" else None
        )

        system_messages = [m for m in messages if m.get("role") == "system"]
        history_messages = [
            m
            for m in messages
            if m.get("role") != "system" and m is not current_user_message
        ]

        current_user_tokens = (
            self.count_message_tokens(current_user_message)
            if current_user_message
            else 0
        )
        request_input_budget = self.calculate_request_input_budget(
            model_name, user_valves.context_target_percent
        )
        system_tokens = self.count_messages_tokens(system_messages)
        history_tokens = self.count_messages_tokens(history_messages)
        request_overhead_tokens = sum(
            self.count_tokens(
                json.dumps(body.get(field), ensure_ascii=False, default=str)
            )
            for field in ("tools", "extra_tools")
            if body.get(field)
        )
        budget_for_history = (
            request_input_budget
            - current_user_tokens
            - system_tokens
            - request_overhead_tokens
        )

        self.debug_log(
            1,
            f"Budget check: history={history_tokens:,} vs budget_for_history={budget_for_history:,} "
            f"(request_budget={request_input_budget:,}, system={system_tokens:,}, tools={request_overhead_tokens:,}, current_user={current_user_tokens:,}, "
            f"pct={user_valves.context_target_percent}, anchor={anchor_n}, recent={recent_n})",
            "📊",
        )

        persistable = self._is_persistable_chat_id(__chat_id__)
        state = (await self._load_context_state(__chat_id__)) if persistable else None
        state = state or {}

        covered_upto = min(anchor_n, len(history_messages))
        blocks: List[Dict[str, str]] = []
        if state:
            stored_upto = min(
                int(state.get("covered_upto", covered_upto)), len(history_messages)
            )
            stored_hash = state.get("covered_hash")
            # Staleness check: if earlier messages were edited/deleted since the
            # last fold, the stored blocks no longer match - start over rather
            # than risk an inconsistent/incorrect summary.
            if stored_hash == self._hash_history_prefix(history_messages[:stored_upto]):
                covered_upto = stored_upto
                blocks = state.get("blocks", [])

        budget = max(budget_for_history, 0)
        estimated_request_tokens = (
            system_tokens
            + history_tokens
            + current_user_tokens
            + request_overhead_tokens
        )
        over_cap = (
            history_tokens > budget or estimated_request_tokens > request_input_budget
        )
        force_fold = False
        prompted = False
        warning_last_asked = int(state.get("warning_last_asked_msg_count", 0))
        total_message_count = len(messages)

        if not over_cap:
            warning_percent = self._current_model_info.get(
                "warning_percent", NO_TABLE_ROWS_FALLBACK_WARNING_PERCENT
            )
            turns_elapsed = (total_message_count - warning_last_asked) // 2
            should_prompt = (
                self.valves.enable_warning_prompt
                and __event_call__
                and persistable
                and budget > 0
                and history_tokens >= budget * warning_percent / 100
                and (
                    warning_last_asked == 0
                    or turns_elapsed >= self.valves.warning_reprompt_interval_turns
                )
            )
            if should_prompt:
                force_fold = await self._ask_compress_now(
                    __event_call__, history_tokens, budget
                )
                prompted = True
                warning_last_asked = total_message_count
                self.debug_log(
                    1,
                    f"Warning-threshold prompt shown at {history_tokens:,}/{budget:,} tokens - "
                    f"user {'accepted' if force_fold else 'declined/timed out'}",
                    "⚠️",
                )

            if not force_fold:
                if prompted and persistable:
                    await self._save_context_state(
                        __chat_id__,
                        {
                            "covered_upto": covered_upto,
                            "covered_hash": self._hash_history_prefix(
                                history_messages[:covered_upto]
                            ),
                            "blocks": blocks,
                            "tool_summaries": state.get("tool_summaries", {}),
                            "warning_last_asked_msg_count": warning_last_asked,
                        },
                    )
                return body

        try:
            candidate, new_covered_upto, new_blocks, stats = (
                await self.build_context_window(
                    history_messages,
                    budget,
                    anchor_n,
                    recent_n,
                    covered_upto,
                    blocks,
                    force=force_fold,
                    preserve_latest_unit=current_user_message is None,
                )
            )
        except Exception as e:
            self.debug_log(
                1, f"Context trimming failed, leaving conversation as-is: {e}", "❌"
            )
            if self.valves.debug_level >= 2:
                traceback.print_exc()
            return body

        candidate, tool_summary_cache, tool_stats = (
            await self.compact_tool_results_to_budget(
                candidate,
                budget,
                persisted_cache=state.get("tool_summaries", {}),
            )
        )

        if persistable and (stats["folded"] or prompted or tool_stats["compacted"]):
            new_hash = self._hash_history_prefix(history_messages[:new_covered_upto])
            await self._save_context_state(
                __chat_id__,
                {
                    "covered_upto": new_covered_upto,
                    "covered_hash": new_hash,
                    "blocks": new_blocks,
                    "tool_summaries": tool_summary_cache,
                    "warning_last_asked_msg_count": warning_last_asked,
                },
            )

        final_messages = system_messages + candidate
        if current_user_message:
            final_messages.append(current_user_message)

        final_tokens = (
            self.count_messages_tokens(final_messages) + request_overhead_tokens
        )

        status_parts = [
            f"kept {stats['kept']}/{len(history_messages)} messages verbatim"
        ]
        if stats["summarized_count"]:
            status_parts.append(
                f"{stats['summarized_count']} older message(s) summarized across "
                f"{stats['block_count']} block(s)"
            )
            if stats["folded"]:
                status_parts.append(
                    "folded 1 new block this turn (user-approved early compression)"
                    if prompted
                    else "folded 1 new block this turn"
                )
        if tool_stats["compacted"]:
            status_parts.append(
                f"semantically compacted {tool_stats['compacted']} oversized tool result(s)"
            )
        if legacy_details_removed:
            status_parts.append(
                f"removed {legacy_details_removed:,} characters of legacy UI details"
            )
        original_tokens = (
            system_tokens
            + history_tokens
            + current_user_tokens
            + request_overhead_tokens
        )
        status_msg = (
            f"Trimmed conversation history: {', '.join(status_parts)} "
            f"({original_tokens:,}→{final_tokens:,} tokens)"
        )

        if __event_emitter__ and (stats["folded"] or tool_stats["compacted"]):
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": status_msg, "done": True},
                    }
                )
            except Exception:
                pass

        self.debug_log(1, status_msg, "🔧")

        final_budget = request_input_budget
        if final_tokens > final_budget:
            error_message = (
                "Context compaction could not fit this request within the model budget "
                f"({final_tokens:,} > {final_budget:,} estimated tokens; "
                f"summary calls {self._summary_calls_used}/"
                f"{self.valves.max_summary_calls_per_request})."
            )
            if __event_emitter__:
                try:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": error_message,
                                "done": True,
                                "level": "error",
                            },
                        }
                    )
                except Exception:
                    pass
            raise ContextBudgetExceededError(error_message)

        body["messages"] = final_messages
        return body


class Filter(ContextManager):
    """Thin Open WebUI filter adapter around the importable context library."""

    pass
