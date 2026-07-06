from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias

from .compaction import transform_tool_result
from .request_pruning import RequestObject, prune_llm_request

HookCallback: TypeAlias = Callable[..., str | None]
MiddlewareCallback: TypeAlias = Callable[..., RequestObject | None]


class HookRegistrar(Protocol):
    def register_hook(
        self,
        name: str,
        callback: HookCallback,
    ) -> None: ...


def register(ctx: HookRegistrar) -> None:
    ctx.register_hook("transform_tool_result", transform_tool_result)
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        _ = register_middleware("llm_request", prune_llm_request)
