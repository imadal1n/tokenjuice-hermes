from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .json_types import JsonValue

_HERMES_CONFIG_PATH: str = "/opt/data/config.yaml"
_HERMES_CONFIG_ENV: str = "HERMES_CONFIG_PATH"
_HERMES_PLUGIN_NAME: str = "tokenjuice-hermes"
_QUOTE_CHARS: frozenset[str] = frozenset({'"', "'"})
_MIN_QUOTED_LENGTH: int = 2


@dataclass(frozen=True, slots=True)
class _YamlEntry:
    indent: int
    text: str


@dataclass(frozen=True, slots=True)
class _ParsedMapping:
    value: dict[str, JsonValue]
    index: int


def load_hermes_plugin_config() -> dict[str, JsonValue]:
    path = os.environ.get(_HERMES_CONFIG_ENV, _HERMES_CONFIG_PATH)
    return load_hermes_plugin_config_from(path, _HERMES_PLUGIN_NAME)


def load_hermes_plugin_config_from(path: str, plugin_name: str) -> dict[str, JsonValue]:
    try:
        document = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    parsed = _load_yaml(document)
    plugins = _as_json_object(parsed.get("plugins"))
    if plugins is None:
        return {}
    entries = _as_json_object(plugins.get("entries"))
    if entries is None:
        return {}
    plugin_entry = _as_json_object(entries.get(plugin_name))
    if plugin_entry is None:
        return {}
    config = _as_json_object(plugin_entry.get("config"))
    return {} if config is None else config


def _as_json_object(value: JsonValue | None) -> dict[str, JsonValue] | None:
    if isinstance(value, dict):
        return value
    return None


def _load_yaml(text: str) -> dict[str, JsonValue]:
    tokens: list[_YamlEntry] = []
    for line in text.splitlines():
        stripped = _strip_yaml_comment(line.rstrip("\n"))
        content = stripped.strip()
        if not content:
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        tokens.append(_YamlEntry(indent=indent, text=content))
    if not tokens:
        return {}
    return _parse_yaml_mapping(tokens, 0, -1).value


def _parse_yaml_mapping(
    tokens: list[_YamlEntry], index: int, base_indent: int
) -> _ParsedMapping:
    result: dict[str, JsonValue] = {}
    while index < len(tokens):
        entry = tokens[index]
        if entry.indent <= base_indent:
            return _ParsedMapping(value=result, index=index)
        key, sep, value = entry.text.partition(":")
        key = key.strip()
        if not sep or not key:
            index += 1
            continue
        value = value.strip()
        if value:
            result[key] = _parse_yaml_scalar(value)
            index += 1
            continue
        index += 1
        if index >= len(tokens) or tokens[index].indent <= entry.indent:
            result[key] = {}
            continue
        child = _parse_yaml_mapping(tokens, index, tokens[index].indent - 1)
        result[key] = child.value
        index = child.index
    return _ParsedMapping(value=result, index=index)


def _strip_yaml_comment(line: str) -> str:
    in_quote: str | None = None
    for index, char in enumerate(line):
        if char in _QUOTE_CHARS:
            if in_quote is None:
                in_quote = char
            elif in_quote == char:
                in_quote = None
        elif char == "#" and in_quote is None:
            return line[:index]
    return line


def _parse_yaml_scalar(value: str) -> JsonValue:
    stripped = value.strip()
    if _is_quoted(stripped):
        return stripped[1:-1]
    return _yaml_scalar_value(stripped)


def _is_quoted(value: str) -> bool:
    return (
        len(value) >= _MIN_QUOTED_LENGTH
        and value[0] == value[-1]
        and value[0] in _QUOTE_CHARS
    )


def _yaml_scalar_value(value: str) -> JsonValue:
    lower = value.lower()
    if lower in ("null", "~"):
        return None
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
