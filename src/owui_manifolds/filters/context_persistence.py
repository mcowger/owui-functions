"""Chat-meta persistence helpers for context-manager state."""

import hashlib
from typing import Any, Dict, List, Optional

try:
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat, Chats
except ImportError:  # Local unit tests and standalone library use.
    get_async_db_context = None
    Chat = None
    Chats = None

from owui_manifolds.filters.context_constants import CONTEXT_MANAGER_META_KEY


class ContextPersistenceMixin:
    def _is_persistable_chat_id(self, chat_id: Optional[str]) -> bool:
        """Temporary/local/channel chats have no real DB row to persist against."""
        return (
            bool(chat_id)
            and not chat_id.startswith("local:")
            and not chat_id.startswith("channel:")
        )

    def _hash_history_prefix(self, messages: List[dict]) -> str:
        hasher = hashlib.sha256()
        for m in messages:
            hasher.update(str(m.get("role", "")).encode("utf-8", "ignore"))
            hasher.update(b"\x00")
            hasher.update(
                self.extract_text_from_content(m.get("content", "")).encode(
                    "utf-8", "ignore"
                )
            )
            hasher.update(b"\x01")
        return hasher.hexdigest()

    async def _load_context_state(self, chat_id: str) -> Optional[Dict[str, Any]]:
        if Chats is None:
            return None
        try:
            chat_model = await Chats.get_chat_by_id(chat_id)
            if not chat_model:
                return None
            return (chat_model.meta or {}).get(CONTEXT_MANAGER_META_KEY)
        except Exception as e:
            self.debug_log(1, f"Failed to load persisted context state: {e}", "⚠️")
            return None

    async def _save_context_state(self, chat_id: str, state: Dict[str, Any]):
        if get_async_db_context is None or Chat is None:
            return
        try:
            async with get_async_db_context() as session:
                chat_item = await session.get(Chat, chat_id)
                if chat_item is None:
                    return
                chat_item.meta = {
                    **(chat_item.meta or {}),
                    CONTEXT_MANAGER_META_KEY: state,
                }
                await session.commit()
        except Exception as e:
            self.debug_log(1, f"Failed to persist context state: {e}", "⚠️")
