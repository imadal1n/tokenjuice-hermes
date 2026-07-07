from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from .compaction_options import (
    PROTECTED_TOOL_NAMES,
    TEXT_FIELDS,
    CompactionMode,
    TokenjuiceOptions,
    parse_options,
    supported_tool_names,
)
from .json_types import (
    FlatJsonObject,
    JsonScalar,
    JsonValue,
    TerminalJsonObject,
    is_error_payload,
    parse_flat_json_object,
)
from .observability import record_compaction, record_rescue
from .rescue_transform import parse_rescue_options, transform_rescue_result

EMBEDDED_DIAGNOSTIC_MARKERS: Final[tuple[str, ...]] = (
    "--- stderr ---",
    "Traceback (most recent call last):",
)


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
    # Exactness guard: protected tools are never rewritten.
    if tool_name in PROTECTED_TOOL_NAMES:
        return None

    options = parse_options(kwargs)
    if options is None:
        return None

    rescue_options = parse_rescue_options(kwargs)
    if rescue_options is not None and tool_name in supported_tool_names(options):
        rescued = transform_rescue_result(result, tool_name, rescue_options)
        if rescued is not None:
            record_rescue(max(0, len(result) - len(rescued)))
            return rescued

    # Terminal compaction path.
    if tool_name in supported_tool_names(options):
        compacted = _transform_terminal_path(result, options)
        if compacted is not None and options.mode == CompactionMode.HEAD_TAIL:
            record_compaction(max(0, len(result) - len(compacted)))
        return compacted

    # Additive rescue path for eligible web/MCP/browser tools.
    if rescue_options is None:
        return None
    rescued = transform_rescue_result(result, tool_name, rescue_options)
    if rescued is not None:
        record_rescue(max(0, len(result) - len(rescued)))
    return rescued


def _transform_terminal_path(
    result: str,
    options: TokenjuiceOptions,
) -> str | None:
    parsed = parse_flat_json_object(result)
    if parsed is None:
        return None

    compacted = _transform_terminal_result(parsed, options)
    if compacted is None:
        return None
    return _dump_json_object(compacted)


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
    if not is_error_payload(payload):
        return options.text_fields

    fields: list[str] = []
    for field in options.text_fields:
        value = payload.get(field)
        if field == "stderr" or _is_embedded_diagnostic_field(field, value):
            continue
        fields.append(field)
    return tuple(fields)


def _is_embedded_diagnostic_field(field: str, value: JsonScalar) -> bool:
    return field == "output" and isinstance(value, str) and _has_embedded_diagnostic(value)


def _has_embedded_diagnostic(text: str) -> bool:
    return any(marker in text for marker in EMBEDDED_DIAGNOSTIC_MARKERS)


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
