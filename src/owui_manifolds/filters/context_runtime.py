"""Shared Context Manager adapter used by provider pipe request boundaries."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from owui_manifolds.filters.context import ContextManager
from owui_manifolds.filters.context_marker import (
    context_is_prepared,
    mark_context_prepared,
)
from owui_manifolds.filters.context_tooling import ContextBudgetExceededError
from owui_manifolds.filters.context_valves import ContextUserValves, ContextValves

CONTEXT_MANAGER_FUNCTION_ID = "context_window_manager_simplified"
_LOGGER = logging.getLogger("owui.context.runtime")


async def _load_context_valves(
    user_id: str | None,
) -> tuple[ContextValves, ContextUserValves]:
    """Read the thin Context Manager function's centralized valve configuration."""

    try:
        from open_webui.models.functions import Functions

        admin_values = (
            await Functions.get_function_valves_by_id(CONTEXT_MANAGER_FUNCTION_ID) or {}
        )
        user_values = (
            await Functions.get_user_valves_by_id_and_user_id(
                CONTEXT_MANAGER_FUNCTION_ID, user_id
            )
            if user_id
            else {}
        ) or {}
        return (
            ContextValves.model_validate(admin_values),
            ContextUserValves.model_validate(user_values),
        )
    except Exception:
        # Standalone tests or installations without the thin filter use library
        # defaults. A missing summary credential simply leaves oversized input
        # unchanged rather than truncating it.
        return ContextValves(), ContextUserValves()


async def prepare_context_for_pipe(
    body: dict[str, Any],
    *,
    model_id: str,
    user: dict[str, Any] | None,
    chat_id: str | None,
    metadata: dict[str, Any] | None = None,
    event_emitter: Any | None = None,
) -> dict[str, Any]:
    """Apply shared context preparation once for the current message snapshot.

    A fingerprint marker makes the initial filter+pipe path idempotent. Open
    WebUI copies the old marker into recursive tool requests, but their messages
    differ, so each newly accumulated tool batch is budgeted before the next
    provider call.
    """

    if context_is_prepared(body, metadata):
        return body

    prepared = deepcopy(body)
    had_body_metadata = "metadata" in prepared
    if metadata is not None:
        prepared["metadata"] = metadata
    admin_valves, user_valves = await _load_context_valves(
        str((user or {}).get("id") or "") or None
    )
    # Never interrupt an agentic turn with the filter's optional confirmation
    # dialog. Crossing the hard budget still compacts automatically.
    admin_valves = admin_valves.model_copy(update={"enable_warning_prompt": False})
    manager = ContextManager()
    manager.valves = admin_valves

    context_user = {"valves": user_valves}
    model = {"info": {"base_model_id": model_id}}
    try:
        prepared = await manager.inlet(
            prepared,
            user=context_user,
            __user__=context_user,
            __model__=model,
            __chat_id__=chat_id,
            __event_emitter__=event_emitter,
            __event_call__=None,
        )
        # The pipe receives OWUI's recursive form_data object by reference.
        # Updating it lets the next loop iteration reuse compacted request
        # context while OWUI's separately persisted typed output stays intact.
        if metadata is not None and not had_body_metadata:
            prepared.pop("metadata", None)
        body.clear()
        body.update(prepared)
        mark_context_prepared(body, metadata)
        return body
    except ContextBudgetExceededError:
        raise
    except Exception:
        _LOGGER.exception("Shared context preparation failed; using original request")
        return mark_context_prepared(body, metadata)
