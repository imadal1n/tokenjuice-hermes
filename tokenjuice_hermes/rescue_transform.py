"""Additive rescue transform for oversized web/MCP/browser tool results.

This module is kept separate from terminal compaction so each pipeline stage
stays small and the rescue path can evolve without touching the existing
terminal-like compaction behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .json_types import FlatJsonObject, JsonScalar, is_error_payload, parse_flat_json_object
from .rescue_excerpt import build_excerpt
from .rescue_store import BlobStore
from .rescue_types import RESCUE_STORE_PATH_DEFAULT

RESCUE_TOOL_NAMES: Final[frozenset[str]] = frozenset({"web_search", "mcp_tool", "browser_snapshot"})
RESCUE_TEXT_FIELDS: Final[tuple[str, ...]] = ("content", "results", "snapshot", "output", "stdout")
RESCUE_EXCLUDED_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "tokenjuice",
    "rescuer",
    "memory",
    "delegate",
    "session",
)
MIN_TEXT_CHARS: Final[int] = 4_000


@dataclass(frozen=True, slots=True)
class RescueOptions:
    session_id: str = ""
    store_path: str = ""
    fetch_available: bool = True
    min_text_chars: int = MIN_TEXT_CHARS
    tool_min_text_chars: dict[str, int] = field(default_factory=dict)
    tool_names: frozenset[str] = RESCUE_TOOL_NAMES
    excluded_tools: frozenset[str] = frozenset()
    text_fields: tuple[str, ...] = RESCUE_TEXT_FIELDS


def transform_rescue_result(
    result: str,
    tool_name: str,
    options: RescueOptions,
) -> str | None:
    """Rescue an oversized eligible tool result, or return None to fail open."""
    parsed = _can_rescue(tool_name, parse_flat_json_object(result), options)
    if parsed is None:
        return None

    store_path = options.store_path or RESCUE_STORE_PATH_DEFAULT
    if not Path(store_path).exists():
        return None

    oversized = _find_oversized_field(parsed, tool_name, options)
    if oversized is None:
        return None
    _field, text = oversized

    try:
        store = BlobStore({"store_path": store_path})
        handle = store.put(text, tool_name=tool_name, session_id=options.session_id)
    except (OSError, ValueError):
        return None

    if not handle:
        return None

    return build_excerpt(text, handle=handle)


def _can_rescue(
    tool_name: str,
    parsed: FlatJsonObject | None,
    options: RescueOptions,
) -> FlatJsonObject | None:
    if parsed is None or is_error_payload(parsed):
        return None
    if not options.session_id or not options.fetch_available:
        return None
    if not _is_eligible_tool(tool_name, options):
        return None
    return parsed


def _is_eligible_tool(tool_name: str, options: RescueOptions) -> bool:
    if tool_name not in options.tool_names or tool_name in options.excluded_tools:
        return False
    lowered = tool_name.lower()
    return not any(lowered.startswith(prefix) for prefix in RESCUE_EXCLUDED_TOOL_PREFIXES)


def parse_rescue_options(kwargs: dict[str, JsonScalar]) -> RescueOptions | None:
    """Build rescue options from flat hook kwargs, returning None if invalid."""
    session_id = kwargs.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""

    store_path = _str_arg(kwargs.get("tokenjuice_rescue_store_path"), default="")
    fetch_available = _bool_arg(kwargs.get("tokenjuice_rescue_fetch_available"), default=False)
    min_text_chars = _nonnegative_int_arg(
        kwargs.get("tokenjuice_rescue_min_text_chars"), default=MIN_TEXT_CHARS
    )
    tool_min_text_chars = _tool_threshold_map_arg(
        kwargs.get("tokenjuice_rescue_tool_min_text_chars"), default={}
    )
    tool_names = _str_set_arg(kwargs.get("tokenjuice_rescue_tool_names"), default=RESCUE_TOOL_NAMES)
    excluded_tools = _str_set_arg(
        kwargs.get("tokenjuice_rescue_excluded_tools"), default=frozenset()
    )
    text_fields = _str_tuple_arg(
        kwargs.get("tokenjuice_rescue_text_fields"), default=RESCUE_TEXT_FIELDS
    )

    if (
        fetch_available is None
        or min_text_chars is None
        or tool_min_text_chars is None
        or tool_names is None
        or excluded_tools is None
        or text_fields is None
    ):
        return None

    return RescueOptions(
        session_id=session_id,
        store_path=store_path,
        fetch_available=fetch_available,
        min_text_chars=min_text_chars,
        tool_min_text_chars=tool_min_text_chars,
        tool_names=tool_names,
        excluded_tools=excluded_tools,
        text_fields=text_fields,
    )


def _find_oversized_field(
    parsed: FlatJsonObject,
    tool_name: str,
    options: RescueOptions,
) -> tuple[str, str] | None:
    threshold = options.tool_min_text_chars.get(tool_name, options.min_text_chars)
    for field_name in options.text_fields:
        value = parsed.get(field_name)
        if isinstance(value, str) and len(value) >= threshold:
            return field_name, value
    return None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _str_arg(value: JsonScalar, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        return ""
    return value


def _bool_arg(value: JsonScalar, *, default: bool) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return None


def _nonnegative_int_arg(value: JsonScalar, *, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _str_set_arg(value: JsonScalar, *, default: frozenset[str]) -> frozenset[str] | None:
    if value is None:
        return default
    if isinstance(value, str):
        return frozenset(_split_csv(value))
    return None


def _str_tuple_arg(value: JsonScalar, *, default: tuple[str, ...]) -> tuple[str, ...] | None:
    if value is None:
        return default
    if not isinstance(value, str):
        return None
    return _split_csv(value)


def _tool_threshold_map_arg(value: JsonScalar, *, default: dict[str, int]) -> dict[str, int] | None:
    """Parse a comma-separated `tool=threshold` map, returning None if malformed."""
    if value is None:
        return default
    if not isinstance(value, str):
        return None
    result: dict[str, int] = {}
    for item in _split_csv(value):
        parsed = _parse_threshold_item(item)
        if parsed is None:
            return None
        tool, threshold = parsed
        result[tool] = threshold
    return result


def _parse_threshold_item(item: str) -> tuple[str, int] | None:
    """Parse a single `tool=threshold` pair, returning None if malformed."""
    if "=" not in item:
        return None
    tool_part, _, threshold_part = item.partition("=")
    tool = tool_part.strip()
    if not tool:
        return None
    try:
        threshold = int(threshold_part.strip())
    except ValueError:
        return None
    if threshold < 0:
        return None
    return tool, threshold
