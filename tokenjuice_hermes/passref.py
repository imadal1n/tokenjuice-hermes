from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import ClassVar, Final, Protocol, TypeAlias, cast

from .json_types import JsonValue
from .observability import (
    record_passref_budget_exceeded,
    record_passref_denial,
    record_passref_enabled,
    record_passref_expansion,
    record_passref_truncation,
)


class RescueStore(Protocol):
    def blob_text(self, handle: str) -> str | None: ...

    def has_blob(self, handle: str) -> bool: ...

    def session_references(self, handle: str, session_id: str) -> bool: ...


ToolRequestCallback: TypeAlias = Callable[..., dict[str, JsonValue] | None]

_HANDLE_RE: Final[re.Pattern[str]] = re.compile(r"tla:([0-9a-f]{12})")

_DEFAULT_MAX_CHARS: Final[int] = 500_000
_DEFAULT_TOTAL_MAX_CHARS: Final[int] = 2_000_000

_HARD_EXEMPT_TOOLS: Final[frozenset[str]] = frozenset({"read_file", "diagnostics"})
_SINK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "bash",
        "curl",
        "edit_file",
        "exec",
        "execute_code",
        "file_write",
        "fs_write",
        "http_post",
        "http_request",
        "run_command",
        "run_shell",
        "shell",
        "subprocess",
        "terminal",
        "upload",
        "write_file",
    }
)


class _Budget:
    __slots__: ClassVar[tuple[str, ...]] = (
        "budget_exceeded",
        "changed",
        "total",
        "truncated",
    )

    def __init__(self) -> None:
        self.total: int = 0
        self.changed: bool = False
        self.truncated: bool = False
        self.budget_exceeded: bool = False


class _PassrefConfig:
    __slots__: ClassVar[tuple[str, ...]] = (
        "allowed_tools",
        "enabled",
        "max_chars",
        "total_max_chars",
    )

    allowed_tools: frozenset[str]
    enabled: bool
    max_chars: int
    total_max_chars: int

    def __init__(
        self,
        *,
        allowed_tools: frozenset[str],
        enabled: bool,
        max_chars: int,
        total_max_chars: int,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.enabled = enabled
        self.max_chars = max_chars
        self.total_max_chars = total_max_chars

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, object]) -> _PassrefConfig:
        raw_enabled = cfg.get("tokenjuice_passref_enabled", False)
        enabled = isinstance(raw_enabled, bool) and raw_enabled

        raw_allowed = cfg.get("tokenjuice_passref_allowed_tools", [])
        allowed_tools = _parse_allowed_tools(raw_allowed)

        max_chars = _parse_positive_int(
            cfg.get("tokenjuice_passref_max_chars", _DEFAULT_MAX_CHARS),
            _DEFAULT_MAX_CHARS,
        )
        total_max_chars = _parse_positive_int(
            cfg.get("tokenjuice_passref_total_max_chars", _DEFAULT_TOTAL_MAX_CHARS),
            _DEFAULT_TOTAL_MAX_CHARS,
        )

        return cls(
            allowed_tools=allowed_tools,
            enabled=enabled,
            max_chars=max_chars,
            total_max_chars=total_max_chars,
        )


def _parse_allowed_tools(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, list | tuple):
        items = cast("list[object] | tuple[object, ...]", raw)
        return frozenset(item for item in items if isinstance(item, str))
    return frozenset()


def _parse_positive_int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return max(0, raw)


def _is_hard_exempt(tool_name: str) -> bool:
    return tool_name.lower() in _HARD_EXEMPT_TOOLS


def _is_sink(tool_name: str) -> bool:
    low = tool_name.lower()
    return any(sink in low for sink in _SINK_TOOLS)


def _tool_allowed(tool_name: str, cfg: _PassrefConfig) -> bool:
    if _is_sink(tool_name):
        return False
    if cfg.allowed_tools:
        return tool_name in cfg.allowed_tools
    return False


def _has_handle(value: JsonValue) -> bool:
    if isinstance(value, str):
        return "tla:" in value and _HANDLE_RE.search(value) is not None
    if isinstance(value, list):
        return any(_has_handle(item) for item in value)
    if isinstance(value, dict):
        return any(_has_handle(item) for item in value.values())
    return False


def _replace_handles(value: JsonValue, replacement: str) -> JsonValue:
    if isinstance(value, str):
        return _HANDLE_RE.sub(lambda _m: replacement, value)
    if isinstance(value, list):
        return [_replace_handles(item, replacement) for item in value]
    if isinstance(value, dict):
        return {key: _replace_handles(item, replacement) for key, item in value.items()}
    return value


def _tool_denial_marker(tool_name: str) -> str:
    return f"[tokenjuice-hermes: tool '{tool_name}' is not eligible for passref expansion]"


def _session_required_marker() -> str:
    return "[tokenjuice-hermes: passref requires a session ID]"


def _maybe_deny_args(
    args: dict[str, JsonValue],
    marker: str,
) -> dict[str, JsonValue] | None:
    if not _has_handle(args):
        return None
    return {"args": _replace_handles(args, marker)}


def make_passref_middleware(
    get_store: Callable[[], RescueStore | None],
    config: Mapping[str, object],
) -> ToolRequestCallback:
    cfg = _PassrefConfig.from_mapping(config)
    record_passref_enabled(enabled=cfg.enabled)

    def _tool_request(
        *,
        tool_name: object = "",
        args: JsonValue = None,
        session_id: object = "",
        **_kwargs: object,
    ) -> dict[str, JsonValue] | None:
        return _tool_request_impl(get_store, cfg, tool_name, args, session_id)

    return _tool_request


def _tool_request_impl(
    get_store: Callable[[], RescueStore | None],
    cfg: _PassrefConfig,
    tool_name: object,
    args: JsonValue,
    session_id: object,
) -> dict[str, JsonValue] | None:
    if not cfg.enabled:
        return None
    if not isinstance(tool_name, str) or not isinstance(args, dict):
        return None
    if _is_hard_exempt(tool_name):
        return None
    if not _tool_allowed(tool_name, cfg):
        record_passref_denial()
        return _maybe_deny_args(args, _tool_denial_marker(tool_name))
    if not isinstance(session_id, str) or not session_id:
        record_passref_denial()
        return _maybe_deny_args(args, _session_required_marker())
    return _expand_with_store(get_store, cfg, args, session_id)


def _expand_with_store(
    get_store: Callable[[], RescueStore | None],
    cfg: _PassrefConfig,
    args: dict[str, JsonValue],
    session_id: str,
) -> dict[str, JsonValue] | None:
    store = get_store()
    if store is None:
        return None
    budget = _Budget()
    expanded = _expand_value(args, store, cfg, budget, session_id)
    if not budget.changed:
        return None
    record_passref_expansion(budget.total)
    if budget.truncated:
        record_passref_truncation()
    if budget.budget_exceeded:
        record_passref_budget_exceeded()
    return {"args": expanded}


def _expand_value(
    value: JsonValue,
    store: RescueStore,
    cfg: _PassrefConfig,
    budget: _Budget,
    session_id: str,
) -> JsonValue:
    if isinstance(value, str):
        return _expand_string(value, store, cfg, budget, session_id)
    if isinstance(value, list):
        return [_expand_value(item, store, cfg, budget, session_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand_value(item, store, cfg, budget, session_id) for key, item in value.items()
        }
    return value


def _expand_string(
    text: str,
    store: RescueStore,
    cfg: _PassrefConfig,
    budget: _Budget,
    session_id: str,
) -> str:
    if "tla:" not in text or _HANDLE_RE.search(text) is None:
        return text
    budget.changed = True

    def _replace(match: re.Match[str]) -> str:
        blob_id = match.group(1)
        if budget.total >= cfg.total_max_chars:
            budget.budget_exceeded = True
            return _budget_marker(cfg.total_max_chars)
        if not store.session_references(blob_id, session_id):
            if not store.has_blob(blob_id):
                return _missing_marker(blob_id)
            return _session_marker(blob_id)
        content = store.blob_text(blob_id)
        if content is None:
            return _missing_marker(blob_id)
        remaining = cfg.total_max_chars - budget.total
        cap = min(cfg.max_chars, remaining)
        if len(content) > cap:
            content = content[:cap] + _truncation_marker(len(content), cap)
            budget.truncated = True
        budget.total += len(content)
        return content

    return _HANDLE_RE.sub(_replace, text)


def _missing_marker(blob_id: str) -> str:
    return f"[tokenjuice-hermes: blob {blob_id} unavailable; re-run the source tool]"


def _session_marker(blob_id: str) -> str:
    return f"[tokenjuice-hermes: blob {blob_id} not available in this session]"


def _budget_marker(total_max_chars: int) -> str:
    return f"[tokenjuice-hermes: total expansion budget {total_max_chars:,} chars exceeded]"


def _truncation_marker(original: int, cap: int) -> str:
    return (
        f"\n[tokenjuice-hermes: truncated, blob is {original:,} chars > passref_max_chars {cap:,}]"
    )
