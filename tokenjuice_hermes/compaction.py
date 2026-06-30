from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
)

JsonScalar: TypeAlias = None | bool | int | float | str
FlatJsonObject: TypeAlias = dict[str, JsonScalar]
TerminalJsonObject: TypeAlias = dict[str, JsonValue]


FLAT_JSON_ADAPTER: Final[TypeAdapter[FlatJsonObject]] = TypeAdapter(FlatJsonObject)
TERMINAL_JSON_ADAPTER: Final[TypeAdapter[TerminalJsonObject]] = TypeAdapter(TerminalJsonObject)

TERMINAL_TOOL_NAMES: Final[frozenset[str]] = frozenset({"terminal", "execute_code"})
PROTECTED_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read_file"})
TEXT_FIELDS: Final[tuple[str, ...]] = ("stdout", "stderr", "output")
MIN_TEXT_CHARS: Final[int] = 240
HEAD_LINES: Final[int] = 3
TAIL_LINES: Final[int] = 2
PREVIEW_CHARS: Final[int] = 72
CONFIG_PREFIX: Final[str] = "tokenjuice_"


class CompactionMode(StrEnum):
    HEAD_TAIL = "head_tail"
    METADATA = "metadata"
    OFF = "off"


class TokenjuiceOptions(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    mode: CompactionMode = Field(default=CompactionMode.HEAD_TAIL, alias="tokenjuice_mode")
    min_text_chars: int = Field(default=MIN_TEXT_CHARS, ge=0, alias="tokenjuice_min_text_chars")
    head_lines: int = Field(default=HEAD_LINES, ge=0, alias="tokenjuice_head_lines")
    tail_lines: int = Field(default=TAIL_LINES, ge=0, alias="tokenjuice_tail_lines")
    preview_chars: int = Field(default=PREVIEW_CHARS, ge=0, alias="tokenjuice_preview_chars")
    text_fields: tuple[str, ...] = Field(default=TEXT_FIELDS, alias="tokenjuice_text_fields")
    tool_aliases: frozenset[str] = Field(
        default_factory=frozenset,
        alias="tokenjuice_tool_aliases",
    )

    @field_validator("text_fields", mode="before")
    @classmethod
    def parse_text_fields(cls, value: JsonValue) -> JsonValue | tuple[str, ...]:
        if isinstance(value, str):
            return _split_csv(value)
        return value

    @field_validator("tool_aliases", mode="before")
    @classmethod
    def parse_tool_aliases(cls, value: JsonValue) -> JsonValue | tuple[str, ...]:
        if isinstance(value, str):
            return _split_csv(value)
        return value


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

    parsed = _parse_json_object(result)
    if parsed is None:
        return None

    compacted = _transform_terminal_result(parsed, options)
    if compacted is None:
        return None
    return TERMINAL_JSON_ADAPTER.dump_json(compacted).decode()


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_options(kwargs: dict[str, JsonScalar]) -> TokenjuiceOptions | None:
    tokenjuice_kwargs = {
        key: value for key, value in kwargs.items() if key.startswith(CONFIG_PREFIX)
    }
    try:
        return TokenjuiceOptions.model_validate(tokenjuice_kwargs)
    except ValidationError:
        return None


def _supported_tool_names(options: TokenjuiceOptions) -> frozenset[str]:
    return TERMINAL_TOOL_NAMES | (options.tool_aliases - PROTECTED_TOOL_NAMES)


def _parse_json_object(text: str) -> FlatJsonObject | None:
    try:
        return FLAT_JSON_ADAPTER.validate_json(text)
    except ValidationError:
        return None


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

    for field in options.text_fields:
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
