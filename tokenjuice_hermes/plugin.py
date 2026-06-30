from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias

from .compaction import transform_tool_result

HookCallback: TypeAlias = Callable[..., str | None]


class HookRegistrar(Protocol):
    def register_hook(
        self,
        name: str,
        callback: HookCallback,
    ) -> None: ...


def register(ctx: HookRegistrar) -> None:
    ctx.register_hook("transform_tool_result", transform_tool_result)
