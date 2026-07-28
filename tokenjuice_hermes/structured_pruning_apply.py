from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .observability import StructuredPruningStats
from .structured_pruning_rescue import AppliedPruning, apply_pruned_groups
from .structured_pruning_selection import candidate_groups
from .structured_pruning_types import PressureRoute

if TYPE_CHECKING:
    from .json_types import JsonValue
    from .structured_pruning_types import ContributionInternal, Group, PruningConfig


@dataclass(frozen=True, slots=True)
class PruningApplicationPlan:
    parsed_contributions: tuple[ContributionInternal, ...]
    groups: list[Group]
    pruned_groups: list[Group]
    route: PressureRoute
    config: PruningConfig
    threshold_savings: int
    context: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PruningApplicationResult:
    pruned_groups: list[Group]
    attempted_count: int
    applied: AppliedPruning
    failure_stats: StructuredPruningStats | None = None


def apply_threshold_pruning(
    plan: PruningApplicationPlan,
) -> PruningApplicationResult | None:
    attempted_count = sum(len(group.contributions) for group in plan.pruned_groups)
    applied = apply_pruned_groups(
        plan.parsed_contributions,
        plan.pruned_groups,
        plan.context,
    )
    if applied is None:
        return None

    if plan.threshold_savings <= 0 or applied.saved_tokens >= plan.threshold_savings:
        return PruningApplicationResult(plan.pruned_groups, attempted_count, applied)

    retry_groups = candidate_groups(
        plan.groups,
        plan.route,
        plan.config,
        include_ttl_protected=plan.route == PressureRoute.HARD,
    )
    if {group.index for group in retry_groups} == {group.index for group in plan.pruned_groups}:
        return PruningApplicationResult(plan.pruned_groups, attempted_count, applied)

    retry_attempted_count = sum(len(group.contributions) for group in retry_groups)
    retry_applied = apply_pruned_groups(
        plan.parsed_contributions,
        retry_groups,
        plan.context,
    )
    if retry_applied is None:
        return PruningApplicationResult(
            plan.pruned_groups,
            retry_attempted_count,
            applied,
            StructuredPruningStats(
                attempted_count=retry_attempted_count,
                rescued_count=applied.rescued_count,
                insufficient_eligible_savings=plan.threshold_savings,
            ),
        )
    return PruningApplicationResult(retry_groups, retry_attempted_count, retry_applied)
