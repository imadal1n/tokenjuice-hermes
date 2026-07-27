from __future__ import annotations

from typing import Final, TypeAlias

from .json_types import JsonValue, is_error_payload, parse_flat_json_object
from .structured_pruning import STRUCTURED_PRUNING_MARKER

RequestObject: TypeAlias = dict[str, JsonValue]
MessageObject: TypeAlias = dict[str, JsonValue]

PROTECTED_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read_file"})
PRUNABLE_TOOL_NAMES: Final[frozenset[str]] = frozenset({"terminal", "execute_code"})
PRESSURE_RATIO: Final[float] = 0.80
FALLBACK_MIN_REQUEST_CHARS: Final[int] = 4_000
PROTECTED_TAIL_TOOL_RESULTS: Final[int] = 2
HEAD_CHARS: Final[int] = 1_500
TAIL_CHARS: Final[int] = 1_500
DIAGNOSTIC_MARKERS: Final[tuple[str, ...]] = (
    "--- stderr ---",
    "Traceback (most recent call last):",
)


def prune_llm_request(
    request: RequestObject | None = None,
    **kwargs: JsonValue,
) -> RequestObject | None:
    if request is None or not _should_prune_request(request, kwargs):
        return None

    messages_key = _request_messages_key(request)
    if messages_key is None:
        return None

    messages = request.get(messages_key)
    if not isinstance(messages, list):
        return None

    next_messages = _prune_messages(messages)
    if next_messages is None:
        return None

    return {
        "request": {**request, messages_key: next_messages},
        "source": "tokenjuice-hermes",
        "reason": "context-pressure-tool-pruning",
    }


def _request_messages_key(request: RequestObject) -> str | None:
    if isinstance(request.get("messages"), list):
        return "messages"
    if isinstance(request.get("input"), list):
        return "input"
    return None


def _should_prune_request(request: RequestObject, kwargs: dict[str, JsonValue]) -> bool:
    compression_enabled = kwargs.get("compression_enabled")
    if compression_enabled is False:
        return False

    pressure_tokens = kwargs.get("request_pressure_tokens")
    threshold_tokens = kwargs.get("threshold_tokens")
    if (
        isinstance(pressure_tokens, int)
        and isinstance(threshold_tokens, int)
        and threshold_tokens > 0
    ):
        return pressure_tokens >= int(threshold_tokens * PRESSURE_RATIO)

    return len(str(request)) >= FALLBACK_MIN_REQUEST_CHARS


def _prune_messages(messages: list[JsonValue]) -> list[JsonValue] | None:
    protected_tool_indexes = _protected_tail_tool_indexes(messages)
    next_messages: list[JsonValue] = []
    changed = False

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return None
        next_message, pruned = _prune_message(dict(message), protected_tool_indexes, index)
        next_messages.append(next_message)
        changed = changed or pruned

    return next_messages if changed else None


def _protected_tail_tool_indexes(messages: list[JsonValue]) -> frozenset[int]:
    protected: list[int] = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        protected.append(index)
        if len(protected) >= PROTECTED_TAIL_TOOL_RESULTS:
            break
    return frozenset(protected)


def _prune_message(
    message: MessageObject,
    protected_tool_indexes: frozenset[int],
    index: int,
) -> tuple[MessageObject, bool]:
    if not _is_eligible_tool_message(message, protected_tool_indexes, index):
        return message, False

    name = message.get("name")
    content = message.get("content")
    if not isinstance(name, str) or not isinstance(content, str):
        return message, False
    compacted = _compact_text(content)
    if compacted == content:
        return message, False
    return {**message, "content": compacted}, True


def _is_eligible_tool_message(
    message: MessageObject,
    protected_tool_indexes: frozenset[int],
    index: int,
) -> bool:
    if index in protected_tool_indexes or message.get("role") != "tool":
        return False

    name = message.get("name")
    content = message.get("content")
    if not isinstance(name, str) or not isinstance(content, str):
        return False
    if name in PROTECTED_TOOL_NAMES or name not in PRUNABLE_TOOL_NAMES:
        return False
    return STRUCTURED_PRUNING_MARKER not in content and not _is_diagnostic_payload(content)


def _is_diagnostic_payload(text: str) -> bool:
    parsed = parse_flat_json_object(text)
    if parsed is not None and is_error_payload(parsed):
        return True
    return any(marker in text for marker in DIAGNOSTIC_MARKERS)


def _compact_text(text: str) -> str:
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text

    head = _cut_head(text, HEAD_CHARS)
    tail = _cut_tail(text, TAIL_CHARS)
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n[tokenjuice-hermes: omitted {omitted} chars from old tool result]\n{tail}"


def _cut_head(text: str, max_chars: int) -> str:
    head = text[:max_chars]
    newline = head.rfind("\n")
    if newline > max_chars // 2:
        return head[:newline]
    return head


def _cut_tail(text: str, max_chars: int) -> str:
    tail = text[-max_chars:]
    newline = tail.find("\n")
    if 0 <= newline < max_chars // 2:
        return tail[newline + 1 :]
    return tail
