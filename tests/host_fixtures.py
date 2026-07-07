from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeAlias

from tokenjuice_hermes.compaction_options import OPTION_KEYS
from tokenjuice_hermes.json_types import JsonScalar, JsonValue

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


class _HermesHostBase:
    """Hermes-like test host state and callback invocation helpers.

    This base models the parts of a real Hermes plugin host that every variant
    shares: a ``config`` mapping, a default ``session_id``, recorded callbacks,
    and helper methods to invoke those callbacks with the session/config kwargs
    that a real host would pass through.
    """

    session_id: str
    _tool_registration_error: bool
    _middleware_registration_error: bool

    def __init__(
        self,
        config: dict[str, JsonValue] | None = None,
        *,
        session_id: str = "",
        tool_registration_error: bool = False,
        middleware_registration_error: bool = False,
    ) -> None:
        self.config: dict[str, JsonValue] = dict(config) if config else {}
        self.session_id = session_id
        self.hooks: list[str] = []
        self.callbacks: dict[str, Callable[..., str | None]] = {}
        self.tools: dict[str, ToolCallback] = {}
        self.middlewares: dict[str, MiddlewareCallback] = {}
        self._tool_registration_error = tool_registration_error
        self._middleware_registration_error = middleware_registration_error

    def _effective_session_id(self, session_id: str | None) -> str:
        return session_id if session_id is not None else self.session_id

    def _hook_kwargs(
        self,
        session_id: str | None,
        extra: dict[str, JsonScalar],
    ) -> dict[str, JsonScalar]:
        kwargs: dict[str, JsonScalar] = {
            key: value
            for key, value in self.config.items()
            if isinstance(value, JsonScalar) and key in OPTION_KEYS
        }
        kwargs.update(extra)
        kwargs["session_id"] = self._effective_session_id(session_id)
        return kwargs

    def invoke_hook(
        self,
        name: str,
        result: str,
        *,
        tool_name: str = "",
        session_id: str | None = None,
        **extra: JsonValue,
    ) -> str | None:
        """Invoke a registered hook callback as a Hermes host would."""
        callback = self.callbacks[name]
        scalar_extra = {key: value for key, value in extra.items() if isinstance(value, JsonScalar)}
        return callback(
            result,
            tool_name=tool_name,
            **self._hook_kwargs(session_id, scalar_extra),
        )

    def invoke_tool(
        self,
        name: str,
        args: dict[str, JsonValue],
        *,
        session_id: str | None = None,
    ) -> JsonValue | None:
        """Invoke a registered tool callback as a Hermes host would."""
        callback = self.tools[name]
        return callback(args, self._effective_session_id(session_id))

    def invoke_middleware(
        self,
        name: str,
        request: RequestObject | None = None,
        *,
        session_id: str | None = None,
        **extra: JsonValue,
    ) -> RequestObject | str | None:
        """Invoke a registered middleware callback as a Hermes host would."""
        callback = self.middlewares[name]
        kwargs: dict[str, JsonValue] = {
            "session_id": self._effective_session_id(session_id),
        }
        kwargs.update(extra)
        if request is None:
            return callback(**kwargs)
        return callback(request, **kwargs)


class HermesHost(_HermesHostBase):
    """Realistic Hermes-like host with hook, tool, and middleware surfaces."""

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        self.callbacks[name] = callback
        self.hooks.append(name)

    def register_tool(self, name: str, callback: ToolCallback) -> None:
        if self._tool_registration_error:
            raise _ToolRegistrationError
        self.tools[name] = callback

    def register_middleware(self, name: str, callback: MiddlewareCallback) -> None:
        if self._middleware_registration_error:
            raise _MiddlewareRegistrationError
        self.middlewares[name] = callback


class HermesToollessHost(_HermesHostBase):
    """Hermes-like host with hook and middleware surfaces but no tool surface."""

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        self.callbacks[name] = callback
        self.hooks.append(name)

    def register_middleware(self, name: str, callback: MiddlewareCallback) -> None:
        if self._middleware_registration_error:
            raise _MiddlewareRegistrationError
        self.middlewares[name] = callback


class HermesMiddlewarelessHost(_HermesHostBase):
    """Hermes-like host with hook and tool surfaces but no middleware surface."""

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        self.callbacks[name] = callback
        self.hooks.append(name)

    def register_tool(self, name: str, callback: ToolCallback) -> None:
        if self._tool_registration_error:
            raise _ToolRegistrationError
        self.tools[name] = callback
