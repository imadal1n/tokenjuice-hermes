from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias, TypeGuard, cast

from .compaction import transform_tool_result
from .hermes_config import load_hermes_plugin_config
from .json_types import JsonScalar, JsonValue
from .observability import tokenjuice_status
from .passref import make_passref_middleware
from .request_pruning import prune_llm_request
from .rescue_fetch import rescuer_fetch
from .rescue_store import BlobStore
from .rescue_types import RESCUE_STORE_PATH_DEFAULT

HookCallback: TypeAlias = Callable[..., str | None]
MiddlewareCallback: TypeAlias = Callable[..., dict[str, JsonValue] | str | None]


class HookRegistrar(Protocol):
    def register_hook(
        self,
        name: str,
        callback: HookCallback,
    ) -> None: ...


def register(ctx: HookRegistrar) -> None:
    raw_config: object = getattr(ctx, "config", None)
    if raw_config is None:
        raw_config = load_hermes_plugin_config()
    raw_config = raw_config or {}
    config: dict[str, object] = cast("dict[str, object]", raw_config)
    hook_config = _flat_json_config(config)

    _ = _try_register_named_middleware(ctx, "llm_request", prune_llm_request)

    fetch_available = _try_register_fetch_tool(ctx, config)
    _ = _try_register_passref_middleware(ctx, config)
    _ = _try_register_status_tool(ctx, config)

    if fetch_available:

        def _transform(
            result: str = "",
            *,
            tool_name: str = "",
            **kwargs: JsonScalar,
        ) -> str | None:
            merged_kwargs = {**hook_config, **kwargs}
            _ = merged_kwargs.setdefault("tokenjuice_rescue_fetch_available", True)
            return transform_tool_result(result, tool_name=tool_name, **merged_kwargs)

        ctx.register_hook("transform_tool_result", _transform)
    else:
        ctx.register_hook("transform_tool_result", transform_tool_result)


def _try_register_named_middleware(
    ctx: HookRegistrar,
    name: str,
    callback: MiddlewareCallback,
) -> bool:
    register_middleware = getattr(ctx, "register_middleware", None)
    if not callable(register_middleware):
        return False
    try:
        _ = register_middleware(name, callback)
    except Exception:  # noqa: BLE001 - middleware registration is optional; failures degrade gracefully
        return False
    return True


def _flat_json_config(config: dict[str, object]) -> dict[str, JsonScalar]:
    return {key: value for key, value in config.items() if _is_json_scalar(value)}


def _is_json_scalar(value: object) -> TypeGuard[JsonScalar]:
    return value is None or isinstance(value, str | int | float | bool)


def _try_register_passref_middleware(
    ctx: HookRegistrar,
    config: dict[str, object],
) -> bool:
    register_middleware = getattr(ctx, "register_middleware", None)
    if not callable(register_middleware):
        return False

    store_path = _rescue_store_path(config)

    def _get_store() -> BlobStore | None:
        try:
            return BlobStore({"store_path": store_path})
        except (OSError, ValueError):
            return None

    try:
        middleware = make_passref_middleware(_get_store, config)
        _ = register_middleware("tool_request", middleware)
    except Exception:  # noqa: BLE001 - passref registration is optional; failures degrade gracefully
        return False
    return True


def _rescue_store_path(config: dict[str, object]) -> str:
    path = config.get("tokenjuice_rescue_store_path", RESCUE_STORE_PATH_DEFAULT)
    if isinstance(path, str) and path:
        return path
    return RESCUE_STORE_PATH_DEFAULT


def _try_register_fetch_tool(
    ctx: HookRegistrar,
    config: dict[str, object],
) -> bool:
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        return False

    def _fetch_tool(args: dict[str, object], session_id: str = "") -> str:
        return rescuer_fetch(args, session_id=session_id, config=config)

    try:
        _ = register_tool("rescuer_fetch", _fetch_tool)
    except Exception:  # noqa: BLE001 - tool registration is optional; failures degrade gracefully
        return False
    return True


def _try_register_status_tool(
    ctx: HookRegistrar,
    config: dict[str, object],
) -> bool:
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        return False

    store_path = _rescue_store_path(config)

    def _status_tool(args: dict[str, object], session_id: str = "") -> str:
        _ = session_id
        return tokenjuice_status(args, store_path=store_path)

    try:
        _ = register_tool("tokenjuice_status", _status_tool)
    except Exception:  # noqa: BLE001 - status tool registration is optional; failures degrade gracefully
        return False
    return True
