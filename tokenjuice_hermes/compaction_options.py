"""Option parsing for the terminal compaction pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .json_types import JsonScalar

TERMINAL_TOOL_NAMES: Final[frozenset[str]] = frozenset({"terminal", "execute_code"})
PROTECTED_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read_file"})
TEXT_FIELDS: Final[tuple[str, ...]] = ("stdout", "stderr", "output")
MIN_TEXT_CHARS: Final[int] = 4_000
HEAD_LINES: Final[int] = 40
TAIL_LINES: Final[int] = 20
PREVIEW_CHARS: Final[int] = 160
CONFIG_PREFIX: Final[str] = "tokenjuice_"
OPTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "tokenjuice_mode",
        "tokenjuice_min_text_chars",
        "tokenjuice_head_lines",
        "tokenjuice_tail_lines",
        "tokenjuice_preview_chars",
        "tokenjuice_text_fields",
        "tokenjuice_tool_aliases",
        "tokenjuice_rescue_store_path",
        "tokenjuice_rescue_fetch_available",
        "tokenjuice_rescue_min_text_chars",
        "tokenjuice_rescue_tool_min_text_chars",
        "tokenjuice_rescue_tool_names",
        "tokenjuice_rescue_excluded_tools",
        "tokenjuice_rescue_text_fields",
        "tokenjuice_rescue_ttl_hours",
        "tokenjuice_rescue_tombstone_ttl_hours",
        "tokenjuice_rescue_max_store_mb",
        "tokenjuice_rescue_fetch_max_chars",
        "tokenjuice_rescue_full_fetch_max_chars",
        "tokenjuice_rescue_refuse_full_fetch",
        "tokenjuice_rescue_grep_max_pattern_len",
        "tokenjuice_rescue_grep_max_line_len",
        "tokenjuice_rescue_grep_timeout_ms",
        "tokenjuice_passref_enabled",
        "tokenjuice_passref_max_chars",
        "tokenjuice_passref_total_max_chars",
    }
)


class CompactionMode(StrEnum):
    HEAD_TAIL = "head_tail"
    METADATA = "metadata"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class TokenjuiceOptions:
    mode: CompactionMode = CompactionMode.HEAD_TAIL
    min_text_chars: int = MIN_TEXT_CHARS
    head_lines: int = HEAD_LINES
    tail_lines: int = TAIL_LINES
    preview_chars: int = PREVIEW_CHARS
    text_fields: tuple[str, ...] = TEXT_FIELDS
    tool_aliases: frozenset[str] = frozenset()


def parse_options(kwargs: dict[str, JsonScalar]) -> TokenjuiceOptions | None:
    """Parse flat tokenjuice kwargs into validated options."""
    tokenjuice_kwargs = {
        key: value for key, value in kwargs.items() if key.startswith(CONFIG_PREFIX)
    }
    if not set(tokenjuice_kwargs).issubset(OPTION_KEYS):
        return None
    return _build_options(tokenjuice_kwargs)


def supported_tool_names(options: TokenjuiceOptions) -> frozenset[str]:
    return TERMINAL_TOOL_NAMES | (options.tool_aliases - PROTECTED_TOOL_NAMES)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _build_options(values: dict[str, JsonScalar]) -> TokenjuiceOptions | None:
    mode = _parse_mode(values.get("tokenjuice_mode"))
    min_text_chars = _parse_nonnegative_int(values.get("tokenjuice_min_text_chars"), MIN_TEXT_CHARS)
    head_lines = _parse_nonnegative_int(values.get("tokenjuice_head_lines"), HEAD_LINES)
    tail_lines = _parse_nonnegative_int(values.get("tokenjuice_tail_lines"), TAIL_LINES)
    preview_chars = _parse_nonnegative_int(values.get("tokenjuice_preview_chars"), PREVIEW_CHARS)
    text_fields = _parse_string_tuple(values.get("tokenjuice_text_fields"), TEXT_FIELDS)
    tool_aliases = _parse_string_tuple(values.get("tokenjuice_tool_aliases"), ())
    if (
        mode is None
        or min_text_chars is None
        or head_lines is None
        or tail_lines is None
        or preview_chars is None
        or text_fields is None
        or tool_aliases is None
    ):
        return None
    return TokenjuiceOptions(
        mode=mode,
        min_text_chars=min_text_chars,
        head_lines=head_lines,
        tail_lines=tail_lines,
        preview_chars=preview_chars,
        text_fields=text_fields,
        tool_aliases=frozenset(tool_aliases),
    )


def _parse_mode(value: JsonScalar) -> CompactionMode | None:
    if value is None:
        return CompactionMode.HEAD_TAIL
    if not isinstance(value, str):
        return None
    try:
        return CompactionMode(value)
    except ValueError:
        return None


def _parse_nonnegative_int(value: JsonScalar, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_string_tuple(value: JsonScalar, default: tuple[str, ...]) -> tuple[str, ...] | None:
    if value is None:
        return default
    if not isinstance(value, str):
        return None
    return _split_csv(value)
