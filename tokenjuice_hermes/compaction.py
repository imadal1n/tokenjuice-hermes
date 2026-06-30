from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypeAlias, TypedDict

from pydantic import TypeAdapter, ValidationError

JsonScalar: TypeAlias = None | bool | int | float | str
FlatJsonObject: TypeAlias = dict[str, JsonScalar]


class TokenjuiceMeta(TypedDict):
    compacted: bool
    original_chars: int


TerminalJsonObject: TypeAlias = dict[str, JsonScalar | TokenjuiceMeta]
FLAT_JSON_ADAPTER: Final[TypeAdapter[FlatJsonObject]] = TypeAdapter(FlatJsonObject)
TERMINAL_JSON_ADAPTER: Final[TypeAdapter[TerminalJsonObject]] = TypeAdapter(TerminalJsonObject)

TERMINAL_TOOL_NAMES: Final[frozenset[str]] = frozenset({"terminal", "execute_code"})
PROTECTED_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read_file"})
TEXT_FIELDS: Final[tuple[str, ...]] = ("stdout", "stderr", "output")
MIN_TEXT_CHARS: Final[int] = 240
HEAD_LINES: Final[int] = 3
TAIL_LINES: Final[int] = 2
MIN_TEXT_LINES: Final[int] = HEAD_LINES + TAIL_LINES + 1


@dataclass(frozen=True, slots=True)
class CompactText:
    text: str
    original_chars: int
    omitted_lines: int


def transform_tool_result(
    result: str = "",
    *,
    tool_name: str = "",
    **_kwargs: JsonScalar,
) -> str | None:
    if tool_name in PROTECTED_TOOL_NAMES:
        return None
    if tool_name not in TERMINAL_TOOL_NAMES:
        return None

    parsed = _parse_json_object(result)
    if parsed is None:
        return None

    compacted = _compact_terminal_result(parsed)
    if compacted is None:
        return None
    return TERMINAL_JSON_ADAPTER.dump_json(compacted).decode()


def _parse_json_object(text: str) -> FlatJsonObject | None:
    try:
        return FLAT_JSON_ADAPTER.validate_json(text)
    except ValidationError:
        return None


def _compact_terminal_result(payload: FlatJsonObject) -> TerminalJsonObject | None:
    next_payload: TerminalJsonObject = dict(payload)
    changed = False

    for field in TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            compacted = _compact_text(value)
            if compacted is not None:
                next_payload[field] = compacted.text
                changed = True

    if not changed:
        return None

    next_payload["tokenjuice"] = {
        "compacted": True,
        "original_chars": _sum_text_chars(payload),
    }
    return next_payload


def _compact_text(text: str) -> CompactText | None:
    lines = text.splitlines()
    if len(text) < MIN_TEXT_CHARS and len(lines) < MIN_TEXT_LINES:
        return None
    if len(lines) < MIN_TEXT_LINES:
        return None

    head = lines[:HEAD_LINES]
    tail = lines[-TAIL_LINES:]
    omitted = len(lines) - len(head) - len(tail)
    compacted = "\n".join(
        [
            *head,
            f"[tokenjuice-hermes: omitted {omitted} middle lines]",
            *tail,
        ]
    )
    return CompactText(text=compacted, original_chars=len(text), omitted_lines=omitted)


def _sum_text_chars(payload: FlatJsonObject) -> int:
    total = 0
    for field in TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            total += len(value)
    return total
