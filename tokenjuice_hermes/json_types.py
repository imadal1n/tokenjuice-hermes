from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FlatJsonObject: TypeAlias = dict[str, JsonScalar]
TerminalJsonObject: TypeAlias = dict[str, JsonValue]
UNICODE_ESCAPE_DIGITS: Final[int] = 4

WHITESPACE: Final[frozenset[str]] = frozenset({" ", "\n", "\r", "\t"})
ESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


@dataclass(frozen=True, slots=True)
class Parsed:
    value: JsonValue
    index: int


@dataclass(frozen=True, slots=True)
class ParsedString:
    value: str
    index: int


@dataclass(frozen=True, slots=True)
class ParsedMember:
    key: str
    value: JsonValue
    index: int


@dataclass(frozen=True, slots=True)
class JsonParser:
    text: str

    def parse(self) -> JsonValue | None:
        parsed = self._value(self._skip(0))
        if parsed is None or self._skip(parsed.index) != len(self.text):
            return None
        return parsed.value

    def _value(self, index: int) -> Parsed | None:
        parsed: Parsed | None = None
        if index >= len(self.text):
            return None
        char = self.text[index]
        if char == '"':
            string = self._string(index)
            parsed = None if string is None else Parsed(string.value, string.index)
        elif char == "{":
            parsed = self._object(index)
        elif char == "[":
            parsed = self._array(index)
        elif char == "t" and self.text.startswith("true", index):
            parsed = Parsed(value=True, index=index + 4)
        elif char == "f" and self.text.startswith("false", index):
            parsed = Parsed(value=False, index=index + 5)
        elif char == "n" and self.text.startswith("null", index):
            parsed = Parsed(None, index + 4)
        elif char == "-" or char.isdigit():
            parsed = self._number(index)
        return parsed

    def _object(self, index: int) -> Parsed | None:
        result: dict[str, JsonValue] = {}
        index = self._skip(index + 1)
        if self._at(index, "}"):
            return Parsed(result, index + 1)
        while index < len(self.text):
            member = self._member(index)
            if member is None:
                return None
            result[member.key] = member.value
            index = self._skip(member.index)
            if self._at(index, "}"):
                return Parsed(result, index + 1)
            if not self._at(index, ","):
                return None
            index = self._skip(index + 1)
        return None

    def _member(self, index: int) -> ParsedMember | None:
        key = self._string(index)
        if key is None:
            return None
        index = self._skip(key.index)
        if not self._at(index, ":"):
            return None
        value = self._value(self._skip(index + 1))
        if value is None:
            return None
        return ParsedMember(key.value, value.value, value.index)

    def _array(self, index: int) -> Parsed | None:
        result: list[JsonValue] = []
        index = self._skip(index + 1)
        if self._at(index, "]"):
            return Parsed(result, index + 1)
        while index < len(self.text):
            value = self._value(index)
            if value is None:
                return None
            result.append(value.value)
            index = self._skip(value.index)
            if self._at(index, "]"):
                return Parsed(result, index + 1)
            if not self._at(index, ","):
                return None
            index = self._skip(index + 1)
        return None

    def _string(self, index: int) -> ParsedString | None:
        chars: list[str] = []
        index += 1
        while index < len(self.text):
            char = self.text[index]
            if char == '"':
                return ParsedString("".join(chars), index + 1)
            if char == "\\":
                escaped = self._escape(index + 1)
                if escaped is None:
                    return None
                chars.append(escaped[0])
                index = escaped[1]
                continue
            chars.append(char)
            index += 1
        return None

    def _escape(self, index: int) -> tuple[str, int] | None:
        if index >= len(self.text):
            return None
        char = self.text[index]
        if char == "u":
            digits = self.text[index + 1 : index + 5]
            if len(digits) != UNICODE_ESCAPE_DIGITS or not all(
                digit in "0123456789abcdefABCDEF" for digit in digits
            ):
                return None
            return chr(int(digits, 16)), index + 5
        value = ESCAPES.get(char)
        if value is None:
            return None
        return value, index + 1

    def _number(self, index: int) -> Parsed | None:
        start = index
        if self._at(index, "-"):
            index += 1
        index = self._digits(index)
        if index == start or self.text[start:index] == "-":
            return None
        is_float = False
        if self._at(index, "."):
            is_float = True
            index = self._digits(index + 1)
        if index < len(self.text) and self.text[index] in {"e", "E"}:
            is_float = True
            index += 1
            if index < len(self.text) and self.text[index] in {"+", "-"}:
                index += 1
            index = self._digits(index)
        raw = self.text[start:index]
        try:
            return Parsed(float(raw) if is_float else int(raw), index)
        except ValueError:
            return None

    def _digits(self, index: int) -> int:
        while index < len(self.text) and self.text[index].isdigit():
            index += 1
        return index

    def _skip(self, index: int) -> int:
        while index < len(self.text) and self.text[index] in WHITESPACE:
            index += 1
        return index

    def _at(self, index: int, expected: str) -> bool:
        return index < len(self.text) and self.text[index] == expected


def parse_json(text: str) -> JsonValue | None:
    return JsonParser(text).parse()


def parse_flat_json_object(text: str) -> FlatJsonObject | None:
    value = parse_json(text)
    if not isinstance(value, dict):
        return None
    result: FlatJsonObject = {}
    for key, field_value in value.items():
        if field_value is None or isinstance(field_value, bool | int | float | str):
            result[key] = field_value
        else:
            return None
    return result


def is_json_scalar(value: JsonValue) -> bool:
    return value is None or isinstance(value, bool | int | float | str)


def is_error_payload(payload: FlatJsonObject) -> bool:
    exit_code = payload.get("exit")
    status = payload.get("status")
    return _is_nonzero_number(exit_code) or _is_error_status(status)


def _is_nonzero_number(value: JsonScalar) -> bool:
    return isinstance(value, int | float) and value != 0


def _is_error_status(value: JsonScalar) -> bool:
    return isinstance(value, str) and value.lower() in {"error", "errored", "failed", "failure"}
