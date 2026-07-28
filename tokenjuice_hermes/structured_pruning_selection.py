"""Candidate selection for structured pruning groups."""

from __future__ import annotations

from .structured_pruning_types import Group, PressureRoute, PruningConfig


def select_pruned_groups(
    groups: list[Group],
    route: PressureRoute,
    required_savings: int,
    config: PruningConfig,
    *,
    fallback_savings: int = 0,
) -> list[Group] | None:
    """Choose groups to prune while maximizing LCP and honoring policy routes."""
    core = _candidate_groups(groups, route, config, include_ttl_protected=False)
    if required_savings <= 0:
        return []
    full = _optimal_subset(core, required_savings)
    if full is not None:
        return full

    if route == PressureRoute.HARD:
        extended = _candidate_groups(groups, route, config, include_ttl_protected=True)
        full_extended = _optimal_subset(extended, required_savings)
        if full_extended is not None:
            return full_extended

        if fallback_savings > 0:
            fallback_extended = _optimal_subset(extended, fallback_savings)
            if fallback_extended is not None:
                return fallback_extended

    if fallback_savings > 0:
        fallback = _optimal_subset(core, fallback_savings)
        if fallback is not None:
            return fallback

    return _partial_subset(core)


def _candidate_groups(
    groups: list[Group],
    route: PressureRoute,
    config: PruningConfig,
    *,
    include_ttl_protected: bool,
) -> list[Group]:
    candidates: list[Group] = []
    for group in groups:
        if group.class_protected or not group.has_disposable:
            continue
        if group.recent_message_protected or group.recent_tool_protected:
            continue
        if group.ttl_protected and not include_ttl_protected:
            continue
        if route == PressureRoute.SOFT and not group.soft_allowed:
            continue
        if route == PressureRoute.HARD and not group.hard_allowed:
            continue
        if group.savings < config.min_saved_tokens:
            continue
        candidates.append(group)
    return candidates


def _optimal_subset(candidates: list[Group], required_savings: int) -> list[Group] | None:
    """Find the subset meeting the target with the latest earliest mutation."""
    if required_savings <= 0:
        return []
    if not candidates:
        return None

    indices = sorted({group.index for group in candidates})
    for earliest in reversed(indices):
        available = [group for group in candidates if group.index >= earliest]
        available.sort(key=lambda group: (-group.savings, group.oldest_created_at_ms or 0))
        selected: list[Group] = []
        total = 0
        for group in available:
            selected.append(group)
            total += group.savings
            if total >= required_savings:
                return selected
    return None


def _partial_subset(candidates: list[Group]) -> list[Group] | None:
    """Return a safe single-group prune when the target cannot be met."""
    if not candidates:
        return None
    latest = max(candidates, key=lambda group: group.index)
    return [latest]
