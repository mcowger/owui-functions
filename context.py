"""
title: 🚀 Context Window Manager (Simplified)
id: context_window_manager_simplified
version: 3.0.0
license: MIT
required_open_webui_version: 0.5.17
description: Keeps conversations inside the model's context window by anchoring the
    earliest and most recent messages verbatim, and persistently, incrementally
    summarizing whatever falls in between instead of silently dropping it. Simplified
    rewrite of the original Advanced Context Manager (JiangNanGenius) - no auto memory,
    RAG/embeddings, keyword generation, or multimodal processing.
"""

import fnmatch
import hashlib
import asyncio
import traceback
from typing import Optional, List, Dict, Callable, Any, Tuple

from pydantic import BaseModel, Field

try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    tiktoken = None

try:
    from openai import AsyncOpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    AsyncOpenAI = None

from open_webui.models.chats import Chat, Chats
from open_webui.internal.db import get_async_db_context

# Flat per-message token overhead to roughly account for chat-formatting tokens
# (role markers, separators) that tiktoken's plain-text encoding doesn't capture.
MESSAGE_OVERHEAD_TOKENS = 20

# Conservative cap on how much of the "excluded middle" text we hand to the
# summarization model in one call, so an extremely long conversation can't blow
# out the summarizer's own context window. If exceeded, only the most recent
# portion (still ordered oldest->newest) of the excluded messages is sent.
SUMMARY_INPUT_TOKEN_CAP = 60000

# Key under chat.meta where persisted anchor/block-summary state is stored.
CONTEXT_MANAGER_META_KEY = "context_manager"

# Last-resort fallback if Valves.model_token_table has no rows at all (e.g. an
# admin clears it) - the table's own trailing "*" row is the intended catch-all
# and normally makes this dead code.
NO_TABLE_ROWS_FALLBACK_LIMIT = 200000
NO_TABLE_ROWS_FALLBACK_CAP_PERCENT = 92
NO_TABLE_ROWS_FALLBACK_WARNING_PERCENT = 80


class ModelMatcher:
    """Matches a model name to a context-window size and cap/warning percents using
    an admin-editable table (see Filter.Valves.model_token_table) of

        pattern,max_tokens,cap_percent,warning_percent

    rows, instead of a hardcoded pattern list. `pattern` is a glob (fnmatch-style,
    e.g. "claude-sonnet-4-5*") matched against the lowercased model name; rows are
    tried in order and the first match wins, so more specific patterns must be
    listed before broader ones (e.g. "claude-sonnet-4-5*" before "claude-sonnet-*").
    A trailing "*" row acts as the catch-all default for unrecognized models.
    """

    def parse_table(self, table_text: str) -> List[Tuple[str, int, int, int]]:
        rows = []
        for raw_line in (table_text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            fields = [f.strip() for f in line.split(",")]
            if len(fields) != 4:
                continue
            pattern, max_tokens, cap_percent, warning_percent = fields
            try:
                rows.append(
                    (pattern.lower(), int(max_tokens), int(cap_percent), int(warning_percent))
                )
            except ValueError:
                continue
        return rows

    def match_model(self, model_name: str, table_text: str) -> Dict[str, Any]:
        model_lower = (model_name or "").lower().strip()
        # Pipe/manifold ids look like "<pipe_id>.<provider>/<model>"; the bare
        # upstream model name is always everything after the last "/", so slicing
        # there alone is sufficient. (Deliberately *not* also splitting on "."
        # first - that would mangle plain versioned names with no pipe prefix at
        # all, like "gpt-5.4-mini" or "glm-5.2-flash".)
        model_lower = model_lower.rsplit("/", 1)[-1]

        if model_lower:
            for pattern, limit, cap_percent, warning_percent in self.parse_table(table_text):
                if fnmatch.fnmatchcase(model_lower, pattern):
                    return {
                        "limit": limit,
                        "cap_percent": cap_percent,
                        "warning_percent": warning_percent,
                        "match_type": "matched",
                        "matched_pattern": pattern,
                    }

        # Only reachable if model_token_table has no rows at all (its default
        # trailing "*" row otherwise matches everything).
        return {
            "limit": NO_TABLE_ROWS_FALLBACK_LIMIT,
            "cap_percent": NO_TABLE_ROWS_FALLBACK_CAP_PERCENT,
            "warning_percent": NO_TABLE_ROWS_FALLBACK_WARNING_PERCENT,
            "match_type": "default",
            "hint": f"model_token_table has no usable rows; using built-in fallback for '{model_name}'.",
        }


class TokenCalculator:
    """Token counter, backed by tiktoken when available."""

    def __init__(self):
        self._encoding = None
        self.model_info: Optional[Dict[str, Any]] = None

    def set_model_info(self, model_info: dict):
        self.model_info = model_info

    def get_encoding(self):
        if not TIKTOKEN_AVAILABLE:
            return None
        if self._encoding is None:
            try:
                self._encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                pass
        return self._encoding

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        encoding = self.get_encoding()
        if encoding:
            try:
                return len(encoding.encode(str(text)))
            except Exception:
                pass
        return len(str(text)) // 4

    def calculate_image_tokens(self) -> int:
        """Flat per-image token estimate. Images are never inspected or described -
        just budgeted for with a flat constant (Valves.default_image_tokens)."""
        if self.model_info:
            return self.model_info.get("image_tokens", 1500)
        return 1500


class Filter:
    class Valves(BaseModel):
        enable_processing: bool = Field(
            default=True, description="🔄 Enable context-window trimming"
        )
        excluded_models: str = Field(
            default="", description="🚫 Excluded model list (comma separated substrings)"
        )

        # ========== Model matching ==========
        model_token_table: str = Field(
            default=(
                "# pattern,max_tokens,cap_percent,warning_percent\n"
                "# pattern is a glob (fnmatch-style *), matched against the lowercased\n"
                "# model name. First match wins - list more specific patterns (e.g.\n"
                "# claude-sonnet-4-5*) before broader ones (e.g. claude-sonnet-*).\n"
                "gpt-5*,1000000,92,80\n"
                "gpt-4o*,128000,92,80\n"
                "gpt-4*,8192,92,80\n"
                "claude-sonnet-4-5*,1000000,92,80\n"
                "claude-sonnet-5*,1000000,92,80\n"
                "claude-sonnet-*,200000,92,80\n"
                "claude-opus-4-5*,1000000,92,80\n"
                "claude-opus-5*,1000000,92,80\n"
                "claude-opus-*,200000,92,80\n"
                "claude-haiku-*,200000,92,80\n"
                "claude-fable-5*,1000000,92,80\n"
                "claude-fable-*,200000,92,80\n"
                "claude-4*,200000,92,80\n"
                "claude-3*,200000,92,80\n"
                "claude*,200000,92,80\n"
                "doubao*vision*,128000,92,80\n"
                "doubao*,50000,92,80\n"
                "glm-5.2*,500000,92,80\n"
                "glm-5.1*,262000,92,80\n"
                "gemini*,1000000,92,80\n"
                "qwen*vl*,32000,92,80\n"
                "*,300000,92,80"
            ),
            description="📋 Model→context-window table, one 'pattern,max_tokens,cap_percent,"
            "warning_percent' row per line (# comments allowed). Edit this to add/adjust "
            "models without a code change. cap_percent is the safety margin applied to "
            "max_tokens; warning_percent is where the early-compression prompt fires, as a "
            "percent of the resulting budget. The trailing '*' row is the catch-all for "
            "unrecognized models - keep it last, or replace it with your own default.",
        )
        default_image_tokens: int = Field(
            default=1500, description="🖼️ Flat per-image token estimate for multimodal messages"
        )
        min_target_tokens: int = Field(
            default=10000, description="⚖️ Absolute floor for the history token budget"
        )

        # ========== Response buffer ==========
        response_buffer_percent: int = Field(
            default=6,
            ge=1,
            le=100,
            description="📝 Reserved response space, as a percent of the model limit",
        )
        response_buffer_max: int = Field(
            default=3000, description="📝 Max reserved response space (tokens)"
        )
        response_buffer_min: int = Field(
            default=1000, description="📝 Min reserved response space (tokens)"
        )

        # ========== Anchor / recent message defaults ==========
        anchor_message_count_default: int = Field(
            default=2,
            description="⚓ Default number of earliest history messages to always keep verbatim "
            "(users can override via their own valve)",
        )
        recent_message_count_default: int = Field(
            default=20,
            description="🕐 Default number of most-recent history messages to always keep verbatim "
            "(users can override via their own valve)",
        )

        # ========== Overflow summarization ==========
        enable_overflow_summary: bool = Field(
            default=True,
            description="📚 Summarize history that doesn't fit the anchor/recent window instead of dropping it",
        )
        api_base: str = Field(
            default="https://ark.cn-beijing.volces.com/api/v3", description="🔗 API base URL"
        )
        api_key: str = Field(default="", description="🔑 API key")
        summary_model: str = Field(
            default="doubao-1-5-lite-32k-250115", description="📝 Model used for overflow summarization"
        )
        summary_prompt: str = Field(
            default="You are compressing an older portion of a conversation so it can be "
            "dropped from the active context window without losing important information.\n\n"
            "The transcript below contains multiple separate exchanges. Treat this as a "
            "checklist: list every exchange in order, and for each one, either (a) summarize "
            "it, or (b) explicitly note \"small talk, nothing to preserve\" - never silently "
            "skip one.\n\n"
            "For each exchange, preserve concrete facts, decisions, constraints, numbers, "
            "names, and technical/code/config details, and any open questions. Do not use "
            "vague filler like 'they discussed various topics' - be specific. Output plain "
            "text only, no preamble.",
            description="📝 System prompt for the overflow-summary call",
        )
        summary_max_tokens: int = Field(
            default=500, description="📝 Max output tokens for the overflow summary"
        )
        request_timeout: int = Field(default=90, description="⏱️ API request timeout (seconds)")
        api_error_retry_times: int = Field(default=2, description="🔄 API error retry count")
        api_error_retry_delay: float = Field(
            default=1.0, description="⏱️ API error retry delay (seconds)"
        )

        # ========== Early warning prompt ==========
        enable_warning_prompt: bool = Field(
            default=True,
            description="⚠️ Before the hard budget cap is hit, ask the user whether to compress "
            "early (requires an active browser session; silently skipped otherwise)",
        )
        warning_reprompt_interval_turns: int = Field(
            default=10,
            ge=1,
            description="⚠️ Minimum number of turns (user+assistant message pairs) between "
            "warning prompts, so accepting/declining once doesn't silence the warning forever",
        )

        debug_level: int = Field(default=0, description="🐛 Debug log verbosity (0-2)")

    class UserValves(BaseModel):
        context_target_percent: int = Field(
            default=50,
            ge=1,
            le=100,
            description="🎯 Percent of the model's context window to actually use for conversation "
            "history (e.g. 50 = target half the window, even on a 1M-token model). Lower is cheaper.",
        )
        anchor_message_count: Optional[int] = Field(
            default=None,
            description="⚓ Number of earliest history messages to always keep verbatim. "
            "Leave blank to use the admin-configured default.",
        )
        recent_message_count: Optional[int] = Field(
            default=None,
            description="🕐 Number of most-recent history messages to always keep verbatim. "
            "Leave blank to use the admin-configured default.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.model_matcher = ModelMatcher()
        self.token_calculator = TokenCalculator()
        self._current_model_name = ""
        self._current_model_info: Dict[str, Any] = {}

    # ========== Logging ==========

    def debug_log(self, level: int, message: str, emoji: str = "🔧"):
        if self.valves.debug_level >= level:
            print(f"{emoji} {message}")

    # ========== Model handling ==========

    def is_model_excluded(self, model_name: str) -> bool:
        if not self.valves.excluded_models or not model_name:
            return False
        excluded_list = [
            m.strip().lower() for m in self.valves.excluded_models.split(",") if m.strip()
        ]
        model_lower = model_name.lower()
        return any(excluded in model_lower for excluded in excluded_list)

    def analyze_model(self, model_name: str) -> Dict[str, Any]:
        model_info = self.model_matcher.match_model(model_name, self.valves.model_token_table)
        model_info["image_tokens"] = self.valves.default_image_tokens
        self.token_calculator.set_model_info(model_info)
        self._current_model_info = model_info
        self.debug_log(
            1,
            f"Model: {model_name} -> limit={model_info['limit']:,} "
            f"cap%={model_info['cap_percent']} warning%={model_info['warning_percent']} "
            f"match={model_info.get('match_type')} pattern={model_info.get('matched_pattern')}",
            "🎯",
        )
        return model_info

    def get_model_token_limit(self, model_name: str) -> int:
        model_info = self.analyze_model(model_name)
        return int(model_info["limit"] * model_info["cap_percent"] / 100)

    # ========== Token counting ==========

    def count_tokens(self, text: str) -> int:
        return self.token_calculator.count_tokens(text)

    def extract_text_from_content(self, content) -> str:
        if isinstance(content, list):
            return " ".join(
                item.get("text", "") for item in content if item.get("type") == "text"
            )
        return str(content) if content else ""

    def count_message_tokens(self, message: dict) -> int:
        if not message:
            return 0
        content = message.get("content", "")
        total = 0
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    total += self.count_tokens(item.get("text", ""))
                elif item.get("type") == "image_url":
                    total += self.token_calculator.calculate_image_tokens()
        else:
            total = self.count_tokens(content)
        return total + MESSAGE_OVERHEAD_TOKENS

    def count_messages_tokens(self, messages: List[dict]) -> int:
        return sum(self.count_message_tokens(m) for m in messages)

    # ========== Budget calculation ==========

    def calculate_target_tokens(
        self, model_name: str, current_user_tokens: int, context_target_percent: int
    ) -> int:
        safe_limit = self.get_model_token_limit(model_name)
        budget_limit = safe_limit * context_target_percent / 100
        response_buffer = min(
            self.valves.response_buffer_max,
            max(
                self.valves.response_buffer_min,
                int(safe_limit * self.valves.response_buffer_percent / 100),
            ),
        )
        target = budget_limit - current_user_tokens - response_buffer
        return int(max(target, self.valves.min_target_tokens))

    # ========== API client ==========

    def get_api_client(self):
        if not OPENAI_AVAILABLE or not self.valves.api_key:
            return None
        return AsyncOpenAI(
            base_url=self.valves.api_base,
            api_key=self.valves.api_key,
            timeout=self.valves.request_timeout,
        )

    async def safe_api_call(self, call_func, call_name: str):
        for attempt in range(self.valves.api_error_retry_times + 1):
            try:
                return await call_func()
            except Exception as e:
                if attempt < self.valves.api_error_retry_times:
                    self.debug_log(
                        1,
                        f"{call_name} failed (attempt {attempt + 1}), retrying in "
                        f"{self.valves.api_error_retry_delay}s: {str(e)[:150]}",
                        "🔄",
                    )
                    await asyncio.sleep(self.valves.api_error_retry_delay)
                else:
                    self.debug_log(1, f"{call_name} failed permanently: {str(e)[:150]}", "❌")
                    return None
        return None

    # ========== Overflow summarization ==========

    def _messages_to_text(self, messages: List[dict]) -> str:
        parts = []
        for msg in messages:
            text = self.extract_text_from_content(msg.get("content", ""))
            if text:
                parts.append(f"{msg.get('role', '')}: {text}")
        return "\n\n".join(parts)

    def _cap_summary_input(self, text: str) -> str:
        """Keep only the most recent portion of the excluded text if it would be too
        large to safely hand to the summarization model in one call."""
        if self.count_tokens(text) <= SUMMARY_INPUT_TOKEN_CAP:
            return text
        # cl100k averages ~4 chars/token; trim from the front (oldest content).
        approx_chars = SUMMARY_INPUT_TOKEN_CAP * 4
        return text[-approx_chars:]

    async def generate_overflow_summary(
        self, excluded_messages: List[dict]
    ) -> Optional[str]:
        client = self.get_api_client()
        if not client:
            return None
        text = self._cap_summary_input(self._messages_to_text(excluded_messages))
        if not text.strip():
            return None

        async def _call():
            response = await client.chat.completions.create(
                model=self.valves.summary_model,
                messages=[
                    {"role": "system", "content": self.valves.summary_prompt},
                    {"role": "user", "content": text},
                ],
                max_tokens=self.valves.summary_max_tokens,
            )
            return response.choices[0].message.content

        result = await self.safe_api_call(_call, "overflow summary")
        return result.strip() if isinstance(result, str) and result.strip() else None

    # ========== Persisted context-manager state (chat.meta) ==========

    def _is_persistable_chat_id(self, chat_id: Optional[str]) -> bool:
        """Temporary/local/channel chats have no real DB row to persist against."""
        return bool(chat_id) and not chat_id.startswith("local:") and not chat_id.startswith(
            "channel:"
        )

    def _hash_history_prefix(self, messages: List[dict]) -> str:
        hasher = hashlib.sha256()
        for m in messages:
            hasher.update(str(m.get("role", "")).encode("utf-8", "ignore"))
            hasher.update(b"\x00")
            hasher.update(
                self.extract_text_from_content(m.get("content", "")).encode("utf-8", "ignore")
            )
            hasher.update(b"\x01")
        return hasher.hexdigest()

    async def _load_context_state(self, chat_id: str) -> Optional[Dict[str, Any]]:
        try:
            chat_model = await Chats.get_chat_by_id(chat_id)
            if not chat_model:
                return None
            return (chat_model.meta or {}).get(CONTEXT_MANAGER_META_KEY)
        except Exception as e:
            self.debug_log(1, f"Failed to load persisted context state: {e}", "⚠️")
            return None

    async def _save_context_state(self, chat_id: str, state: Dict[str, Any]):
        try:
            async with get_async_db_context() as session:
                chat_item = await session.get(Chat, chat_id)
                if chat_item is None:
                    return
                chat_item.meta = {**(chat_item.meta or {}), CONTEXT_MANAGER_META_KEY: state}
                await session.commit()
        except Exception as e:
            self.debug_log(1, f"Failed to persist context state: {e}", "⚠️")

    # ========== Early warning prompt ==========

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

    # ========== Core selection algorithm ==========

    async def build_context_window(
        self,
        history_messages: List[dict],
        budget: int,
        anchor_n: int,
        recent_n: int,
        covered_upto: int,
        blocks: List[Dict[str, str]],
        force: bool = False,
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
                {"kept": 0, "summarized_count": 0, "block_count": len(blocks), "folded": False},
            )

        anchor_n = max(0, min(anchor_n, n))
        recent_n = max(0, min(recent_n, n))
        recent_start = max(anchor_n, n - recent_n)
        covered_upto = max(anchor_n, min(covered_upto, recent_start))

        anchor = history_messages[:anchor_n]
        pending = history_messages[covered_upto:recent_start]
        recent = history_messages[recent_start:]

        def block_messages(block_list: List[Dict[str, str]]) -> List[dict]:
            return [
                {"role": "system", "content": f"[Summary of earlier conversation]\n{b['text']}"}
                for b in block_list
            ]

        candidate = anchor + block_messages(blocks) + pending + recent
        folded = False

        if (
            pending
            and self.valves.enable_overflow_summary
            and (force or self.count_messages_tokens(candidate) > budget)
        ):
            summary_text = await self.generate_overflow_summary(pending)
            if summary_text:
                blocks = blocks + [{"text": summary_text}]
                covered_upto = recent_start
                folded = True
                pending = []
                candidate = anchor + block_messages(blocks) + pending + recent

        stats = {
            "kept": len(anchor) + len(pending) + len(recent),
            "summarized_count": covered_upto - anchor_n,
            "block_count": len(blocks),
            "folded": folded,
        }
        return candidate, covered_upto, blocks, stats

    # ========== Entry point ==========

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
        if not self.valves.enable_processing:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        model_name = body.get("model", "unknown")
        base_model_id = isinstance(__model__, dict) and __model__.get("info", {}).get(
            "base_model_id"
        )
        if base_model_id:
            model_name = base_model_id
        if self.is_model_excluded(model_name):
            return body

        self._current_model_name = model_name

        user_dict = user if isinstance(user, dict) else (__user__ if isinstance(__user__, dict) else None)
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

        current_user_message = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                current_user_message = msg
                break

        system_messages = [m for m in messages if m.get("role") == "system"]
        history_messages = [
            m
            for m in messages
            if m.get("role") != "system" and m is not current_user_message
        ]

        current_user_tokens = (
            self.count_message_tokens(current_user_message) if current_user_message else 0
        )
        target_tokens = self.calculate_target_tokens(
            model_name, current_user_tokens, user_valves.context_target_percent
        )
        system_tokens = self.count_messages_tokens(system_messages)
        history_tokens = self.count_messages_tokens(history_messages)
        budget_for_history = target_tokens - system_tokens

        self.debug_log(
            1,
            f"Budget check: history={history_tokens:,} vs budget_for_history={budget_for_history:,} "
            f"(target={target_tokens:,}, system={system_tokens:,}, current_user={current_user_tokens:,}, "
            f"pct={user_valves.context_target_percent}, anchor={anchor_n}, recent={recent_n})",
            "📊",
        )

        persistable = self._is_persistable_chat_id(__chat_id__)
        state = (await self._load_context_state(__chat_id__)) if persistable else None
        state = state or {}

        covered_upto = min(anchor_n, len(history_messages))
        blocks: List[Dict[str, str]] = []
        if state:
            stored_upto = min(int(state.get("covered_upto", covered_upto)), len(history_messages))
            stored_hash = state.get("covered_hash")
            # Staleness check: if earlier messages were edited/deleted since the
            # last fold, the stored blocks no longer match - start over rather
            # than risk an inconsistent/incorrect summary.
            if stored_hash == self._hash_history_prefix(history_messages[:stored_upto]):
                covered_upto = stored_upto
                blocks = state.get("blocks", [])

        budget = max(budget_for_history, 0)
        over_cap = history_tokens > budget
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
                force_fold = await self._ask_compress_now(__event_call__, history_tokens, budget)
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
                            "warning_last_asked_msg_count": warning_last_asked,
                        },
                    )
                return body

        try:
            candidate, new_covered_upto, new_blocks, stats = await self.build_context_window(
                history_messages,
                budget,
                anchor_n,
                recent_n,
                covered_upto,
                blocks,
                force=force_fold,
            )
        except Exception as e:
            self.debug_log(1, f"Context trimming failed, leaving conversation as-is: {e}", "❌")
            if self.valves.debug_level >= 2:
                traceback.print_exc()
            return body

        if persistable and (stats["folded"] or prompted):
            new_hash = self._hash_history_prefix(history_messages[:new_covered_upto])
            await self._save_context_state(
                __chat_id__,
                {
                    "covered_upto": new_covered_upto,
                    "covered_hash": new_hash,
                    "blocks": new_blocks,
                    "warning_last_asked_msg_count": warning_last_asked,
                },
            )

        final_messages = system_messages + candidate
        if current_user_message:
            final_messages.append(current_user_message)

        final_tokens = self.count_messages_tokens(final_messages)

        status_parts = [f"kept {stats['kept']}/{len(history_messages)} messages verbatim"]
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
        original_tokens = system_tokens + history_tokens + current_user_tokens
        status_msg = (
            f"Trimmed conversation history: {', '.join(status_parts)} "
            f"({original_tokens:,}→{final_tokens:,} tokens)"
        )

        if __event_emitter__ and stats["folded"]:
            try:
                await __event_emitter__(
                    {"type": "status", "data": {"description": status_msg, "done": True}}
                )
            except Exception:
                pass

        self.debug_log(1, status_msg, "🔧")

        body["messages"] = final_messages
        return body
