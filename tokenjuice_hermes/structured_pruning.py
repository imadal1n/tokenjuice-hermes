"""TokenJuice structured context-pruning policy for Hermes.

The policy runs over Hermes' structured contributions before request assembly and
pressure checks. It is disabled by default and fail-open: malformed config or
unexpected input returns ``None`` so that Hermes falls back to its normal
compaction path.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from .observability import record_structured_pruning
from .structured_pruning_config import parse_config
from .structured_pruning_groups import (
    build_groups,
    parse_contributions,
    resolve_now_ms,
    select_pruned_groups,
)
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
    current_pressure_tokens: int,
    threshold_tokens: int,
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
    current_pressure_tokens: int,
    threshold_tokens: int,
    context: dict[str, JsonValue],
) -> StructuredPruningResult | None:
    prepared = _prepare_prune(contributions, current_pressure_tokens, threshold_tokens, context)
    if prepared is None:
        return None
    parsed_contributions, config, route, target_tokens = prepared

    required_savings = max(0, current_pressure_tokens - target_tokens)

    now_ms = resolve_now_ms(parsed_contributions, context)
    groups = build_groups(parsed_contributions, config, now_ms)
    pruned_groups = select_pruned_groups(groups, route, required_savings, config)
    if pruned_groups is None:
        return None

    pruned_ids = {c.id for group in pruned_groups for c in group.contributions}
    retained = [c for c in parsed_contributions if c.id not in pruned_ids]
    saved_tokens = sum(group.savings for group in pruned_groups)
    pruned_count = sum(len(group.contributions) for group in pruned_groups)

    if config.accounting_enabled:
        with contextlib.suppress(Exception):
            record_structured_pruning(
                pruned_count=pruned_count,
                saved_tokens=saved_tokens,
                phase=_string_context(context, "phase"),
            )

    return {
        "effective_contributions": [c.original for c in retained],
        "effective_messages": [
            c.original for c in retained if c.kind in ("message", "tool_interaction")
        ],
        "effective_tools": [c.original for c in retained if c.kind == "tool_schema"],
        "accounting": {
            "saved_tokens": saved_tokens,
            "pruned_count": pruned_count,
            "pruned_groups": len(pruned_groups),
        },
    }


def _prepare_prune(
    contributions: Sequence[Contribution],
    current_pressure_tokens: int,
    threshold_tokens: int,
    context: dict[str, JsonValue],
) -> tuple[tuple[ContributionInternal, ...], PruningConfig, PressureRoute, int] | None:
    config = parse_config(context)
    if config is None or not config.enabled:
        return None

    if threshold_tokens <= 0:
        return None
    if current_pressure_tokens < 0:
        return None

    parsed_contributions = parse_contributions(contributions)
    if parsed_contributions is None:
        return None

    trigger_tokens = int(config.threshold_tokens * config.trigger_ratio / 100)
    if current_pressure_tokens < trigger_tokens:
        return None

    route = (
        PressureRoute.HARD
        if current_pressure_tokens >= config.threshold_tokens
        else PressureRoute.SOFT
    )
    target_ratio = (
        config.hard_target_ratio
        if route == PressureRoute.HARD
        else config.soft_target_ratio
    )
    target_tokens = int(config.threshold_tokens * target_ratio / 100)

    return parsed_contributions, config, route, target_tokens


def _string_context(context: dict[str, JsonValue], key: str) -> str:
    value = context.get(key)
    return value if isinstance(value, str) else ""
