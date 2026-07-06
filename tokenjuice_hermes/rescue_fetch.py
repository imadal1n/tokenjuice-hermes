"""rescuer_fetch tool implementation."""

from __future__ import annotations

from .rescue_grep import grep_text
from .rescue_store import BlobStore

_CONFIG_KEY_MAP: dict[str, str] = {
    "tokenjuice_rescue_store_path": "store_path",
    "tokenjuice_rescue_ttl_hours": "ttl_hours",
    "tokenjuice_rescue_tombstone_ttl_hours": "tombstone_ttl_hours",
    "tokenjuice_rescue_max_store_mb": "max_store_mb",
    "tokenjuice_rescue_fetch_max_chars": "fetch_max_chars",
    "tokenjuice_rescue_full_fetch_max_chars": "full_fetch_max_chars",
    "tokenjuice_rescue_refuse_full_fetch": "refuse_full_fetch",
    "tokenjuice_rescue_grep_max_pattern_len": "grep_max_pattern_len",
    "tokenjuice_rescue_grep_max_line_len": "grep_max_line_len",
    "tokenjuice_rescue_grep_timeout_ms": "grep_timeout_ms",
}


def rescuer_fetch(
    args: dict[str, object],
    *,
    session_id: str = "",
    config: dict[str, object] | None = None,
) -> str:
    """Model-facing fetch tool for rescued blobs.

    Supported modes:
      stat  -- blob metadata
      range -- line slice
      grep  -- bounded pattern search
      full  -- full content (refused over cap)
    """
    normalized = _normalize_config(config or {})
    store = BlobStore(normalized)
    handle = args.get("id", "")
    mode = args.get("mode", "")
    if not isinstance(handle, str):
        return "Error: id must be a string"
    if not isinstance(mode, str):
        return "Error: mode must be a string"

    if mode == "grep":
        return _run_grep(store, handle, args, session_id, normalized)

    start = _int_arg(args.get("start"), 0)
    count = _int_arg(args.get("count"), 20)
    return store.fetch(
        handle,
        mode,
        session_id=session_id,
        start=start,
        count=count,
    )


def _normalize_config(config: dict[str, object]) -> dict[str, object]:
    """Map tokenjuice-prefixed config keys to the keys BlobStore expects."""
    result: dict[str, object] = dict(config)
    for tokenjuice_key, store_key in _CONFIG_KEY_MAP.items():
        if tokenjuice_key in result and store_key not in result:
            result[store_key] = result[tokenjuice_key]
    return result


def _run_grep(
    store: BlobStore,
    handle: str,
    args: dict[str, object],
    session_id: str,
    config: dict[str, object],
) -> str:
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str):
        return "Error: pattern must be a string"
    if not session_id or not store.session_references(handle, session_id):
        return f"Error: handle {handle} not available in this session"
    text = store.blob_text(handle)
    if text is None:
        return f"Error: handle {handle} not found (may have been swept)"
    return grep_text(text, pattern, config)


def _int_arg(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, value)
