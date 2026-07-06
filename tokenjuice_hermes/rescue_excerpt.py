"""Excerpt builders for rescued tool results.

Produces compact, content-type-aware previews that explicitly tell the model
that the inline text is a preview, not the full content.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from .json_types import JsonValue

_EXCERPT_NOTICE: Final[str] = (
    "Preview only — this is NOT the full content. Use "
    "rescuer_fetch(id='{handle}', mode='full') for the complete result."
)


def detect_type(raw: str) -> tuple[str, str]:
    """Return (kind, short_meta) for the raw text."""
    stripped = raw.lstrip()
    if not stripped:
        return ("text", "empty")
    if stripped[0] in "{[":
        kind = _try_json_kind(raw)
        if kind is not None:
            return kind
    if re.match(
        r"^\s*<!DOCTYPE\s+html|<html|<body|<div|<table|<svg|<xml",
        stripped,
        re.IGNORECASE,
    ):
        tag = re.match(r"^\s*<(\w+)", stripped, re.IGNORECASE)
        tag_name = tag.group(1).lower() if tag else "?"
        return ("html", f"<{tag_name}>")
    return ("text", f"{len(raw):,} chars")


def _try_json_kind(raw: str) -> tuple[str, str] | None:
    try:
        obj = cast("JsonValue", json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, list):
        return ("json", f"array[{len(obj)}]")
    if isinstance(obj, dict):
        keys = list(obj.keys())[:3]
        return ("json", f"object keys: {keys}")
    return ("json", type(obj).__name__)


def build_excerpt(
    raw: str,
    kind: str | None = None,
    cfg: dict[str, object] | None = None,
    *,
    handle: str = "...",
) -> str:
    """Build a preview of *raw* that is explicitly not the full content."""
    config = cfg if cfg is not None else {}
    head_lines = _int_or(config.get("head_lines"), 40)
    tail_lines = _int_or(config.get("tail_lines"), 15)
    json_head_items = _int_or(config.get("json_head_items"), 5)
    json_tail_items = _int_or(config.get("json_tail_items"), 2)
    excerpt_max_chars = _int_or(config.get("excerpt_max_chars"), 8_000)

    detected_kind, _ = detect_type(raw) if kind is None else (kind, "")

    if detected_kind == "json":
        body = _json_excerpt(raw, json_head_items, json_tail_items, excerpt_max_chars)
    else:
        body = _text_excerpt(raw, head_lines, tail_lines, excerpt_max_chars)

    return (
        f"[tokenjuice-hermes: tool result rescued. type={detected_kind}; "
        f"size={len(raw):,} chars; lines={len(raw.splitlines()):,}]\n"
        f"{_EXCERPT_NOTICE.format(handle=handle)}\n"
        f"--- preview ---\n"
        f"{body}"
    )


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _json_excerpt(raw: str, head_items: int, tail_items: int, max_chars: int) -> str:
    try:
        obj = cast("JsonValue", json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return _text_excerpt(raw, 40, 15, max_chars)

    head = _safe_json_head(obj, head_items)
    tail = _safe_json_tail(obj, tail_items)
    n_items = len(obj) if isinstance(obj, (list, dict)) else 0
    show_tail = n_items > head_items + tail_items

    parts = [f"[JSON excerpt: {_json_desc(obj)}]"]
    parts.append("--- head ---")
    parts.append(json.dumps(head, indent=2, ensure_ascii=False))
    if show_tail:
        parts.append("--- tail ---")
        parts.append(json.dumps(tail, indent=2, ensure_ascii=False))
    return "\n".join(parts)[:max_chars]


def _text_excerpt(raw: str, head_lines: int, tail_lines: int, max_chars: int) -> str:
    lines = raw.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return raw[:max_chars]
    head = lines[:head_lines]
    tail = lines[-tail_lines:] if tail_lines else []
    omitted = len(lines) - len(head) - len(tail)
    parts = ["--- head ---"]
    parts.extend(head)
    parts.append(f"[tokenjuice-hermes: omitted {omitted} middle lines]")
    parts.extend(tail)
    return "\n".join(parts)[:max_chars]


def _safe_json_head(obj: JsonValue, n: int) -> JsonValue:
    if isinstance(obj, list):
        return obj[:n]
    if isinstance(obj, dict):
        return dict(list(obj.items())[:n])
    return str(obj)[:2000]


def _safe_json_tail(obj: JsonValue, n: int) -> JsonValue:
    if isinstance(obj, list):
        return obj[-n:] if n > 0 else []
    if isinstance(obj, dict):
        return dict(list(obj.items())[-n:]) if n > 0 else {}
    return ""


def _json_desc(obj: JsonValue) -> str:
    if isinstance(obj, list):
        return f"array[{len(obj)}]"
    if isinstance(obj, dict):
        return f"object, {len(obj)} keys"
    return type(obj).__name__


def build_outline(raw: str, max_chars: int = 4_000) -> str:
    """Return a lightweight structural outline of *raw*."""
    kind, meta = detect_type(raw)
    if kind == "json":
        try:
            obj = cast("JsonValue", json.loads(raw))
            return _outline_json(obj, max_chars)
        except (json.JSONDecodeError, ValueError):
            pass
    lines = raw.splitlines()
    return (
        f"[outline: {kind}, {len(raw):,} chars, {len(lines)} lines]\n"
        f"{meta}\n"
        f"Use mode='range' or mode='grep' to navigate."
    )[:max_chars]


def _outline_json(obj: JsonValue, max_chars: int) -> str:
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            sample = obj[0]
            summary = _outline_json_object_sample(sample, len(obj))
            return f"[JSON outline]\narray[{len(obj)}] of objects:\n{summary}"[:max_chars]
        item_type = type(obj[0]).__name__ if obj else "unknown"
        return f"[JSON outline]\narray[{len(obj)}] of {item_type}"[:max_chars]
    if isinstance(obj, dict):
        summary = _outline_json_object_sample(obj, 1)
        return f"[JSON outline]\nobject:\n{summary}"[:max_chars]
    return f"[JSON outline]\nscalar: {type(obj).__name__}"[:max_chars]


def _outline_json_object_sample(obj: dict[str, JsonValue], count: int) -> str:
    lines: list[str] = []
    for key, value in obj.items():
        type_name = type(value).__name__
        if isinstance(value, list):
            type_name = f"list[{len(value)}]"
        elif isinstance(value, dict):
            type_name = f"dict[{len(value)}]"
        lines.append(f"  - {key}: {type_name}")
    if count > 1:
        lines.append(f"  (sampled from {count} objects)")
    return "\n".join(lines)
