from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .json_types import (
    FlatJsonObject,
    JsonScalar,
    JsonValue,
    TerminalJsonObject,
    is_error_payload,
    parse_flat_json_object,
)

TERMINAL_TOOL_NAMES: Final[frozenset[str]] = frozenset({"terminal", "execute_code"})
PROTECTED_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read_file"})
TEXT_FIELDS: Final[tuple[str, ...]] = ("stdout", "stderr", "output")
MIN_TEXT_CHARS: Final[int] = 240
HEAD_LINES: Final[int] = 3
TAIL_LINES: Final[int] = 2
PREVIEW_CHARS: Final[int] = 72
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


@dataclass(frozen=True, slots=True)
class CompactText:
    text: str
    omitted_lines: int
    meta: dict[str, JsonValue]


def transform_tool_result(
    result: str = "",
    *,
    tool_name: str = "",
    **kwargs: JsonScalar,
) -> str | None:
    if tool_name in PROTECTED_TOOL_NAMES:
        return None

    options = _parse_options(kwargs)
    if options is None:
        return None

    if tool_name not in _supported_tool_names(options):
        return None

    parsed = parse_flat_json_object(result)
    if parsed is None:
        return None

    compacted = _transform_terminal_result(parsed, options)
    if compacted is None:
        return None
    return _dump_json_object(compacted)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_options(kwargs: dict[str, JsonScalar]) -> TokenjuiceOptions | None:
    tokenjuice_kwargs = {
        key: value for key, value in kwargs.items() if key.startswith(CONFIG_PREFIX)
    }
    if not set(tokenjuice_kwargs).issubset(OPTION_KEYS):
        return None
    return _build_options(tokenjuice_kwargs)


def _build_options(values: FlatJsonObject) -> TokenjuiceOptions | None:
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


def _supported_tool_names(options: TokenjuiceOptions) -> frozenset[str]:
    return TERMINAL_TOOL_NAMES | (options.tool_aliases - PROTECTED_TOOL_NAMES)


def _dump_json_object(payload: TerminalJsonObject) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _transform_terminal_result(
    payload: FlatJsonObject,
    options: TokenjuiceOptions,
) -> TerminalJsonObject | None:
    match options.mode:
        case CompactionMode.HEAD_TAIL:
            return _compact_terminal_result(payload, options)
        case CompactionMode.METADATA:
            return _metadata_terminal_result(payload, options)
        case CompactionMode.OFF:
            return None


def _compact_terminal_result(
    payload: FlatJsonObject,
    options: TokenjuiceOptions,
) -> TerminalJsonObject | None:
    next_payload: TerminalJsonObject = dict(payload)
    fields: dict[str, JsonValue] = {}
    text_fields = _compactable_text_fields(payload, options)

    for field in text_fields:
        value = payload.get(field)
        if isinstance(value, str):
            compacted = _compact_text(value, options)
            if compacted is not None:
                next_payload[field] = compacted.text
                fields[field] = compacted.meta

    if not fields:
        return None

    next_payload["tokenjuice"] = _tokenjuice_meta(
        compacted=True,
        payload=payload,
        mode=options.mode,
        fields=fields,
    )
    return next_payload


def _metadata_terminal_result(
    payload: FlatJsonObject,
    options: TokenjuiceOptions,
) -> TerminalJsonObject | None:
    fields: dict[str, JsonValue] = {}

    for field in options.text_fields:
        value = payload.get(field)
        if isinstance(value, str):
            meta = _field_meta(value, 0, options.preview_chars)
            if _should_transform_text(value, options):
                fields[field] = meta

    if not fields:
        return None

    next_payload: TerminalJsonObject = dict(payload)
    next_payload["tokenjuice"] = _tokenjuice_meta(
        compacted=False,
        payload=payload,
        mode=options.mode,
        fields=fields,
    )
    return next_payload


def _compactable_text_fields(
    payload: FlatJsonObject,
    options: TokenjuiceOptions,
) -> tuple[str, ...]:
    if is_error_payload(payload):
        return tuple(field for field in options.text_fields if field != "stderr")
    return options.text_fields


def _compact_text(text: str, options: TokenjuiceOptions) -> CompactText | None:
    lines = text.splitlines()
    if not _should_transform_text(text, options):
        return None

    head = lines[: options.head_lines]
    tail = lines[-options.tail_lines :] if options.tail_lines else []
    omitted = len(lines) - len(head) - len(tail)
    if omitted <= 0:
        return CompactText(
            text=text,
            omitted_lines=0,
            meta=_field_meta(text, 0, options.preview_chars),
        )
    compacted = "\n".join(
        [
            *head,
            f"[tokenjuice-hermes: omitted {omitted} middle lines]",
            *tail,
        ]
    )
    return CompactText(
        text=compacted,
        omitted_lines=omitted,
        meta=_field_meta(text, omitted, options.preview_chars),
    )


def _should_transform_text(text: str, options: TokenjuiceOptions) -> bool:
    lines = text.splitlines()
    min_text_lines = options.head_lines + options.tail_lines + 1
    return len(text) >= options.min_text_chars or len(lines) >= min_text_lines


def _field_meta(text: str, omitted_lines: int, preview_chars: int) -> dict[str, JsonValue]:
    return {
        "original_chars": len(text),
        "original_lines": len(text.splitlines()),
        "omitted_lines": omitted_lines,
        "preview": text[:preview_chars],
    }


def _tokenjuice_meta(
    *,
    compacted: bool,
    payload: FlatJsonObject,
    mode: CompactionMode,
    fields: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "compacted": compacted,
        "original_chars": _sum_text_chars(payload),
        "mode": mode.value,
        "fields": fields,
    }


def _sum_text_chars(payload: FlatJsonObject) -> int:
    total = 0
    for field in TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            total += len(value)
    return total
