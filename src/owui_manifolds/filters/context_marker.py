"""Idempotence markers for shared context preparation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTEXT_PREPARED_KEY = "_owui_context_prepared"
CONTEXT_PREPARED_VERSION = 1


def context_payload_fingerprint(body: dict[str, Any]) -> str:
    """Hash context-bearing request fields deterministically."""

    payload = {
        "messages": body.get("messages") or [],
        "tools": body.get("tools") or [],
        "extra_tools": body.get("extra_tools") or [],
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        serialized = repr(payload)
    return hashlib.sha256(serialized.encode("utf-8", "replace")).hexdigest()


def _marker_metadata(
    body: dict[str, Any], metadata: dict[str, Any] | None
) -> dict[str, Any] | None:
    if metadata is not None:
        return metadata
    value = body.get("metadata")
    return value if isinstance(value, dict) else None


def context_is_prepared(
    body: dict[str, Any], metadata: dict[str, Any] | None = None
) -> bool:
    marker_container = _marker_metadata(body, metadata)
    marker = marker_container.get(CONTEXT_PREPARED_KEY) if marker_container else None
    return (
        isinstance(marker, dict)
        and marker.get("version") == CONTEXT_PREPARED_VERSION
        and marker.get("payload") == context_payload_fingerprint(body)
    )


def mark_context_prepared(
    body: dict[str, Any], metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    marker_container = _marker_metadata(body, metadata)
    if marker_container is None:
        marker_container = body.setdefault("metadata", {})
    marker_container[CONTEXT_PREPARED_KEY] = {
        "version": CONTEXT_PREPARED_VERSION,
        "payload": context_payload_fingerprint(body),
    }
    return body
