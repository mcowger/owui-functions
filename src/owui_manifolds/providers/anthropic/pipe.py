"""
title: Anthropic API Manifold
id: anthropic
author: Podden (forked)
original_author: Balaxxe / nbellochi
version: 1.1.1
license: MIT
required_open_webui_version: 0.10.2
requirements: anthropic>=0.103, openai~=2.41, tiktoken>=0.7, pydantic~=2.13

A lean Open WebUI manifold for Anthropic Claude models. Parsing is delegated
to the Anthropic Python SDK's high-level streaming helpers; this file only
implements the Open WebUI-specific glue (message conversion, tool bridging,
history reconstruction, event emission, citations).

Supported:
- Streaming responses via the stable client.messages.stream()
- Open WebUI-owned custom-function loops
- Native thinking/tool continuation through reasoning_details
- Extended thinking (unified EFFORT control: off/adaptive/low/medium/high/xhigh/max)
- web_search + web_fetch server tools with citations
- Image input (vision)
- Prompt caching (cache_control)
- Typed error handling
"""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
import json
import logging
import re
import time
import traceback
from datetime import datetime
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Union,
)

from pydantic import BaseModel, Field

from owui_manifolds.filters.context_runtime import prepare_context_for_pipe
from owui_manifolds.shared.history_store import OpenWebUIItemStore
from owui_manifolds.shared.ids import generate_ulid
from owui_manifolds.shared.rendering import (
    format_reasoning_details,
    format_tool_call_details,
)
from owui_manifolds.shared.tools import dispatch_direct_tool

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

try:
    from anthropic import OverloadedError
except ImportError:

    class OverloadedError(Exception):
        pass


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Open WebUI optional integrations (import-guarded so the module loads in any
# runtime, including bare test environments).
# ---------------------------------------------------------------------------
try:
    from open_webui.utils.middleware import process_tool_result

    PROCESS_TOOL_RESULT_AVAILABLE = True
except ImportError:
    process_tool_result = None
    PROCESS_TOOL_RESULT_AVAILABLE = False

try:
    from open_webui.models.chats import Chats

    CHATS_AVAILABLE = True
except ImportError:
    Chats = None
    CHATS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Regex patterns for Open WebUI history reconstruction.
# ---------------------------------------------------------------------------
# RAG / memory scrubbing (Open WebUI injects these into user turns).
PATTERN_USER_CONTEXT = re.compile(r"\nUser Context:\n(.*)$", flags=re.DOTALL)
PATTERN_EMPTY_CONTEXT = re.compile(r"<context>\s*</context>", flags=re.DOTALL)
PATTERN_SOURCE_TAGS = re.compile(r"<source[^>]*>.*?</source>", flags=re.DOTALL)
PATTERN_RAG_MESSAGE = re.compile(r"### Task:.*?<context>.*?</context>", re.DOTALL)

# Assistant history: <details> blocks the pipe itself emitted.
PATTERN_TOOL_CALLS_DETAILS = re.compile(
    r'\n?<details type="tool_calls"[^>]*>.*?</details>\n?',
    flags=re.DOTALL,
)
PATTERN_TOOL_CALLS_BLOCK = re.compile(
    r'\n?<details type="tool_calls"([^>]*)>.*?</details>\n?',
    flags=re.DOTALL,
)
PATTERN_TOOL_CALLS_ATTRS = re.compile(r'(\w+)="([^"]*)"')
PATTERN_REASONING_BLOCK = re.compile(
    r'\n?<details type="reasoning"[^>]*>.*?</details>\n?',
    flags=re.DOTALL,
)


# ---------------------------------------------------------------------------
# Side-table storage for full-size tool call/result payloads.
#
# Rendered <details type="tool_calls"> blocks in message text carry only a
# possibly-truncated preview plus a "ref" attribute pointing at a ULID; the
# full-fidelity tool_use/tool_result blocks live in
# chat.chat["anthropic_pipe"]["items"][ulid], written/read via the Chats
# model. This keeps large tool outputs from being re-embedded (and resent to
# the API) in full on every subsequent turn.
# ---------------------------------------------------------------------------
class ToolResultStore:
    """Persists full-fidelity tool call/result payloads under
    ``chat.chat["anthropic_pipe"]["items"]``, keyed by ULID, via Open WebUI's
    ``Chats`` model. The live ``Chats.get_chat_by_id``/``update_chat_by_id``
    are async (see open_webui/models/chats.py), so this store is async too.
    """

    ROOT_KEY = "anthropic_pipe"
    VERSION = 1

    def __init__(self, chats_model: Any = None):
        self._Chats = chats_model if chats_model is not None else Chats
        self._store = OpenWebUIItemStore(
            root_key=self.ROOT_KEY,
            version=self.VERSION,
            chats_model=self._Chats,
        )

    async def save(self, chat_id: Optional[str], ulid: str, payload: dict) -> bool:
        try:
            return (
                await self._store.save_item(chat_id, payload, ulid=ulid)
            ) is not None
        except Exception as exc:
            logger.warning("ToolResultStore.save failed: %s", exc)
            return False

    async def load(self, chat_id: Optional[str], ulid: str) -> Optional[dict]:
        try:
            return await self._store.load_item(chat_id, ulid)
        except Exception as exc:
            logger.warning("ToolResultStore.load failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Effort → API payload mapping.
#
# One unified EFFORT control drives both extended thinking and output effort:
#   off       -> thinking disabled, no output effort
#   adaptive  -> thinking:{type:"adaptive"}, no output effort
#   low..max  -> output_config.effort=<level> AND thinking:{type:"adaptive"}
#
# All target models (haiku 4.5+, sonnet 4.6+, opus 4.7+, fable 5+) support
# both output effort and adaptive thinking, so no capability clamping is done.
# ---------------------------------------------------------------------------
EFFORT_LEVELS = ("off", "adaptive", "low", "medium", "high", "xhigh", "max")
OUTPUT_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class Pipe:
    API_VERSION = "2023-06-01"
    _DEFAULT_API_BASE = "https://api.anthropic.com"
    _DEFAULT_MAX_TOKENS = 64000
    _DEFAULT_CONTEXT_LENGTH = 200000

    # 24h capability/model-list cache shared across requests.
    _api_capabilities_cache: Dict[str, dict] = {}
    _api_capabilities_cache_ts: float = 0.0
    _API_CACHE_TTL = 86400

    class Valves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="Your API Key Here",
            description="Anthropic API key.",
        )
        ANTHROPIC_BASE_URL: str = Field(
            default="https://api.anthropic.com",
            description="Custom base URL for the Anthropic API.",
        )
        WEB_SEARCH: bool = Field(
            default=True,
            description="Enable Claude's server-side web_search tool.",
        )
        WEB_FETCH: bool = Field(
            default=True,
            description="Enable Claude's server-side web_fetch tool (URL retrieval).",
        )
        MAX_TOOL_CALLS: int = Field(
            default=15,
            ge=1,
            le=9999,
            description=(
                "Maximum provider-only pause_turn/compaction continuations. "
                "Open WebUI owns custom-function loops."
            ),
        )
        MAX_RETRIES: int = Field(
            default=3,
            ge=0,
            le=50,
            description="Max retries for transient stream failures.",
        )
        CACHE_CONTROL: Literal[
            "cache disabled",
            "cache tools array only",
            "cache tools array and system prompt",
            "cache tools array, system prompt and messages",
        ] = Field(
            default="cache tools array, system prompt and messages",
            description="Prompt-cache scope (5-minute ephemeral).",
        )
        REQUEST_TIMEOUT: int = Field(
            default=300,
            ge=30,
            le=9999,
            description="Request timeout in seconds for Anthropic API calls.",
        )
        TOOL_CALL_TIMEOUT: int = Field(
            default=120,
            ge=10,
            le=9999,
            description="Timeout in seconds for individual tool execution.",
        )
        PERSIST_TOOL_RESULTS: bool = Field(
            default=True,
            description=(
                "Deprecated compatibility setting for legacy rendered tool history; "
                "Open WebUI now persists structured tool output."
            ),
        )
        WEB_SEARCH_USER_CITY: str = Field(
            default="", description="Web search: user city."
        )
        WEB_SEARCH_USER_REGION: str = Field(
            default="", description="Web search: user region/state."
        )
        WEB_SEARCH_USER_COUNTRY: str = Field(
            default="", description="Web search: user country code."
        )
        WEB_SEARCH_USER_TIMEZONE: str = Field(
            default="", description="Web search: user timezone."
        )
        LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
            default="INFO",
            description="Logging level.",
        )

    class UserValves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Overrides the admin-configured API key.",
        )
        EFFORT: Literal["off", "adaptive", "low", "medium", "high", "xhigh", "max"] = (
            Field(
                default="adaptive",
                description=(
                    "Reasoning effort. 'off' disables thinking; 'adaptive' lets Claude "
                    "decide; low/medium/high/xhigh/max set output effort with adaptive "
                    "thinking. Also controllable via reasoning_effort."
                ),
            )
        )
        THINKING_DISPLAY: Literal["summarized", "omitted"] = Field(
            default="summarized",
            description="How thinking is rendered: 'summarized' shows it, 'omitted' hides it.",
        )
        WEB_SEARCH_MAX_USES: int = Field(
            default=5, ge=1, le=20, description="Maximum web searches per request."
        )
        WEB_FETCH_MAX_USES: int = Field(
            default=5, ge=1, le=20, description="Maximum web fetches per request."
        )
        WEB_SEARCH_USER_CITY: str = Field(
            default="", description="Web search: user city."
        )
        WEB_SEARCH_USER_REGION: str = Field(
            default="", description="Web search: user region/state."
        )
        WEB_SEARCH_USER_COUNTRY: str = Field(
            default="", description="Web search: user country code."
        )
        WEB_SEARCH_USER_TIMEZONE: str = Field(
            default="", description="Web search: user timezone."
        )

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "anthropic"
        self.valves = self.Valves()
        self.tool_result_store = ToolResultStore()

    # -----------------------------------------------------------------
    # Model listing (dynamic API fetch + 24h capability cache).
    # -----------------------------------------------------------------
    async def pipes(self) -> List[dict]:
        return await self._get_anthropic_models()

    async def _get_anthropic_models(self) -> List[dict]:
        if (
            self._api_capabilities_cache
            and time.time() - self._api_capabilities_cache_ts < self._API_CACHE_TTL
        ):
            return [
                self._build_model_entry(name, info)
                for name, info in self._api_capabilities_cache.items()
            ]

        models: List[dict] = []
        new_cache: Dict[str, dict] = {}
        try:
            client = self._make_client(self.valves.ANTHROPIC_API_KEY)
            async for m in client.models.list():
                name = m.id
                display_name = getattr(m, "display_name", name) or name
                max_tokens = getattr(m, "max_tokens", 0) or 0
                max_input = getattr(m, "max_input_tokens", 0) or 0
                info = {
                    "max_tokens": (
                        max_tokens if max_tokens > 0 else self._DEFAULT_MAX_TOKENS
                    ),
                    "context_length": (
                        max_input if max_input > 0 else self._DEFAULT_CONTEXT_LENGTH
                    ),
                    "supports_vision": True,
                    "supports_thinking": True,
                    "_display_name": display_name,
                }
                new_cache[name] = info
                models.append(self._build_model_entry(name, info, display_name))

            Pipe._api_capabilities_cache = new_cache
            Pipe._api_capabilities_cache_ts = time.time()
            logger.info("Cached %d Anthropic models from API", len(new_cache))
            return models
        except Exception as exc:
            logger.warning("Could not fetch models from Anthropic API: %s", exc)
            if self._api_capabilities_cache:
                return [
                    self._build_model_entry(name, info)
                    for name, info in self._api_capabilities_cache.items()
                ]
            return models

    @staticmethod
    def _build_model_entry(name: str, info: dict, display_name: str = "") -> dict:
        return {
            "id": f"anthropic/{name}",
            "name": display_name or info.get("_display_name") or name,
            "context_length": info.get("context_length", Pipe._DEFAULT_CONTEXT_LENGTH),
            "supports_vision": info.get("supports_vision", True),
            "supports_thinking": info.get("supports_thinking", True),
            "is_hybrid_model": info.get("supports_thinking", True),
            "max_output_tokens": info.get("max_tokens", Pipe._DEFAULT_MAX_TOKENS),
            "info": {"meta": {"capabilities": {"status_updates": True}}},
        }

    def _model_info(self, model_name: str) -> dict:
        info = self._api_capabilities_cache.get(model_name)
        if info:
            return info
        return {
            "max_tokens": self._DEFAULT_MAX_TOKENS,
            "context_length": self._DEFAULT_CONTEXT_LENGTH,
            "supports_vision": True,
            "supports_thinking": True,
        }

    # -----------------------------------------------------------------
    # Client factory.
    # -----------------------------------------------------------------
    def _make_client(self, api_key: str) -> AsyncAnthropic:
        base_url = (self.valves.ANTHROPIC_BASE_URL or "").strip() or None
        return AsyncAnthropic(
            api_key=api_key,
            timeout=self.valves.REQUEST_TIMEOUT,
            max_retries=self.valves.MAX_RETRIES,
            **({"base_url": base_url} if base_url else {}),
        )

    # -----------------------------------------------------------------
    # Thinking / effort config.
    # -----------------------------------------------------------------
    def _resolve_effort(self, body: dict, user_valves: "Pipe.UserValves") -> str:
        raw = body.get("reasoning_effort")
        if raw in EFFORT_LEVELS:
            return raw
        # Open WebUI sends reasoning_effort as low/medium/high; anything else
        # falls back to the user's configured EFFORT valve.
        return user_valves.EFFORT

    def _apply_thinking_config(
        self, payload: dict, body: dict, user_valves: "Pipe.UserValves"
    ) -> None:
        effort = self._resolve_effort(body, user_valves)
        display = user_valves.THINKING_DISPLAY

        if effort == "off":
            payload["thinking"] = {"type": "disabled"}
            return

        # adaptive + all numeric levels use adaptive thinking.
        payload["thinking"] = {"type": "adaptive", "display": display}

        if effort in OUTPUT_EFFORT_LEVELS:
            payload["output_config"] = {"effort": effort}

    # -----------------------------------------------------------------
    # Message conversion (Open WebUI -> Anthropic).
    # -----------------------------------------------------------------
    async def _convert_messages(
        self, raw_messages: list, memory_enabled: bool, chat_id: Optional[str] = None
    ) -> tuple[list[dict], list[dict]]:
        """Return (system_blocks, messages) in Anthropic format."""
        system_blocks: list[dict] = []
        messages: list[dict] = []
        extracted_memories: Optional[str] = None

        if not raw_messages:
            return system_blocks, messages

        for i, msg in enumerate(raw_messages):
            role = msg.get("role")
            raw_content = msg.get("content")

            # OpenAI-style tool result role -> Anthropic tool_result block.
            if role == "tool":
                tool_use_id = msg.get("tool_call_id", "")
                content_str = (
                    raw_content
                    if isinstance(raw_content, str)
                    else (
                        raw_content[0].get("text", "")
                        if isinstance(raw_content, list) and raw_content
                        else ""
                    )
                )
                block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content_str,
                }
                if (
                    messages
                    and messages[-1].get("role") == "user"
                    and isinstance(messages[-1].get("content"), list)
                    and messages[-1]["content"]
                    and isinstance(messages[-1]["content"][0], dict)
                    and messages[-1]["content"][0].get("type") == "tool_result"
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue

            if role == "assistant":
                # Open WebUI preserves this extension when it executes tools
                # and reinvokes the pipe. Restore native thinking signatures
                # and tool_use blocks before adding tool_result messages.
                native_blocks: list[dict[str, Any]] = []
                for detail in msg.get("reasoning_details") or []:
                    if (
                        not isinstance(detail, dict)
                        or detail.get("type") != "anthropic"
                    ):
                        continue
                    for block in detail.get("content") or []:
                        if isinstance(block, dict):
                            native_blocks.append(dict(block))
                if native_blocks:
                    messages.append({"role": "assistant", "content": native_blocks})
                    continue

                # Legacy assistant turns rendered tool calls into HTML.
                if (
                    isinstance(raw_content, str)
                    and '<details type="tool_calls"' in raw_content
                ):
                    parsed = await self._parse_assistant_tool_calls(
                        raw_content, chat_id
                    )
                    if parsed:
                        messages.extend(parsed)
                        continue

            blocks = self._convert_content(raw_content, role=role)
            if role == "assistant":
                for tool_call in msg.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function") or {}
                    name = str(function.get("name") or "")
                    tool_id = str(tool_call.get("id") or "")
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    if name and tool_id:
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": name,
                                "input": arguments,
                            }
                        )
            if not blocks:
                continue

            if role == "system":
                for block in blocks:
                    text = block.get("text", "")
                    if memory_enabled:
                        text, extracted_memories = self._extract_memories(text)
                        block["text"] = text
                    if block.get("text", "").strip():
                        system_blocks.append(block)
                continue

            messages.append({"role": role, "content": blocks})

            # Append memory context to the final user turn.
            if (
                memory_enabled
                and i == len(raw_messages) - 1
                and role == "user"
                and extracted_memories
            ):
                messages[-1]["content"].append(
                    {
                        "type": "text",
                        "text": (
                            "\n\n---\n**IMPORTANT:** The following is NOT part of the "
                            "user's message, but context from a memory system to help "
                            f"answer the user's questions:\n\n{extracted_memories}"
                        ),
                    }
                )

        return system_blocks, messages

    def _convert_content(
        self, content: Union[str, List[dict], None], role: str = "user"
    ) -> List[dict]:
        if content is None:
            return []

        if isinstance(content, str):
            # Strip our own rendered blocks from assistant history — the visible
            # prose is what feeds back into context; reasoning/tool blocks are
            # reconstructed separately (or intentionally dropped).
            if role == "assistant":
                content = PATTERN_TOOL_CALLS_DETAILS.sub("", content)
                content = PATTERN_REASONING_BLOCK.sub("", content)
            text = content.strip()
            return [{"type": "text", "text": text}] if text else []

        processed: List[dict] = []
        for item in content:
            itype = item.get("type")
            if itype == "text":
                text = item.get("text", "")
                if text.strip():
                    processed.append({"type": "text", "text": text})
            elif itype == "image_url":
                block = self._convert_image(item)
                if block:
                    processed.append(block)
            elif itype == "tool_calls":
                for tc in item.get("tool_calls", []):
                    if tc.get("type") == "function" and "function" in tc:
                        fn = tc["function"]
                        processed.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id", ""),
                                "name": fn.get("name", ""),
                                "input": fn.get("arguments", {}),
                            }
                        )
            elif itype == "tool_results":
                for r in item.get("results", []):
                    call = r.get("call") or {}
                    tid = call.get("id", "")
                    if tid and "result" in r:
                        processed.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": str(r["result"]),
                            }
                        )
            else:
                logger.debug("Unknown content type %r -> text", itype)
                processed.append(
                    {"type": "text", "text": f"[Unsupported content type: {itype}]"}
                )
        return processed

    @staticmethod
    def _convert_image(item: dict) -> Optional[dict]:
        image_url = (item.get("image_url") or {}).get("url", "")
        if image_url.startswith("data:image"):
            try:
                header, encoded = image_url.split(",", 1)
                mime_type = header.split(":")[1].split(";")[0]
            except (ValueError, IndexError):
                return {"type": "text", "text": "[Error processing image data URL]"}
            supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
            if mime_type not in supported:
                return {"type": "text", "text": f"[Unsupported image type {mime_type}]"}
            try:
                if len(base64.b64decode(encoded)) > 25 * 1024 * 1024:
                    return {"type": "text", "text": "[Image too large (>25MB)]"}
            except Exception:
                return {"type": "text", "text": "[Invalid base64 image]"}
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": encoded},
            }
        if image_url.startswith(("http://", "https://")):
            return {"type": "image", "source": {"type": "url", "url": image_url}}
        return {"type": "text", "text": f"[Invalid image URL: {image_url}]"}

    async def _parse_assistant_tool_calls(
        self, content: str, chat_id: Optional[str] = None
    ) -> list[dict]:
        """Reconstruct tool_use/tool_result exchange from rendered details blocks."""
        segments: list[tuple[str, str]] = []
        last_end = 0
        for m in PATTERN_TOOL_CALLS_BLOCK.finditer(content):
            segments.append(("text", content[last_end : m.start()]))
            segments.append(("tool_call", m.group(1)))
            last_end = m.end()
        segments.append(("text", content[last_end:]))

        messages: list[dict] = []
        current_assistant: list[dict] = []
        pending_results: list[dict] = []

        def flush() -> None:
            if current_assistant:
                messages.append(
                    {"role": "assistant", "content": list(current_assistant)}
                )
                current_assistant.clear()
            if pending_results:
                messages.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for kind, data in segments:
            if kind == "text":
                if pending_results:
                    flush()
                text = PATTERN_REASONING_BLOCK.sub("", data).strip()
                if text:
                    current_assistant.append({"type": "text", "text": text})
                continue

            attrs = dict(PATTERN_TOOL_CALLS_ATTRS.findall(data))
            tc_id = html.unescape(attrs.get("id", "") or "")
            tc_name = html.unescape(attrs.get("name", "") or "")
            if not tc_id or not tc_name:
                logger.warning("Skipping malformed tool_calls block (missing id/name)")
                continue
            args_raw = html.unescape(attrs.get("arguments", "") or "")
            result_raw = html.unescape(attrs.get("result", "") or "")
            ref = html.unescape(attrs.get("ref", "") or "")
            done = (attrs.get("done", "true") or "true") == "true"
            is_error = (attrs.get("error", "false") or "false") == "true"
            try:
                tc_input = json.loads(args_raw) if args_raw else {}
                if not isinstance(tc_input, dict):
                    tc_input = {}
            except (json.JSONDecodeError, ValueError):
                tc_input = {}

            current_assistant.append(
                {"type": "tool_use", "id": tc_id, "name": tc_name, "input": tc_input}
            )
            if done:
                result_content = result_raw or "(no result)"
                if ref and self.valves.PERSIST_TOOL_RESULTS and chat_id:
                    stored = await self.tool_result_store.load(chat_id, ref)
                    if stored is not None and "output" in stored:
                        output = stored["output"]
                        result_content = (
                            output if isinstance(output, str) else str(output)
                        )
                result_block: dict = {
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": result_content,
                }
                if is_error:
                    result_block["is_error"] = True
            else:
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": "tool execution was interrupted",
                    "is_error": True,
                }
            pending_results.append(result_block)

        flush()
        return messages

    @staticmethod
    def _extract_memories(text: str) -> tuple[str, Optional[str]]:
        match = PATTERN_USER_CONTEXT.search(text)
        if match:
            body = match.group(1).strip()
            extracted = f"User Context:\n{body}" if body else None
            return text[: match.start()].strip(), extracted
        return text.strip(), None

    # -----------------------------------------------------------------
    # RAG source scrubbing.
    # -----------------------------------------------------------------
    def _remove_rag_sources(self, messages: list, filenames: List[str]) -> None:
        if not filenames:
            return
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            new_content: list = []
            modified = False
            for block in msg["content"]:
                if block.get("type") != "text":
                    new_content.append(block)
                    continue
                text = block.get("text", "")
                match = PATTERN_RAG_MESSAGE.search(text)
                if not match:
                    new_content.append(block)
                    continue
                rag = match.group(0)
                for fn in filenames:
                    rag = re.sub(
                        rf'<source[^>]*name="{re.escape(fn)}"[^>]*>.*?</source>\s*',
                        "",
                        rag,
                        flags=re.DOTALL,
                    )
                start, end = match.span()
                if PATTERN_EMPTY_CONTEXT.search(rag) or not PATTERN_SOURCE_TAGS.search(
                    rag
                ):
                    new_text = (text[:start] + text[end:]).strip()
                else:
                    new_text = (text[:start] + rag + text[end:]).strip()
                if new_text:
                    nb = dict(block)
                    nb["text"] = new_text
                    new_content.append(nb)
                modified = True
            if modified:
                messages[i]["content"] = new_content
                return

    # -----------------------------------------------------------------
    # Tool conversion (Open WebUI __tools__ + server tools -> Anthropic).
    # -----------------------------------------------------------------
    def _build_tools(
        self, tools: Optional[dict], body: dict, user_valves: "Pipe.UserValves"
    ) -> list[dict]:
        claude_tools: list[dict] = []
        seen: set[str] = set()
        server_tool_names = {"web_search", "web_fetch"}

        # Client-side function tools from body.tools (schemas).
        for entry in body.get("tools", []) or []:
            if entry.get("type") != "function":
                continue
            fn = entry.get("function", {})
            name = fn.get("name")
            if not name or name in seen or name in server_tool_names:
                continue
            claude_tools.append(
                {
                    "name": name,
                    "description": fn.get("description", f"Tool: {name}"),
                    "input_schema": fn.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )
            seen.add(name)

        # Open WebUI runtime tools (callables) with specs.
        for entry in (tools or {}).values():
            if not isinstance(entry, dict):
                continue
            spec = entry.get("spec") or {}
            name = spec.get("name")
            if not name or name in seen or name in server_tool_names:
                continue
            claude_tools.append(
                {
                    "name": name,
                    "description": spec.get("description", f"Tool: {name}"),
                    "input_schema": spec.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )
            seen.add(name)

        # Server-side web tools (GA variants).
        if self.valves.WEB_SEARCH:
            tool = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": user_valves.WEB_SEARCH_MAX_USES,
            }
            loc = self._web_location(user_valves)
            if loc:
                tool["user_location"] = loc
            claude_tools.append(tool)

        if self.valves.WEB_FETCH:
            claude_tools.append(
                {
                    "type": "web_fetch_20250910",
                    "name": "web_fetch",
                    "max_uses": user_valves.WEB_FETCH_MAX_USES,
                }
            )

        return claude_tools

    def _web_location(self, user_valves: "Pipe.UserValves") -> Optional[dict]:
        city = user_valves.WEB_SEARCH_USER_CITY or self.valves.WEB_SEARCH_USER_CITY
        region = (
            user_valves.WEB_SEARCH_USER_REGION or self.valves.WEB_SEARCH_USER_REGION
        )
        country = (
            user_valves.WEB_SEARCH_USER_COUNTRY or self.valves.WEB_SEARCH_USER_COUNTRY
        )
        tz = (
            user_valves.WEB_SEARCH_USER_TIMEZONE or self.valves.WEB_SEARCH_USER_TIMEZONE
        )
        if not (city or region or country or tz):
            return None
        loc: dict = {"type": "approximate"}
        if city:
            loc["city"] = city
        if region:
            loc["region"] = region
        if country:
            loc["country"] = country
        if tz:
            loc["timezone"] = tz
        return loc

    def _client_tool_names(self, tools: Optional[dict]) -> set[str]:
        names: set[str] = set()
        for entry in (tools or {}).values():
            if not isinstance(entry, dict):
                continue
            # Includes both local-callable tools and "direct" tool-server
            # tools (e.g. Open Terminal's run_command/list_files/etc., which
            # have no local callable and are executed client-side via
            # __event_call__ — see _execute_tool/_execute_direct). Without
            # this, Claude's tool_use blocks for direct tools would be
            # mistaken for server-side tools (web_search/web_fetch) and
            # silently dropped instead of dispatched.
            if entry.get("callable") is not None or entry.get("direct"):
                spec = entry.get("spec") or {}
                if spec.get("name"):
                    names.add(spec["name"])
        return names

    # -----------------------------------------------------------------
    # Prompt caching (5-minute ephemeral).
    # -----------------------------------------------------------------
    @staticmethod
    def _cache_marker() -> dict:
        return {"type": "ephemeral"}

    def _apply_cache_control(self, payload: dict) -> None:
        level = self.valves.CACHE_CONTROL
        if level == "cache disabled":
            return

        # Clear any stale markers first.
        for tool in payload.get("tools", []):
            tool.pop("cache_control", None)
        for block in payload.get("system", []):
            if isinstance(block, dict):
                block.pop("cache_control", None)
        for msg in payload.get("messages", []):
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)

        marker = self._cache_marker()

        tools = payload.get("tools", [])
        if tools:
            tools[-1]["cache_control"] = marker
        if level == "cache tools array only":
            return

        system = payload.get("system", [])
        for block in reversed(system):
            if block.get("type") == "text" and block.get("text", "").strip():
                block["cache_control"] = marker
                break
        if level == "cache tools array and system prompt":
            return

        messages = payload.get("messages", [])
        if messages:
            last = messages[-1]
            content = last.get("content")
            if isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1]["cache_control"] = marker

    # -----------------------------------------------------------------
    # Payload assembly.
    # -----------------------------------------------------------------
    async def _build_payload(
        self,
        *,
        body: dict,
        user_valves: "Pipe.UserValves",
        tools: Optional[dict],
        memory_enabled: bool,
        chat_id: Optional[str] = None,
    ) -> tuple[dict, set[str]]:
        model_name = body["model"].split("/")[-1]
        info = self._model_info(model_name)
        max_tokens = min(body.get("max_tokens", info["max_tokens"]), info["max_tokens"])

        system_blocks, messages = await self._convert_messages(
            body.get("messages", []) or [], memory_enabled, chat_id
        )

        # NOTE: client.messages.stream() is implicitly streaming and rejects a
        # stream= kwarg (unlike .create()). Do not add "stream" to this payload.
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_blocks:
            payload["system"] = system_blocks
        if isinstance(body.get("stop"), list) and body["stop"]:
            payload["stop_sequences"] = body["stop"]

        self._apply_thinking_config(payload, body, user_valves)

        claude_tools = self._build_tools(tools, body, user_valves)
        if claude_tools:
            payload["tools"] = claude_tools

        self._apply_cache_control(payload)
        client_tool_names = {
            str(tool.get("name"))
            for tool in claude_tools
            if tool.get("name")
            and not str(tool.get("type") or "").startswith(
                ("web_search_", "web_fetch_")
            )
        }
        return payload, client_tool_names

    # -----------------------------------------------------------------
    # SDK-typed extractors (parsing lives in the SDK).
    # -----------------------------------------------------------------
    @staticmethod
    def _message_to_api_blocks(message: Any) -> list[dict]:
        """Serialize an SDK Message's content blocks for the next turn."""
        blocks: list[dict] = []
        for block in message.content:
            data = block.model_dump(exclude_none=True, mode="json")
            btype = data.get("type", "")
            if btype == "text":
                data.pop("citations", None)
            if btype == "tool_use":
                caller = data.get("caller")
                if isinstance(caller, dict) and caller.get("type") == "direct":
                    data.pop("caller", None)
            blocks.append(data)
        return blocks

    @staticmethod
    def _tool_calls_from_message(
        message: Any, client_tool_names: set[str]
    ) -> list[dict]:
        """Return client-side tool_use blocks the pipe must execute."""
        calls: list[dict] = []
        for block in message.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = getattr(block, "name", "") or ""
            if name not in client_tool_names:
                continue  # server tools (web_search/web_fetch) run API-side.
            calls.append(
                {
                    "id": getattr(block, "id", "") or "",
                    "name": name,
                    "input": getattr(block, "input", {}) or {},
                }
            )
        return calls

    # -----------------------------------------------------------------
    # Rendering helpers (Open WebUI presentation).
    # -----------------------------------------------------------------
    @staticmethod
    def _format_thinking_block(content: str, signature: str = "") -> str:
        return format_reasoning_details(content, summary="Thought")

    @staticmethod
    def _format_tool_result_block(
        tool_id: str,
        name: str,
        args: dict,
        output: str,
        is_error: bool = False,
        max_chars: int = 4000,
        ref: str = "",
    ) -> str:
        return format_tool_call_details(
            tool_id=tool_id,
            name=name,
            args=args,
            output=output,
            is_error=is_error,
            max_chars=max_chars,
            ref=ref,
        )

    # -----------------------------------------------------------------
    # Tool execution.
    # -----------------------------------------------------------------
    async def _execute_direct_tool(
        self,
        call: dict,
        entry: dict,
        event_call: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]],
        metadata: dict,
    ) -> tuple[Any, bool]:
        """Execute an Open WebUI 'direct' tool-server tool (e.g. Open
        Terminal's run_command/list_files/glob_search/etc.) via the
        __event_call__ round-trip to the browser, exactly as Open WebUI's own
        native middleware does for tool.get('direct') == True entries (see
        utils/middleware.py: tool_call_handler).

        These tools have no local 'callable' — OWUI intentionally executes
        them client-side (auth/session context lives in the browser),
        dispatched via a WebSocket 'execute:tool' event that the frontend's
        executeTool() picks up and forwards to executeToolServer().
        """
        result = await dispatch_direct_tool(
            name=call["name"],
            arguments=call["input"],
            entry=entry,
            event_call=event_call,
            metadata=metadata,
            call_id=call["id"],
        )
        return result.output_text, result.status != "ok"

    async def _execute_tool(
        self,
        call: dict,
        tools: dict,
        request: Any,
        metadata: dict,
        user: dict,
        event_call: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None,
    ) -> tuple[dict, str, list, list, bool]:
        """Return (result_block, rendered_output, files, embeds, is_error)."""
        name = call["name"]
        entry = (tools or {}).get(name) or {}
        fn = entry.get("callable")
        if fn is None:
            if entry.get("direct"):
                result, is_error = await self._execute_direct_tool(
                    call, entry, event_call, metadata
                )
                if isinstance(result, str):
                    result_str = result
                else:
                    try:
                        result_str = json.dumps(result, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        result_str = str(result)
                block = {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": result_str,
                }
                if is_error:
                    block["is_error"] = True
                return block, result_str, [], [], is_error

            err = f"Error: tool '{name}' not found"
            block = {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": err,
                "is_error": True,
            }
            return block, err, [], [], True

        try:
            if inspect.iscoroutinefunction(fn):
                value = await asyncio.wait_for(
                    fn(**call["input"]), timeout=self.valves.TOOL_CALL_TIMEOUT
                )
            else:
                value = await asyncio.wait_for(
                    asyncio.to_thread(fn, **call["input"]),
                    timeout=self.valves.TOOL_CALL_TIMEOUT,
                )
            result: Any = value
            is_error = False
        except Exception as exc:
            result = f"Error executing tool '{name}': {exc}"
            is_error = True

        files: list = []
        embeds: list = []
        if PROCESS_TOOL_RESULT_AVAILABLE and request and not is_error:
            try:
                result, files, embeds = await process_tool_result(
                    request, name, result, "pipe", metadata=metadata, user=user
                )
            except Exception as exc:
                logger.warning("process_tool_result failed for %r: %s", name, exc)

        if isinstance(result, str):
            result_str = result
        else:
            try:
                result_str = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                result_str = str(result)

        block: dict = {
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": result_str,
        }
        if is_error:
            block["is_error"] = True
        return block, result_str, files, embeds, is_error

    # -----------------------------------------------------------------
    # Citations (kept behavior: web_search_result_location -> source event).
    # -----------------------------------------------------------------
    async def _emit_citation(self, citation: Any, emit: Callable, counter: int) -> None:
        try:
            ctype = getattr(citation, "type", "")
            if ctype != "web_search_result_location":
                return
            url = getattr(citation, "url", "")
            title = getattr(citation, "title", "Unknown Source") or "Unknown Source"
            cited_text = getattr(citation, "cited_text", "") or ""
            await emit(
                {
                    "type": "source",
                    "data": {
                        "source": {"name": title, "url": url, "id": str(counter)},
                        "document": [cited_text],
                        "metadata": [
                            {
                                "source": f"{url}#{counter}",
                                "date_accessed": datetime.now().isoformat(),
                                "name": f"[{counter}]",
                            }
                        ],
                    },
                }
            )
        except Exception as exc:
            logger.warning("Failed to emit citation: %s", exc)

    # -----------------------------------------------------------------
    # Error handling.
    # -----------------------------------------------------------------
    async def _handle_error(self, exc: Exception, emit: Callable) -> None:
        if isinstance(exc, RateLimitError):
            user_msg = "⚠️ Rate limit reached. Please try again in a moment."
        elif isinstance(exc, AuthenticationError):
            user_msg = "🔑 Invalid API key. Please verify your Anthropic API key."
        elif isinstance(exc, PermissionDeniedError):
            user_msg = (
                "🚫 Access denied. Your API key lacks permission for this request."
            )
        elif isinstance(exc, NotFoundError):
            user_msg = "❓ Resource not found. Check if the model is available."
        elif isinstance(exc, BadRequestError):
            user_msg = "📝 Invalid request format. Check your input and try again."
        elif isinstance(exc, UnprocessableEntityError):
            user_msg = "📄 Request format issue. Check your message structure."
        elif isinstance(exc, (InternalServerError, OverloadedError)):
            user_msg = "🔧 Server temporarily unavailable. Try again shortly."
        elif isinstance(exc, APIConnectionError):
            user_msg = "🌐 Connection error. Check your network and try again."
        elif isinstance(exc, APIStatusError):
            code = getattr(exc, "status_code", "Unknown")
            user_msg = f"⚡ API Error ({code}). Please try again."
        else:
            user_msg = "💥 An unexpected error occurred. Please try again."

        logger.error("Anthropic error: %s", exc)
        await emit(
            {"type": "notification", "data": {"type": "error", "content": user_msg}}
        )
        await emit(
            {
                "type": "source",
                "data": {
                    "source": {"name": "Anthropic Error", "url": None},
                    "document": [traceback.format_exc()],
                    "metadata": [
                        {
                            "source": "anthropic api",
                            "type": "error",
                            "date_accessed": datetime.utcnow().isoformat(),
                        }
                    ],
                },
            }
        )
        await emit(
            {
                "type": "status",
                "data": {"description": "❌ Response with Errors", "done": True},
            }
        )

    # -----------------------------------------------------------------
    # Task requests (title/tag generation) — non-streaming, no tools.
    # -----------------------------------------------------------------
    async def _run_task(self, body: dict) -> str:
        try:
            model_name = body["model"].split("/")[-1]
            messages: list[dict] = []
            for msg in body.get("messages", []):
                role = msg.get("role")
                if role == "system":
                    continue
                content = msg.get("content", "")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                elif isinstance(content, list):
                    parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    if parts:
                        messages.append({"role": role, "content": " ".join(parts)})

            client = self._make_client(self.valves.ANTHROPIC_API_KEY)
            response = await client.messages.create(
                model=model_name,
                max_tokens=body.get("max_tokens", 4096),
                messages=messages,
            )
            return "".join(
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ).strip()
        except Exception as exc:
            logger.debug("Task model error: %s", exc)
            return ""

    @staticmethod
    def _openai_stream_chunk(
        model: str,
        *,
        delta: Optional[dict[str, Any]] = None,
        finish_reason: Optional[str] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta or {},
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage:
            chunk["usage"] = usage
        return chunk

    async def _stream_native_once(
        self,
        *,
        payload: dict[str, Any],
        client_tool_names: set[str],
        api_key: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Adapt native Anthropic streams while Open WebUI owns tool calls."""

        client = self._make_client(api_key)
        model = str(payload.get("model") or "")
        citation_counter = 0

        try:
            # pause_turn/compaction are provider continuations, not custom
            # function calls. Continue those internally without executing tools.
            for _ in range(self.valves.MAX_TOOL_CALLS + 1):
                async with client.messages.stream(**payload) as stream:
                    try:
                        async for event in stream:
                            event_type = getattr(event, "type", None)
                            if event_type == "text":
                                text = getattr(event, "text", "") or ""
                                if text:
                                    yield self._openai_stream_chunk(
                                        model, delta={"content": text}
                                    )
                            elif event_type == "citation":
                                citation = getattr(event, "citation", None)
                                if citation is not None:
                                    citation_counter += 1
                                    await self._emit_citation(
                                        citation, emit, citation_counter
                                    )
                            elif event_type == "content_block_stop":
                                block = getattr(event, "content_block", None)
                                if getattr(block, "type", None) == "thinking":
                                    thinking = getattr(block, "thinking", "") or ""
                                    if thinking:
                                        yield self._openai_stream_chunk(
                                            model,
                                            delta={"reasoning_content": thinking},
                                        )
                    except ValueError as exc:
                        logger.warning(
                            "Malformed Anthropic tool input JSON; using final snapshot: %s",
                            exc,
                        )
                    message = await stream.get_final_message()

                native_blocks = self._message_to_api_blocks(message)
                tool_calls = self._tool_calls_from_message(message, client_tool_names)
                usage_obj = getattr(message, "usage", None)
                usage = (
                    usage_obj.model_dump(mode="json", exclude_none=True)
                    if hasattr(usage_obj, "model_dump")
                    else None
                )

                if tool_calls:
                    yield self._openai_stream_chunk(
                        model,
                        delta={
                            "reasoning_details": [
                                {
                                    "type": "anthropic",
                                    "index": 0,
                                    "content": native_blocks,
                                }
                            ]
                        },
                    )
                    for index, call in enumerate(tool_calls):
                        yield self._openai_stream_chunk(
                            model,
                            delta={
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": call["id"],
                                        "type": "function",
                                        "function": {
                                            "name": call["name"],
                                            "arguments": json.dumps(
                                                call["input"], ensure_ascii=False
                                            ),
                                        },
                                    }
                                ]
                            },
                        )
                    yield self._openai_stream_chunk(
                        model, finish_reason="tool_calls", usage=usage
                    )
                    return

                stop_reason = getattr(message, "stop_reason", None)
                if stop_reason in {"pause_turn", "compaction"}:
                    payload["messages"].append(
                        {"role": "assistant", "content": native_blocks}
                    )
                    self._apply_cache_control(payload)
                    continue

                suffix = self._stop_reason_suffix(message, stop_reason)
                if suffix:
                    yield self._openai_stream_chunk(model, delta={"content": suffix})
                yield self._openai_stream_chunk(
                    model, finish_reason="stop", usage=usage
                )
                return

            yield {
                "error": {
                    "message": "Anthropic provider continuation limit reached",
                    "type": "continuation_limit",
                }
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_error(exc, emit)
            yield {"error": {"message": str(exc), "type": type(exc).__name__}}

    # -----------------------------------------------------------------
    # Main entry point.
    # -----------------------------------------------------------------
    async def pipe(
        self,
        body: dict[str, Any],
        __user__: Dict[str, Any],
        __event_emitter__: Callable[[Dict[str, Any]], Awaitable[None]],
        __event_call__: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None,
        __metadata__: dict[str, Any] = {},
        __tools__: Optional[Dict[str, Dict[str, Any]]] = None,
        __files__: Optional[Dict[str, Any]] = None,
        __task__: Optional[dict[str, Any]] = None,
        __task_body__: Optional[dict[str, Any]] = None,
        __request__: Optional[Any] = None,
    ) -> Any:
        logger.setLevel(getattr(logging, self.valves.LOG_LEVEL, logging.INFO))
        metadata = __metadata__ or {}

        async def emit(event: dict) -> None:
            if __event_emitter__ is None:
                return
            try:
                await __event_emitter__(event)
            except Exception as exc:
                logger.warning("Event emitter failed: %s", exc)

        async def status(
            description: str, done: bool = False, level: str = "info"
        ) -> None:
            await emit(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "level": level,
                    },
                }
            )

        async def delta(content: str) -> None:
            await emit({"type": "message", "data": {"content": content}})

        async def replace(content: str) -> None:
            await emit({"type": "replace", "data": {"content": content}})

        try:
            user_valves = __user__.get("valves")
            if user_valves is None or not isinstance(user_valves, self.UserValves):
                user_valves = self.UserValves.model_validate(
                    user_valves.model_dump()
                    if hasattr(user_valves, "model_dump")
                    else (user_valves or {})
                )

            user_api_key = (user_valves.ANTHROPIC_API_KEY or "").strip()
            api_key = user_api_key or self.valves.ANTHROPIC_API_KEY
            if not api_key or api_key == "Your API Key Here":
                await status("No API Key Set!", done=True)
                return "Error: No API key configured. Set it in admin Valves or your personal UserValves."

            if __task__:
                return await self._run_task(__task_body__ or body)

            body = await prepare_context_for_pipe(
                body,
                model_id=str(body.get("model") or "")
                .split("/")[-1]
                .removeprefix(f"{self.id}."),
                user=__user__,
                chat_id=metadata.get("chat_id"),
                metadata=metadata,
                event_emitter=__event_emitter__,
            )

            if inspect.isawaitable(__tools__):
                __tools__ = await __tools__
            tools = __tools__ or {}

            user_settings = (__user__ or {}).get("settings") or {}
            user_ui_settings = user_settings.get("ui") or {}
            memory_enabled = bool(user_ui_settings.get("memory", False))

            payload, client_tool_names = await self._build_payload(
                body=body,
                user_valves=user_valves,
                tools=tools,
                memory_enabled=memory_enabled,
                chat_id=metadata.get("chat_id"),
            )

            if body.get("stream", False):
                return self._stream_native_once(
                    payload=payload,
                    client_tool_names=client_tool_names,
                    api_key=api_key,
                    emit=emit,
                )

            return "Error: Streaming is required for Anthropic chat."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_error(exc, emit)
            return f"Error: {exc}"

    @staticmethod
    def _stop_reason_suffix(message: Any, stop_reason: Optional[str]) -> str:
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            explanation = getattr(details, "explanation", None) if details else None
            labels = {
                "cyber": "cybersecurity policy",
                "bio": "biological safety policy",
                "reasoning_extraction": "reasoning extraction policy",
            }
            label = (
                labels.get(category, "content policy") if category else "content policy"
            )
            msg = f"\n\n⚠️ Request declined by Claude ({label})."
            if explanation:
                msg += f"\n\n_{explanation}_"
            return msg
        if stop_reason == "stop_sequence":
            return ""
        if stop_reason == "max_tokens":
            return "\n\n⚠️ Response truncated (max output tokens reached)."
        if stop_reason == "model_context_window_exceeded":
            return "\n\n⚠️ Maximum context window reached for this model."
        return ""


__all__ = ["Pipe"]
