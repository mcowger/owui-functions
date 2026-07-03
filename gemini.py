"""
title: Google Gemini API Manifold
id: google_gemini
author: Justin Kropp
author_url: https://github.com/jrkropp
description: Google Gemini API Manifold
required_open_webui_version: 0.6.28
requirements: google-genai~=2.8, pydantic~=2.13
version: 0.1.0
license: MIT
"""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    from google import genai as _genai_module
    from google.genai import types as _genai_types_module
except Exception:
    _genai_module = None
    _genai_types_module = None

genai = _genai_module
types = _genai_types_module


_LOG_LEVELS: tuple[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], ...] = (
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
)
_DEFAULT_LOG_LEVEL = ((os.getenv("GLOBAL_LOG_LEVEL", "INFO") or "INFO").strip().upper())
if _DEFAULT_LOG_LEVEL not in _LOG_LEVELS:
    _DEFAULT_LOG_LEVEL = "INFO"


class PipeValves(BaseModel):
    model_config = ConfigDict(extra="ignore")

    API_KEY: str = Field(
        default=((os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()),
        description="Gemini Developer API key. Defaults to GOOGLE_API_KEY or GEMINI_API_KEY.",
    )
    BASE_URL: str | None = Field(
        default=((os.getenv("GOOGLE_GEMINI_BASE_URL") or "").strip() or None),
        description=(
            "Optional custom Gemini Developer API base URL. Expected format is the scheme + host only, "
            "for example https://generativelanguage.googleapis.com or your proxy origin. Do NOT include "
            "the API version path (/v1, /v1beta, /v1alpha) or resource paths like /models; use API_VERSION "
            "to select the version segment. Leave blank to use the SDK default endpoint."
        ),
    )
    API_VERSION: str | None = Field(
        default=((os.getenv("GOOGLE_GEMINI_API_VERSION") or "").strip() or "v1beta"),
        description=(
            "Gemini Developer API version segment added by the SDK when BASE_URL is unset or points at a "
            "versionless proxy/root. Common values are v1beta, v1alpha, or v1. Leave blank to use the SDK default."
        ),
    )
    MODEL_ID: str = Field(
        default="gemini-3.5-flash, gemini-3.1-pro-preview, gemini-3.1-flash-lite",
        description="Comma-separated Gemini model IDs exposed to Open WebUI.",
    )
    THINKING_LEVEL: Literal["disabled", "minimal", "low", "medium", "high"] = Field(
        default="disabled",
        description="Default Gemini thinking level. 'disabled' omits thinking_config.thinking_level.",
    )
    THINKING_BUDGET: int | None = Field(
        default=None,
        description="Optional Gemini thinking budget. Leave blank to omit.",
    )
    INCLUDE_THOUGHTS: bool = Field(
        default=False,
        description="Request visible thought parts from Gemini when supported.",
    )
    USE_PERMISSIVE_SAFETY: bool = Field(
        default=False,
        description="If enabled, requests BLOCK_NONE for standard Gemini text safety categories.",
    )
    SERVER_TOOL_MODE: Literal["search", "search_code", "maps", "code", "none"] = Field(
        default="search",
        description=(
            "Which server-side Gemini tools to enable. "
            "'search' = Google Search + URL Context (default). "
            "'search_code' = Google Search + URL Context + Code Execution. "
            "'maps' = Google Maps only (cannot combine with any other tool). "
            "'code' = Code Execution only. "
            "'none' = no server-side tools."
        ),
    )
    MAX_TOOL_CALLS: int = Field(
        default=30,
        description="Maximum number of tool calls allowed in a single request.",
    )
    MAX_FUNCTION_CALL_LOOPS: int = Field(
        default=100,
        description="Maximum Gemini tool-call/model-call loops per request.",
    )
    RESPONSE_MIME_TYPE: str | None = Field(
        default=None,
        description="Optional default response_mime_type for Gemini output.",
    )
    MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max retries for transient upstream errors (e.g. 502/503) and connection failures.",
    )
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default=_DEFAULT_LOG_LEVEL,
        description="Logging level.",
    )

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return (value or "").upper()

    @field_validator("BASE_URL", "API_VERSION", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class UserValves(BaseModel):
    model_config = ConfigDict(extra="ignore")

    THINKING_LEVEL: Literal["disabled", "minimal", "low", "medium", "high"] = Field(
        default="disabled",
        description="Gemini thinking level.",
    )
    THINKING_BUDGET: int | None = Field(
        default=None,
        description="Gemini thinking budget tokens. Leave blank to omit.",
    )
    INCLUDE_THOUGHTS: bool = Field(
        default=False,
        description="Show visible thought blocks in the response.",
    )
    SERVER_TOOL_MODE: Literal["search", "search_code", "maps", "code", "none"] = Field(
        default="search",
        description=(
            "Server-side tools to enable. "
            "search = Google Search + URL Context. "
            "search_code = Google Search + URL Context + Code Execution. "
            "maps = Google Maps only. "
            "code = Code Execution only. "
            "none = no server-side tools."
        ),
    )
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level.",
    )


class RuntimeConfig(PipeValves):
    pass


def merge_valves(pipe_valves: PipeValves, user_valves: UserValves) -> RuntimeConfig:
    data = pipe_valves.model_dump()
    data["THINKING_LEVEL"] = user_valves.THINKING_LEVEL
    data["THINKING_BUDGET"] = user_valves.THINKING_BUDGET
    data["INCLUDE_THOUGHTS"] = user_valves.INCLUDE_THOUGHTS
    data["SERVER_TOOL_MODE"] = user_valves.SERVER_TOOL_MODE
    data["LOG_LEVEL"] = user_valves.LOG_LEVEL
    return RuntimeConfig(**data)


def _resolve_server_tools(mode: str) -> tuple[bool, bool, bool, bool]:
    """Return (google_search, google_maps, url_context, code_execution) for a mode.

    Known API restrictions:
    - Maps cannot be combined with Search, URL Context, or Code Execution.
    - Code Execution cannot be combined with Maps (covered above).
    """
    if mode == "search":
        return True, False, True, False
    if mode == "search_code":
        return True, False, True, True
    if mode == "maps":
        return False, True, False, False  # Maps must be alone
    if mode == "code":
        return False, False, False, True
    # "none" or unknown
    return False, False, False, False


def _get_logger(level_name: str) -> logging.Logger:
    logger = logging.getLogger("gemini_pipe")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    return logger


ULID_LENGTH = 16
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DETAILS_BLOCK_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL)


def _ensure_genai_sdk() -> tuple[Any, Any]:
    global genai, types
    if genai is not None and types is not None:
        return genai, types
    try:
        from google import genai as imported_genai
        from google.genai import types as imported_types
    except Exception as exc:
        raise RuntimeError(
            "google-genai is not available in the Open WebUI runtime. "
            "Install the frontmatter requirements for this function or add google-genai~=2.8 to the server environment."
        ) from exc
    genai = imported_genai
    types = imported_types
    return genai, types


def generate_ulid() -> str:
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(ULID_LENGTH))


def strip_details_blocks(text: str) -> str:
    return _DETAILS_BLOCK_RE.sub("", text or "")


class HistoryStore(Protocol):
    async def save_items(
        self, chat_key: dict[str, Any], message_id: str, items: list[dict[str, Any]], model_id: str
    ) -> list[str]:
        ...

    async def load_items(
        self, chat_key: dict[str, Any], item_ids: list[str], model_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        ...

    async def load_assistant_message_item_payloads(
        self, chat_key: dict[str, Any], model_id: str | None = None
    ) -> list[list[dict[str, Any]]]:
        ...


def _message_chain(messages_map: dict[str, dict[str, Any]], current_id: str | None) -> list[dict[str, Any]]:
    if not messages_map or not current_id:
        return []
    current = messages_map.get(current_id)
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    while isinstance(current, dict):
        message_id = current.get("id")
        if isinstance(message_id, str) and message_id in visited:
            break
        if isinstance(message_id, str):
            visited.add(message_id)
        ordered.append(current)
        parent_id = current.get("parentId")
        current = messages_map.get(parent_id) if isinstance(parent_id, str) else None
    ordered.reverse()
    return ordered


class OpenWebUIHistoryStore(HistoryStore):
    VERSION = 1
    MESSAGE_FIELD = "google_gemini_item_ids"

    def __init__(self, chats_model: Any | None = None):
        if chats_model is not None:
            self._Chats = chats_model
        else:
            try:
                from open_webui.models.chats import Chats

                self._Chats = Chats
            except Exception:
                self._Chats = None

    async def save_items(
        self,
        chat_key: dict[str, Any],
        message_id: str,
        items: list[dict[str, Any]],
        model_id: str,
    ) -> list[str]:
        if not self._Chats:
            return []
        chat_id = chat_key.get("chat_id")
        if not chat_id:
            return []
        chat = await self._Chats.get_chat_by_id(chat_id)
        if not chat:
            return []

        root = chat.chat.setdefault("google_gemini_pipe", {"__v": self.VERSION})
        items_store = root.setdefault("items", {})
        messages_index = root.setdefault("messages_index", {})
        bucket = messages_index.setdefault(message_id, {"role": "assistant", "done": True, "item_ids": []})

        now = int(time.time())
        created: list[str] = []
        for payload in items:
            ulid = generate_ulid()
            items_store[ulid] = {
                "model": model_id,
                "created_at": now,
                "payload": payload,
                "message_id": message_id,
            }
            bucket.setdefault("item_ids", []).append(ulid)
            created.append(ulid)

        await self._Chats.update_chat_by_id(chat_id, chat.chat)
        try:
            await self._Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id,
                message_id,
                {self.MESSAGE_FIELD: created},
            )
        except Exception:
            pass
        return created

    async def load_items(
        self,
        chat_key: dict[str, Any],
        item_ids: list[str],
        model_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not self._Chats:
            return {}
        chat_id = chat_key.get("chat_id")
        if not chat_id:
            return {}
        chat = await self._Chats.get_chat_by_id(chat_id)
        if not chat:
            return {}

        items_store = chat.chat.get("google_gemini_pipe", {}).get("items", {})
        loaded: dict[str, dict[str, Any]] = {}
        for ulid in item_ids:
            item = items_store.get(ulid)
            if not item:
                continue
            if model_id is not None and item.get("model") != model_id:
                continue
            payload = item.get("payload")
            if isinstance(payload, dict):
                loaded[ulid] = payload
        return loaded

    async def load_assistant_message_item_payloads(
        self, chat_key: dict[str, Any], model_id: str | None = None
    ) -> list[list[dict[str, Any]]]:
        if not self._Chats:
            return []
        chat_id = chat_key.get("chat_id")
        if not chat_id:
            return []
        chat = await self._Chats.get_chat_by_id(chat_id)
        if not chat:
            return []

        history = chat.chat.get("history", {}) if isinstance(chat.chat, dict) else {}
        messages_map = history.get("messages", {}) if isinstance(history.get("messages"), dict) else {}
        chain = _message_chain(messages_map, history.get("currentId"))

        root = chat.chat.get("google_gemini_pipe", {}) if isinstance(chat.chat, dict) else {}
        items_store = root.get("items", {}) if isinstance(root.get("items"), dict) else {}

        result: list[list[dict[str, Any]]] = []
        for message in chain:
            if message.get("role") != "assistant":
                continue
            item_ids = message.get(self.MESSAGE_FIELD)
            if not isinstance(item_ids, list):
                result.append([])
                continue
            payloads: list[dict[str, Any]] = []
            for ulid in item_ids:
                if not isinstance(ulid, str):
                    continue
                item = items_store.get(ulid)
                if not isinstance(item, dict):
                    continue
                if model_id is not None and item.get("model") != model_id:
                    continue
                payload = item.get("payload")
                if isinstance(payload, dict):
                    payloads.append(payload)
            result.append(payloads)
        return result


def _render_system_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and "text" in content:
        return str(content.get("text", ""))
    if isinstance(content, Iterable) and not isinstance(content, (str, bytes, dict)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return " ".join(part for part in parts if part).strip()
    return str(content or "")


def _guess_mime_type_from_url(url: str) -> str | None:
    path = urlparse(url).path
    guessed, _ = mimetypes.guess_type(path)
    return guessed


def _content_to_json(content: types.Content) -> dict[str, Any]:
    return content.model_dump(mode="json", exclude_none=True)


def _owui_block_to_part(block: Any) -> types.Part:
    if isinstance(block, str):
        return types.Part.from_text(text=block)
    if not isinstance(block, dict):
        return types.Part.from_text(text=str(block))

    kind = block.get("type")
    if kind == "text":
        return types.Part.from_text(text=str(block.get("text", "")))

    if kind == "image_url":
        image_url = (block.get("image_url") or {}).get("url") or ""
        if image_url.startswith("data:"):
            header, _, data = image_url.partition(",")
            mime_type = header.split(";", 1)[0][5:] or "image/jpeg"
            return types.Part.from_bytes(data=base64.b64decode(data), mime_type=mime_type)
        return types.Part.from_uri(file_uri=image_url, mime_type=_guess_mime_type_from_url(image_url))

    if kind == "input_image":
        image_url = block.get("image_url") or ""
        return types.Part.from_uri(file_uri=image_url, mime_type=_guess_mime_type_from_url(image_url))

    return types.Part.from_text(text=json.dumps(block, ensure_ascii=False, default=str))


class HistoryManager:
    def __init__(self, store: HistoryStore):
        self._store = store

    async def build_contents_from_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        chat_key: dict[str, Any] | None,
        model_id: str,
    ) -> tuple[list[types.Content], str | None]:
        contents: list[types.Content] = []
        system_parts: list[str] = []

        assistant_payload_groups = await self._store.load_assistant_message_item_payloads(
            chat_key or {}, model_id=model_id
        )
        assistant_payload_index = 0

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                rendered = _render_system_content(msg.get("content"))
                if rendered:
                    system_parts.append(rendered)
                continue

            if role in {"user", "developer"}:
                raw_content = msg.get("content") or []
                if isinstance(raw_content, str):
                    raw_content = [{"type": "text", "text": raw_content}]
                parts = [_owui_block_to_part(block) for block in raw_content if block is not None]
                if role == "developer":
                    parts = [types.Part.from_text(text="[Developer instruction]\n")] + parts
                if parts:
                    contents.append(types.Content(role="user", parts=parts))
                continue

            if role != "assistant":
                continue

            raw_content = msg.get("content") or ""
            if not isinstance(raw_content, str):
                raw_content = str(raw_content)

            restored_payloads = (
                assistant_payload_groups[assistant_payload_index]
                if assistant_payload_index < len(assistant_payload_groups)
                else []
            )
            assistant_payload_index += 1
            if restored_payloads:
                restored: list[types.Content] = []
                for payload in restored_payloads:
                    try:
                        restored.append(types.Content.model_validate(payload))
                    except Exception:
                        continue
                if restored:
                    contents.extend(restored)
                    continue

            visible = strip_details_blocks(raw_content).strip()
            if visible:
                contents.append(types.Content(role="model", parts=[types.Part.from_text(text=visible)]))

        system_instruction = "\n\n".join(part for part in system_parts if part).strip() or None
        return contents, system_instruction

    async def persist_contents_for_message(
        self,
        *,
        chat_key: dict[str, Any] | None,
        message_id: str | None,
        model_id: str,
        contents: list[types.Content],
        visible_text: str,
    ) -> str:
        if not chat_key or not message_id or not contents:
            return visible_text

        payloads = [_content_to_json(content) for content in contents]
        await self._store.save_items(chat_key, message_id, payloads, model_id)
        return visible_text


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    arguments: dict[str, Any]
    output_text: str
    response_payload: dict[str, Any]
    status: Literal["ok", "error"]
    error_message: str | None = None


@dataclass
class _StreamResult:
    """Outcome of consuming one streamed Gemini response.

    ``model_contents`` holds each streamed chunk's ``Content`` verbatim (never
    merged), so ``thought_signature`` stays attached to its originating part.
    These must be circulated back to the API unchanged, exactly as the SDK's
    own chat session does in ``record_history``.
    """

    model_contents: list["types.Content"]
    tool_calls: list["ToolCall"]
    visible_blocks: list[str]
    final_text: str
    final_text_emitted: bool
    grounding_response: "types.GenerateContentResponse | None"


class OpenWebUIToolRegistry:
    def __init__(self, registry: dict[str, Any] | None):
        self._definitions: dict[str, ToolDefinition] = {}
        for entry in (registry or {}).values():
            if not isinstance(entry, dict):
                continue
            raw_spec = entry.get("spec")
            spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_params = spec.get("parameters")
            params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {"type": "object", "properties": {}}
            self._definitions[name] = ToolDefinition(
                name=name,
                description=str(spec.get("description") or ""),
                parameters=params,
            )

    def iter_definitions(self) -> list[ToolDefinition]:
        return list(self._definitions.values())


class OpenWebUIToolExecutor:
    """Execute tool calls using callables from ``__tools__``.

    Some Open WebUI tool registrations (e.g. Open Terminal's run_command,
    list_files, glob_search, grep_search, write_file, etc. — any "direct"
    tool-server tool) carry no local ``callable`` at all. Open WebUI
    intentionally executes those client-side, in the browser, via a
    WebSocket round-trip (``__event_call__`` -> ``{'type': 'execute:tool',
    ...}``), exactly as its own native middleware does
    (utils/middleware.py: tool_call_handler). ``event_call``/``metadata``
    are threaded through here so this executor can dispatch those tools the
    same way instead of reporting "Tool not found".
    """

    def __init__(
        self,
        registry: dict[str, Any] | None,
        *,
        parallel: bool = True,
        event_call: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self._parallel = parallel
        self._callables: dict[str, Any] = {}
        self._direct_entries: dict[str, dict[str, Any]] = {}
        for entry in (registry or {}).values():
            if not isinstance(entry, dict):
                continue
            raw_spec = entry.get("spec")
            spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue
            fn = entry.get("callable")
            if fn is not None:
                self._callables[name] = fn
            elif entry.get("direct"):
                self._direct_entries[name] = entry
        self._event_call = event_call
        self._metadata = metadata or {}

    async def execute(self, calls: list[ToolCall]) -> list[ToolResult]:
        if not self._parallel or len(calls) <= 1:
            return [await self._execute_one(call) for call in calls]

        seen: set[str] = set()
        has_duplicates = any((call.name in seen) or (seen.add(call.name) or False) for call in calls)
        if has_duplicates:
            return [await self._execute_one(call) for call in calls]

        return list(await asyncio.gather(*(self._execute_one(call) for call in calls)))

    async def _execute_direct(self, call: ToolCall, entry: dict[str, Any]) -> ToolResult:
        if not callable(self._event_call):
            error = (
                f"Tool '{call.name}' is a direct tool-server tool and requires "
                "__event_call__ (browser round-trip) context, which is not "
                "available for this request."
            )
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
                output_text=json.dumps({"error": error}, ensure_ascii=False),
                response_payload={"error": error},
                status="error",
                error_message="__event_call__ not available",
            )

        try:
            result = await self._event_call(
                {
                    "type": "execute:tool",
                    "data": {
                        "id": str(uuid.uuid4()),
                        "name": call.name,
                        "params": call.arguments,
                        "server": entry.get("server", {}),
                        "session_id": self._metadata.get("session_id"),
                    },
                }
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
                output_text=f"Direct tool error: {exc}",
                response_payload={"error": str(exc)},
                status="error",
                error_message=str(exc),
            )

        payload = result if isinstance(result, dict) else {"result": result}
        output_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            arguments=call.arguments,
            output_text=output_text,
            response_payload=payload,
            status="ok",
        )

    def _candidate_names(self, name: str) -> list[str]:
        """Return possible registry keys for a (possibly namespaced) call name.

        Gemini namespaces function declarations under ``default_api`` and some
        proxies surface tool calls as ``default_api:list_files`` (or
        ``default_api.list_files``). Our registry is keyed by the bare tool
        name, so try the raw name first, then progressively strip a leading
        ``namespace:`` / ``namespace.`` segment.
        """
        candidates = [name]
        for sep in (":", "."):
            if sep in name:
                candidates.append(name.rsplit(sep, 1)[-1])
        # De-duplicate while preserving order.
        seen: set[str] = set()
        return [c for c in candidates if c and not (c in seen or seen.add(c))]

    def _resolve(self, name: str) -> tuple[Any | None, dict[str, Any] | None]:
        for candidate in self._candidate_names(name):
            fn = self._callables.get(candidate)
            if fn is not None:
                return fn, None
            entry = self._direct_entries.get(candidate)
            if entry is not None:
                return None, entry
        return None, None

    async def _execute_one(self, call: ToolCall) -> ToolResult:
        fn, direct_entry = self._resolve(call.name)
        if fn is None:
            if direct_entry is not None:
                return await self._execute_direct(call, direct_entry)
            _logger = _get_logger("INFO")
            _logger.warning(
                "Tool not found: call.name=%r; callables=%r direct=%r",
                call.name,
                sorted(self._callables.keys()),
                sorted(self._direct_entries.keys()),
            )
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
                output_text="Tool not found",
                response_payload={"error": "Tool not found"},
                status="error",
                error_message="Tool not found",
            )

        try:
            if inspect.iscoroutinefunction(fn):
                value = await fn(**call.arguments)
            else:
                value = await asyncio.to_thread(fn, **call.arguments)
            payload = value if isinstance(value, dict) else {"result": value}
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
                output_text=json.dumps(value, ensure_ascii=False, default=str),
                response_payload=payload,
                status="ok",
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
                output_text=f"Tool error: {exc}",
                response_payload={"error": str(exc)},
                status="error",
                error_message=str(exc),
            )


class OpenWebUIRuntimeEvents:
    def __init__(self, emitter: Any | None):
        self._emitter = emitter

    async def _emit(self, payload: dict[str, Any]) -> None:
        if not self._emitter:
            return
        if inspect.iscoroutinefunction(self._emitter):
            await self._emitter(payload)
            return
        result = self._emitter(payload)
        if inspect.isawaitable(result):
            await result

    async def status(self, description: str, *, done: bool = False, **extra: Any) -> None:
        await self._emit({"type": "status", "data": {"description": description, "done": done, **extra}})

    async def delta(self, content: str) -> None:
        await self._emit({"type": "chat:message:delta", "data": {"role": "assistant", "content": content}})

    async def replace(self, content: str) -> None:
        await self._emit({"type": "chat:message", "data": {"role": "assistant", "content": content}})

    async def notification(
        self,
        content: str,
        *,
        level: Literal["info", "success", "warning", "error"] = "info",
    ) -> None:
        await self._emit({"type": "notification", "data": {"type": level, "content": content}})

    async def chat_completion(self, data: dict[str, Any]) -> None:
        await self._emit({"type": "chat:completion", "data": data})

    async def source(self, data: dict[str, Any]) -> None:
        await self._emit({"type": "source", "data": data})


def _normalize_model_id(raw_model_id: str, default_model: str) -> str:
    value = (raw_model_id or "").strip() or (default_model or "").strip()
    if not value:
        return ""
    if value.startswith("google_gemini."):
        value = value[len("google_gemini.") :]
    if "." in value:
        prefix, suffix = value.split(".", 1)
        if prefix and suffix.startswith("gemini-"):
            value = suffix
    return value


def _resolve_thinking_level(body: dict[str, Any], cfg: RuntimeConfig) -> str | None:
    raw = body.get("thinking_level") or body.get("reasoning_effort") or cfg.THINKING_LEVEL
    if raw in (None, "", "disabled"):
        return None
    return str(raw).lower()


def _build_thinking_config(body: dict[str, Any], cfg: RuntimeConfig) -> types.ThinkingConfig | None:
    include_thoughts = body.get("include_thoughts")
    if include_thoughts is None:
        include_thoughts = cfg.INCLUDE_THOUGHTS

    thinking_budget = body.get("thinking_budget")
    if thinking_budget is None:
        thinking_budget = cfg.THINKING_BUDGET

    thinking_level = _resolve_thinking_level(body, cfg)
    if thinking_level is None and thinking_budget is None and not include_thoughts:
        return None

    payload: dict[str, Any] = {"include_thoughts": bool(include_thoughts)}
    if thinking_budget is not None:
        payload["thinking_budget"] = int(thinking_budget)
    if thinking_level is not None:
        payload["thinking_level"] = thinking_level.upper()
    return types.ThinkingConfig.model_validate(payload)


def _build_safety_settings(body: dict[str, Any], cfg: RuntimeConfig) -> list[types.SafetySetting] | None:
    if cfg.USE_PERMISSIVE_SAFETY:
        categories = [
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        ]
        return [
            types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for category in categories
        ]

    raw = body.get("safety_settings")
    if not raw:
        return None
    if isinstance(raw, list):
        settings: list[types.SafetySetting] = []
        for item in raw:
            try:
                settings.append(types.SafetySetting.model_validate(item))
            except Exception:
                continue
        return settings or None
    return None


def _tool_config(*, server_tools: bool = False) -> types.ToolConfig:
    if server_tools:
        # VALIDATED is required when include_server_side_tool_invocations is True.
        # AUTO is not supported in that mode per the tool combination docs.
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="VALIDATED"),
            include_server_side_tool_invocations=True,
        )
    return types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO"))


def _build_tools(
    registry: OpenWebUIToolRegistry,
    *,
    google_search: bool = False,
    google_maps: bool = False,
    url_context: bool = False,
    code_execution: bool = False,
) -> list[types.Tool] | None:
    declarations = [
        types.FunctionDeclaration(
            name=definition.name,
            description=definition.description or None,
            parameters_json_schema=definition.parameters,
        )
        for definition in registry.iter_definitions()
    ]
    has_server_tools = google_search or google_maps or url_context
    if has_server_tools:
        # When combining server-side built-in tools with custom functions, all must
        # live in a single Tool object per the tool combination docs.
        combined = types.Tool(
            function_declarations=declarations if declarations else None,
        )
        if google_search:
            combined.google_search = types.GoogleSearch()
        if google_maps:
            combined.google_maps = types.GoogleMaps()
        if url_context:
            combined.url_context = types.UrlContext()
        tools = [combined]
        # Code execution must be a separate Tool object.
        if code_execution:
            tools.append(types.Tool(code_execution=types.ToolCodeExecution()))
        return tools
    if code_execution:
        tools = []
        if declarations:
            tools.append(types.Tool(function_declarations=declarations))
        tools.append(types.Tool(code_execution=types.ToolCodeExecution()))
        return tools
    if not declarations:
        return None
    return [types.Tool(function_declarations=declarations)]


async def _emit_grounding_sources(
    response: types.GenerateContentResponse,
    events: "OpenWebUIRuntimeEvents",
) -> None:
    """Emit grounding chunks as Open WebUI source events (sidebar citations)."""
    try:
        candidate = response.candidates[0] if response.candidates else None
        if not candidate:
            return
        meta = candidate.grounding_metadata
        if not meta:
            return
        chunks = meta.grounding_chunks or []
        seen: set[str] = set()
        for i, chunk in enumerate(chunks):
            web = chunk.web if chunk else None
            if not web or not web.uri:
                continue
            uri = web.uri
            if uri in seen:
                continue
            seen.add(uri)
            title = web.title or uri
            await events.source({
                "source": {"name": title, "url": uri},
                "document": [title],
                "metadata": [{"source": uri}],
            })
    except Exception:
        pass


_TOOL_TYPE_DISPLAY: dict[str, str] = {
    "GOOGLE_SEARCH_WEB": "web_search",
    "GOOGLE_SEARCH": "web_search",
    "GOOGLE_MAPS": "maps_search",
    "URL_CONTEXT": "url_context",
    "FILE_SEARCH": "file_search",
    "CODE_EXECUTION": "code_execution",
}


def _format_server_tool_call_detail(part: Any) -> str:
    """Render a server-side toolCall part as a <details> block."""
    try:
        tc = part.tool_call
        if tc is not None:
            raw_type = str(getattr(tc, "tool_type", "") or "")
            # Strip enum prefix e.g. "ToolType.GOOGLE_SEARCH_WEB" -> "GOOGLE_SEARCH_WEB"
            if "." in raw_type:
                raw_type = raw_type.split(".")[-1]
            display_name = _TOOL_TYPE_DISPLAY.get(raw_type, raw_type.lower() or "web_search")
            args = getattr(tc, "args", {}) or {}
            call_id = str(getattr(tc, "id", "") or "")
            escaped_args = html.escape(json.dumps(args, ensure_ascii=False, default=str))
            return (
                f'<details type="tool_calls" done="true" id="{html.escape(call_id)}" '
                f'name="{html.escape(display_name)}" arguments="{escaped_args}" result="" files="" embeds="">\n'
                "<summary>Tool Executed</summary>\n"
                "</details>\n"
            )
    except Exception:
        pass
    return ""


def _build_generation_config(
    *,
    body: dict[str, Any],
    cfg: RuntimeConfig,
    system_instruction: str | None,
    tools: list[types.Tool] | None,
    server_tools: bool = False,
) -> types.GenerateContentConfig:
    payload: dict[str, Any] = {
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "top_k": body.get("top_k"),
        "max_output_tokens": body.get("max_tokens"),
        "stop_sequences": body.get("stop") if isinstance(body.get("stop"), list) else None,
        "system_instruction": system_instruction,
        "thinking_config": _build_thinking_config(body, cfg),
        "safety_settings": _build_safety_settings(body, cfg),
        "response_mime_type": body.get("response_mime_type") or cfg.RESPONSE_MIME_TYPE,
        "response_schema": body.get("response_schema"),
        "response_json_schema": body.get("response_json_schema"),
        "tools": tools,
        "tool_config": _tool_config(server_tools=server_tools) if tools else None,
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    filtered = {key: value for key, value in payload.items() if value is not None}
    return types.GenerateContentConfig.model_validate(filtered)


def _tool_response_content(result: ToolResult) -> types.Content:
    part = types.Part.from_function_response(name=result.name, response=result.response_payload)
    return types.Content(role="tool", parts=[part])


def _format_tool_call_detail(result: ToolResult) -> str:
    escaped_args = html.escape(json.dumps(result.arguments, ensure_ascii=False, default=str))
    escaped_output = html.escape(json.dumps(result.response_payload, ensure_ascii=False, default=str))
    error_attr = ' error="true"' if result.status != "ok" else ""
    return (
        f'<details type="tool_calls" done="true" id="{html.escape(result.call_id)}" '
        f'name="{html.escape(result.name)}" arguments="{escaped_args}" result="{escaped_output}" files="" embeds=""{error_attr}>\n'
        "<summary>Tool Executed</summary>\n"
        "</details>\n"
    )


def _build_http_options(cfg: RuntimeConfig) -> types.HttpOptions | None:
    payload: dict[str, Any] = {}
    if cfg.BASE_URL:
        payload["base_url"] = cfg.BASE_URL.rstrip("/")
    if cfg.API_VERSION:
        payload["api_version"] = cfg.API_VERSION
    payload["retry_options"] = types.HttpRetryOptions(attempts=cfg.MAX_RETRIES + 1)
    return types.HttpOptions.model_validate(payload)


@asynccontextmanager
async def _gemini_client(cfg: RuntimeConfig):
    _ensure_genai_sdk()
    http_options = _build_http_options(cfg)
    async with genai.Client(api_key=cfg.API_KEY, http_options=http_options).aio as client:
        yield client



class Pipe:
    id = "google_gemini"

    class Valves(PipeValves):
        pass

    class UserValves(UserValves):
        pass

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.history_store = OpenWebUIHistoryStore()
        self.history_manager = HistoryManager(self.history_store)

    async def pipes(self) -> list[dict[str, Any]]:
        models = [model_id.strip() for model_id in (self.valves.MODEL_ID or "").split(",") if model_id.strip()]
        return [{"id": model_id, "name": f"Google Gemini: {model_id}"} for model_id in models]

    async def _resolve_tools(self, __tools__: Any) -> dict[str, Any]:
        if inspect.isawaitable(__tools__):
            return await __tools__
        return __tools__ or {}

    async def _stream_response(
        self,
        *,
        client: Any,
        model_id: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        events: "OpenWebUIRuntimeEvents",
        emit: bool,
        use_code_execution: bool,
    ) -> "_StreamResult":
        """Stream a single Gemini response, emitting parts live in stream order.

        Aggregates all streamed parts into one ``types.Content`` (preserving
        ``thought_signature`` on each part so context survives tool loops) and
        returns the reconstructed response for the caller's tool-call handling.
        """

        model_contents: list[types.Content] = []
        tool_calls: list[ToolCall] = []
        visible_blocks: list[str] = []
        final_text_parts: list[str] = []
        pending_thoughts: list[str] = []
        grounding_response: types.GenerateContentResponse | None = None
        final_text_emitted = False

        async def _flush_thoughts() -> None:
            if not pending_thoughts:
                return
            text = "".join(pending_thoughts).strip()
            pending_thoughts.clear()
            if not text:
                return
            block = (
                '<details type="reasoning" done="true">\n'
                "<summary>Thought</summary>\n"
                f"{html.escape(text)}\n"
                "</details>\n"
            )
            visible_blocks.append(block)
            if emit:
                await events.delta(block)

        stream = await client.models.generate_content_stream(
            model=model_id, contents=contents, config=config
        )
        async for chunk in stream:
            candidate = chunk.candidates[0] if chunk.candidates else None
            if candidate is not None and candidate.grounding_metadata is not None:
                grounding_response = chunk
            content = candidate.content if candidate else None
            if not content or not content.parts:
                continue

            # Keep each chunk's Content verbatim. The SDK never merges streamed
            # chunks (see chats.record_history); rebuilding a single Content
            # detaches thought_signature from its function_call part, which the
            # API rejects on the next turn.
            model_contents.append(content)

            for part in content.parts:
                if part.thought and part.text:
                    pending_thoughts.append(part.text)
                    continue

                # Any non-thought part ends the current thought section.
                await _flush_thoughts()

                if getattr(part, "function_call", None) is not None:
                    fn_call = part.function_call
                    if fn_call.name:
                        tool_calls.append(
                            ToolCall(
                                call_id=fn_call.id or generate_ulid(),
                                name=fn_call.name,
                                arguments=fn_call.args or {},
                            )
                        )
                    continue

                if getattr(part, "tool_call", None) is not None:
                    block = _format_server_tool_call_detail(part)
                    if block:
                        visible_blocks.append(block)
                        if emit:
                            await events.delta(block)
                    continue

                if use_code_execution and getattr(part, "executable_code", None) is not None:
                    ec = part.executable_code
                    code = getattr(ec, "code", "") or ""
                    lang = str(getattr(ec, "language", "") or "python").lower()
                    block = (
                        '<details type="tool_calls" done="true" id="" name="code_execution" '
                        'arguments="" result="" files="" embeds="">\n'
                        "<summary>Code Executed</summary>\n"
                        f"```{lang}\n{html.escape(code)}\n```\n"
                        "</details>\n"
                    )
                    visible_blocks.append(block)
                    if emit:
                        await events.delta(block)
                    continue

                if use_code_execution and getattr(part, "code_execution_result", None) is not None:
                    cr = part.code_execution_result
                    output = getattr(cr, "output", "") or ""
                    outcome = str(getattr(cr, "outcome", "") or "")
                    block = (
                        '<details type="tool_calls" done="true" id="" name="code_result" '
                        'arguments="" result="" files="" embeds="">\n'
                        "<summary>Code Result</summary>\n"
                        f"```\n{html.escape(output)}\n```\n"
                        f"{('Outcome: ' + html.escape(outcome)) if outcome else ''}\n"
                        "</details>\n"
                    )
                    visible_blocks.append(block)
                    if emit:
                        await events.delta(block)
                    continue

                if part.text:
                    final_text_parts.append(part.text)
                    if emit:
                        await events.delta(part.text)
                        final_text_emitted = True

        # Flush any trailing thought text (no visible part followed it).
        await _flush_thoughts()

        return _StreamResult(
            model_contents=model_contents,
            tool_calls=tool_calls,
            visible_blocks=visible_blocks,
            final_text="".join(final_text_parts).strip(),
            final_text_emitted=final_text_emitted,
            grounding_response=grounding_response,
        )

    async def _run_request(
        self,
        *,
        body: dict[str, Any],
        cfg: RuntimeConfig,
        events: OpenWebUIRuntimeEvents,
        tool_registry: OpenWebUIToolRegistry,
        tool_executor: OpenWebUIToolExecutor,
        metadata: dict[str, Any],
        allow_tools: bool,
        persist_history: bool,
    ) -> str:
        _ensure_genai_sdk()
        if not cfg.API_KEY:
            raise ValueError("GOOGLE_API_KEY / GEMINI_API_KEY is not set")

        model_id = _normalize_model_id(str(body.get("model") or ""), cfg.MODEL_ID.split(",", 1)[0].strip())
        if not model_id.startswith("gemini-"):
            raise ValueError(f"Invalid Gemini model id: {model_id}")

        history_key = {"chat_id": metadata.get("chat_id"), "pipe_id": self.id}
        contents, system_instruction = await self.history_manager.build_contents_from_messages(
            messages=body.get("messages") or [],
            chat_key=history_key,
            model_id=model_id,
        )

        use_search, use_maps, use_url_context, use_code_execution = (
            _resolve_server_tools(cfg.SERVER_TOOL_MODE) if allow_tools else (False, False, False, False)
        )
        has_server_tools = use_search or use_maps or use_url_context
        gemini_tools = _build_tools(
            tool_registry,
            google_search=use_search,
            google_maps=use_maps,
            url_context=use_url_context,
            code_execution=use_code_execution,
        ) if allow_tools else None
        config = _build_generation_config(
            body=body,
            cfg=cfg,
            system_instruction=system_instruction,
            tools=gemini_tools,
            server_tools=has_server_tools,
        )

        logger = _get_logger(cfg.LOG_LEVEL)
        visible_blocks: list[str] = []
        persisted_contents: list[types.Content] = []
        tool_calls_executed = 0
        final_text = ""
        final_text_emitted = False

        async with _gemini_client(cfg) as client:
            for loop_index in range(cfg.MAX_FUNCTION_CALL_LOOPS):
                logger.debug("Gemini request loop=%s model=%s", loop_index + 1, model_id)
                stream_result = await self._stream_response(
                    client=client,
                    model_id=model_id,
                    contents=contents,
                    config=config,
                    events=events,
                    emit=True,
                    use_code_execution=use_code_execution,
                )
                model_contents = stream_result.model_contents
                visible_blocks.extend(stream_result.visible_blocks)

                tool_calls = stream_result.tool_calls if allow_tools else []

                # Circulate each streamed chunk's Content back verbatim (including
                # thought_signatures) so context is preserved across iterations.
                # The chunks are the live SDK objects — no serialization round-trip
                # strips thought_signature.  Do NOT add to persisted_contents here
                # — cross-turn history only needs function_response items.
                contents.extend(model_contents)

                if not tool_calls:
                    persisted_contents.extend(model_contents)
                    final_text = stream_result.final_text
                    final_text_emitted = stream_result.final_text_emitted
                    if has_server_tools and stream_result.grounding_response is not None:
                        await _emit_grounding_sources(stream_result.grounding_response, events)
                    break

                if tool_calls_executed + len(tool_calls) > cfg.MAX_TOOL_CALLS:
                    raise RuntimeError("Gemini exceeded MAX_TOOL_CALLS for this request")

                tool_results = await tool_executor.execute(tool_calls)
                tool_calls_executed += len(tool_results)

                for result in tool_results:
                    block = _format_tool_call_detail(result)
                    visible_blocks.append(block)
                    await events.delta(block)
                    tool_content = _tool_response_content(result)
                    contents.append(tool_content)
                    persisted_contents.append(tool_content)
            else:
                # Loop exhausted without a text response — fall through to fallback below.
                pass

            if not final_text and tool_calls_executed > 0:
                fallback_config = _build_generation_config(
                    body=body,
                    cfg=cfg,
                    system_instruction=system_instruction,
                    tools=None,
                    server_tools=False,
                )
                fallback_result = await self._stream_response(
                    client=client,
                    model_id=model_id,
                    contents=contents,
                    config=fallback_config,
                    events=events,
                    emit=not final_text_emitted,
                    use_code_execution=False,
                )
                visible_blocks.extend(fallback_result.visible_blocks)
                persisted_contents.extend(fallback_result.model_contents)
                final_text = fallback_result.final_text or final_text
                if fallback_result.final_text_emitted:
                    final_text_emitted = True

        blocks_text = "".join(visible_blocks)
        visible_text = (blocks_text + ("\n\n" if blocks_text and final_text else "") + final_text).strip()
        if persist_history:
            visible_text = await self.history_manager.persist_contents_for_message(
                chat_key=history_key,
                message_id=metadata.get("message_id"),
                model_id=model_id,
                contents=persisted_contents,
                visible_text=visible_text,
            )
        await events.chat_completion({"content": visible_text, "done": True})
        return visible_text

    async def pipe(
        self,
        body: dict[str, Any] | None = None,
        __user__: dict[str, Any] | None = None,
        __assistant__: dict[str, Any] | None = None,
        __event_emitter__: Any | None = None,
        __event_call__: Any | None = None,
        __tools__: Any | None = None,
        __tasks__: Iterable[Any] | None = None,
        __task__: Any | None = None,
        __task_body__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> Any:
        body = body or {}
        metadata = __metadata__ or {}
        user_valves = self.UserValves.model_validate((__user__ or {}).get("valves") or {})
        cfg = merge_valves(self.valves, user_valves)
        events = OpenWebUIRuntimeEvents(__event_emitter__)
        logger = _get_logger(cfg.LOG_LEVEL)

        try:
            resolved_tools = await self._resolve_tools(__tools__)
            registry = OpenWebUIToolRegistry(resolved_tools)
            executor = OpenWebUIToolExecutor(
                resolved_tools,
                parallel=True,
                event_call=__event_call__,
                metadata=metadata,
            )

            if __task__ is not None:
                return await self._run_request(
                    body=body,
                    cfg=cfg,
                    events=events,
                    tool_registry=registry,
                    tool_executor=executor,
                    metadata=metadata,
                    allow_tools=False,
                    persist_history=False,
                )

            result = await self._run_request(
                body=body,
                cfg=cfg,
                events=events,
                tool_registry=registry,
                tool_executor=executor,
                metadata=metadata,
                allow_tools=True,
                persist_history=True,
            )
            if body.get("stream", False):
                async def _single_chunk() -> Any:
                    if result:
                        yield result

                return _single_chunk()
            return result
        except Exception as exc:
            logger.exception("Gemini pipe failed")
            await events.notification(f"Gemini request failed: {exc}", level="error")
            await events.status("Gemini request failed.", done=True, level="error")
            return f"Error: {exc}"


__all__ = ["Pipe"]

