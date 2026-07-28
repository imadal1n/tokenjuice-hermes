"""TokenJuice structured context-pruning policy for Hermes.

The policy runs over Hermes' structured contributions before request assembly and
pressure checks. It is disabled by default and fail-open: malformed config or
unexpected input returns ``None`` so that Hermes falls back to its normal
compaction path.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from .observability import StructuredPruningStats, record_structured_pruning
from .structured_pruning_config import parse_config
from .structured_pruning_groups import (
    build_groups,
    parse_contributions,
    resolve_now_ms,
)
from .structured_pruning_provider import (
    provider_messages_from_contributions,
    provider_tools_from_contributions,
)
from .structured_pruning_rescue import apply_pruned_groups
from .structured_pruning_selection import select_pruned_groups
from .structured_pruning_types import (
    STRUCTURED_PRUNING_MARKER,
    PressureRoute,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .json_types import JsonValue
    from .structured_pruning_types import (
        Contribution,
        ContributionInternal,
        PruningConfig,
        StructuredPruningResult,
    )

__all__ = ["STRUCTURED_PRUNING_MARKER", "prune_structured_context"]


def prune_structured_context(
    contributions: Sequence[Contribution],
    current_pressure_tokens: int | None = None,
    threshold_tokens: int | None = None,
    **context: JsonValue,
) -> StructuredPruningResult | None:
    """Return an effective pruned prompt view or ``None`` when pruning is disabled/unsafe."""
    try:
        return _prune_structured_context(
            contributions,
            current_pressure_tokens,
            threshold_tokens,
            context,
        )
    except Exception:  # noqa: BLE001 - structured pruning is fail-open by design
        return None


def _prune_structured_context(
    contributions: Sequence[Contribution],
    current_pressure_tokens: int | None,
    threshold_tokens: int | None,
    context: dict[str, JsonValue],
) -> StructuredPruningResult | None:
    prepared = _prepare_prune(contributions, current_pressure_tokens, threshold_tokens, context)
    if prepared is None:
        return None
    parsed_contributions, config, route, target_tokens, threshold, pressure = prepared

    required_savings = max(0, pressure - target_tokens)
    threshold_savings = max(0, pressure - threshold)
    fallback_savings = _fallback_savings(required_savings, threshold_savings)

    now_ms = resolve_now_ms(parsed_contributions, context)
    groups = build_groups(parsed_contributions, config, now_ms)
    pruned_groups = select_pruned_groups(
        groups,
        route,
        required_savings,
        config,
        fallback_savings=fallback_savings,
    )
    if pruned_groups is None:
        _record_structured_pruning(
            enabled=config.accounting_enabled,
            context=context,
            stats=StructuredPruningStats(insufficient_eligible_savings=threshold_savings),
        )
        return None

    attempted_count = sum(len(group.contributions) for group in pruned_groups)
    applied = apply_pruned_groups(
        parsed_contributions,
        pruned_groups,
        context,
    )
    if applied is None:
        _record_structured_pruning(
            enabled=config.accounting_enabled,
            context=context,
            stats=StructuredPruningStats(
                attempted_count=attempted_count,
                insufficient_eligible_savings=threshold_savings,
            ),
        )
        return None

    if threshold_savings > 0 and applied.saved_tokens < threshold_savings:
        _record_structured_pruning(
            enabled=config.accounting_enabled,
            context=context,
            stats=StructuredPruningStats(
                attempted_count=attempted_count,
                rescued_count=applied.rescued_count,
                insufficient_eligible_savings=threshold_savings - applied.saved_tokens,
            ),
        )
        return None

    _record_structured_pruning(
        enabled=config.accounting_enabled,
        context=context,
        pruned_count=applied.pruned_count,
        saved_tokens=applied.saved_tokens,
        stats=StructuredPruningStats(
            attempted_count=attempted_count,
            rescued_count=applied.rescued_count,
        ),
    )

    effective_messages = provider_messages_from_contributions(applied.retained)
    effective_tools = provider_tools_from_contributions(applied.retained)

    result: StructuredPruningResult = {
        "effective_contributions": [c.original for c in applied.retained],
        "effective_messages": effective_messages,
        "effective_system_prompt": "",
        "effective_tools": effective_tools,
        "accounting": {
            "saved_tokens": applied.saved_tokens,
            "pruned_count": applied.pruned_count,
            "pruned_groups": len(pruned_groups),
            "attempted_count": attempted_count,
            "rescued_count": applied.rescued_count,
            "insufficient_eligible_savings": 0,
        },
    }
    return result


def _record_structured_pruning(
    *,
    enabled: bool,
    context: dict[str, JsonValue],
    pruned_count: int = 0,
    saved_tokens: int = 0,
    stats: StructuredPruningStats | None = None,
) -> None:
    if enabled:
        with contextlib.suppress(Exception):
            record_structured_pruning(
                pruned_count=pruned_count,
                saved_tokens=saved_tokens,
                phase=_string_context(context, "phase"),
                stats=stats,
            )


def _fallback_savings(required_savings: int, threshold_savings: int) -> int:
    if threshold_savings <= 0:
        return 0
    return min(required_savings, threshold_savings * 4)


def _prepare_prune(
    contributions: Sequence[Contribution],
    current_pressure_tokens: int | None,
    threshold_tokens: int | None,
    context: dict[str, JsonValue],
) -> tuple[tuple[ContributionInternal, ...], PruningConfig, PressureRoute, int, int, int] | None:
    config = parse_config(context)
    if config is None or not config.enabled:
        return None

    effective_threshold = _resolve_threshold(threshold_tokens, context, config)
    if effective_threshold <= 0:
        return None

    parsed_contributions = parse_contributions(contributions)
    if parsed_contributions is None:
        return None

    pressure = _resolve_pressure(parsed_contributions, current_pressure_tokens, context)
    if pressure is None or pressure < 0:
        return None

    trigger_tokens = _resolve_trigger(context, config, effective_threshold)
    if trigger_tokens <= 0:
        trigger_tokens = int(effective_threshold * config.trigger_ratio / 100)
    target_tokens = _resolve_target(context, config, effective_threshold, pressure)
    if target_tokens <= 0:
        target_tokens = effective_threshold

    if pressure < trigger_tokens:
        return None

    route = PressureRoute.HARD if pressure >= effective_threshold else PressureRoute.SOFT
    return parsed_contributions, config, route, target_tokens, effective_threshold, pressure


def _resolve_threshold(
    threshold_tokens: int | None,
    context: dict[str, JsonValue],
    config: PruningConfig,
) -> int:
    if threshold_tokens is not None and threshold_tokens > 0:
        return threshold_tokens
    context_threshold = _int_context(context, "threshold_tokens")
    if context_threshold is not None and context_threshold > 0:
        return context_threshold
    return config.threshold_tokens


def _resolve_pressure(
    parsed_contributions: tuple[ContributionInternal, ...],
    current_pressure_tokens: int | None,
    context: dict[str, JsonValue],
) -> int | None:
    """Derive current request pressure from explicit data or contribution estimates."""
    if current_pressure_tokens is not None:
        return current_pressure_tokens if current_pressure_tokens >= 0 else None
    tool_schema_tokens = _int_context(context, "tool_schema_tokens") or 0
    message_tokens = sum(c.token_estimate for c in parsed_contributions if c.kind != "tool_schema")
    pressure = message_tokens + tool_schema_tokens
    return pressure if pressure > 0 else None


def _resolve_trigger(
    context: dict[str, JsonValue],
    config: PruningConfig,
    threshold_tokens: int,
) -> int:
    context_trigger = _int_context(context, "trigger_tokens")
    if context_trigger is not None and context_trigger > 0:
        return context_trigger
    return int(threshold_tokens * config.trigger_ratio / 100)


def _resolve_target(
    context: dict[str, JsonValue],
    config: PruningConfig,
    threshold_tokens: int,
    pressure: int,
) -> int:
    context_target = _int_context(context, "target_tokens")
    if context_target is not None and context_target > 0:
        return context_target
    route = PressureRoute.HARD if pressure >= threshold_tokens else PressureRoute.SOFT
    ratio = config.hard_target_ratio if route == PressureRoute.HARD else config.soft_target_ratio
    return int(threshold_tokens * ratio / 100)


def _int_context(context: dict[str, JsonValue], key: str) -> int | None:
    value = context.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _string_context(context: dict[str, JsonValue], key: str) -> str:
    value = context.get(key)
    return value if isinstance(value, str) else ""
