from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeAlias

from tokenjuice_hermes.json_types import JsonValue

RequestObject: TypeAlias = dict[str, JsonValue]
MiddlewareCallback: TypeAlias = Callable[..., RequestObject | str | None]
ToolCallback: TypeAlias = Callable[..., JsonValue | None]


class HookOnlyHost:
    def __init__(self) -> None:
        self.hooks: list[str] = []
        self.callbacks: dict[str, Callable[..., str | None]] = {}

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        self.callbacks[name] = callback
        self.hooks.append(name)


class MiddlewareHost(HookOnlyHost):
    def __init__(self) -> None:
        super().__init__()
        self.middlewares: dict[str, MiddlewareCallback] = {}

    def register_middleware(
        self,
        name: str,
        callback: MiddlewareCallback,
    ) -> None:
        self.middlewares[name] = callback


class ToolHost(MiddlewareHost):
    def __init__(self) -> None:
        super().__init__()
        self.tools: dict[str, ToolCallback] = {}
        self.config: dict[str, JsonValue] = {}

    def register_tool(self, name: str, callback: ToolCallback) -> None:
        self.tools[name] = callback


class _ToolRegistrationError(RuntimeError):
    """Raised by test hosts that intentionally fail tool registration."""


class _MiddlewareRegistrationError(RuntimeError):
    """Raised by test hosts that intentionally fail middleware registration."""


class FailingToolHost(MiddlewareHost):
    def register_tool(self, _name: str, _callback: ToolCallback) -> None:
        raise _ToolRegistrationError


class FailingMiddlewareHost(HookOnlyHost):
    """Hook-only host whose register_middleware always raises.

    Inherits from HookOnlyHost rather than MiddlewareHost so the test fixture
    does not need to satisfy method-override checks for register_middleware.
    """

    def __init__(self) -> None:
        super().__init__()
        self.middlewares: dict[str, MiddlewareCallback] = {}

    def register_middleware(
        self,
        _name: str,
        _callback: MiddlewareCallback,
    ) -> None:
        raise _MiddlewareRegistrationError


def big_web_content(*, lines: int = 1200) -> str:
    return "\n".join(f"web result line {number:04d}" for number in range(1, lines + 1))


def web_result(content: str) -> str:
    return json.dumps({"content": content})


def extract_hex_handle(text: str) -> str | None:
    match = re.search(r"\b[0-9a-f]{12}\b", text)
    return match.group(0) if match else None
