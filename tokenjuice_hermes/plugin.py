from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeGuard, cast

from .compaction import transform_tool_result
from .hermes_config import load_hermes_plugin_config
from .json_types import JsonScalar, JsonValue
from .observability import tokenjuice_status
from .passref import make_passref_middleware
from .request_pruning import prune_llm_request
from .rescue_fetch import rescuer_fetch
from .rescue_store import BlobStore
from .rescue_types import RESCUE_STORE_PATH_DEFAULT
from .structured_pruning import prune_structured_context

if TYPE_CHECKING:
    from .structured_pruning_types import Contribution

HookCallback: TypeAlias = Callable[..., JsonValue | None]
MiddlewareCallback: TypeAlias = Callable[..., dict[str, JsonValue] | str | None]
ToolHandler: TypeAlias = Callable[..., JsonValue | None]

_TOOLSET: str = "tokenjuice-hermes"
_FETCH_TOOL_SCHEMA: dict[str, object] = {
    "description": "Fetch exact content rescued by tokenjuice-hermes.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "mode": {"type": "string", "enum": ["stat", "range", "grep", "full"]},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
            "pattern": {"type": "string"},
        },
        "required": ["id", "mode"],
    },
}
_STATUS_TOOL_SCHEMA: dict[str, object] = {
    "description": "Return aggregate tokenjuice-hermes rescue status.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


class HookRegistrar(Protocol):
    def register_hook(
        self,
        name: str,
        callback: HookCallback,
    ) -> None: ...


def register(ctx: HookRegistrar) -> None:
    raw_config: object = getattr(ctx, "config", None)
    raw_config = raw_config or load_hermes_plugin_config()
    config: dict[str, object] = cast("dict[str, object]", raw_config)
    if not _has_tokenjuice_config(config):
        config = cast("dict[str, object]", load_hermes_plugin_config())
    hook_config = _flat_json_config(config)

    _ = _try_register_named_middleware(ctx, "llm_request", prune_llm_request)
    _ = _try_register_structured_pruning_hook(ctx, config)

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


def _try_register_structured_pruning_hook(
    ctx: HookRegistrar,
    config: dict[str, object],
) -> bool:
    if not _is_structured_pruning_enabled(config):
        return False
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return False

    def _structured_context_prune(
        contributions: Sequence[Contribution],
        *,
        current_pressure_tokens: int | None = None,
        threshold_tokens: int | None = None,
        **kwargs: JsonValue,
    ) -> dict[str, JsonValue] | None:
        merged_config = {**config, **kwargs}
        flat_config = _flat_json_config(merged_config)
        result = prune_structured_context(
            contributions,
            current_pressure_tokens,
            threshold_tokens,
            **flat_config,
        )
        if result is None:
            return None
        return cast("dict[str, JsonValue]", cast("object", result))

    try:
        _ = register_hook("structured_context_prune", _structured_context_prune)
    except Exception:  # noqa: BLE001 - hook registration is optional; failures degrade gracefully
        return False
    return True


def _is_structured_pruning_enabled(config: dict[str, object]) -> bool:
    value = config.get("tokenjuice_prompt_pruning_enabled")
    if value is True:
        return True
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "on", "1"}
    return False


def _flat_json_config(config: dict[str, object]) -> dict[str, JsonScalar]:
    return {key: value for key, value in config.items() if _is_json_scalar(value)}


def _has_tokenjuice_config(config: dict[str, object]) -> bool:
    return any(key.startswith("tokenjuice_") for key in config)


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

    def _fetch_tool(
        args: dict[str, object],
        session_id: str = "",
        **_kwargs: JsonValue,
    ) -> str:
        return rescuer_fetch(args, session_id=session_id, config=config)

    return _try_register_tool(
        ctx,
        name="rescuer_fetch",
        schema=_FETCH_TOOL_SCHEMA,
        handler=_fetch_tool,
        description="Fetch exact content rescued by tokenjuice-hermes.",
    )


def _try_register_tool(
    ctx: HookRegistrar,
    *,
    name: str,
    schema: dict[str, object],
    handler: ToolHandler,
    description: str,
) -> bool:
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        return False
    try:
        _ = register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=schema,
            handler=handler,
            description=description,
        )
    except TypeError:
        pass
    else:
        return True

    try:
        _ = register_tool(name, handler)
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

    def _status_tool(
        args: dict[str, object],
        session_id: str = "",
        **_kwargs: JsonValue,
    ) -> str:
        _ = session_id
        return tokenjuice_status(args, store_path=store_path)

    return _try_register_tool(
        ctx,
        name="tokenjuice_status",
        schema=_STATUS_TOOL_SCHEMA,
        handler=_status_tool,
        description="Return aggregate tokenjuice-hermes rescue status.",
    )
