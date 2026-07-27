"""Contribution grouping, protection, and candidate selection for pruning."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .structured_pruning_types import (
    HARD_CLEAR_POLICY,
    PREFIX_CACHE_SCOPES,
    PROTECTED_CLASSES,
    SOFT_TRIM_POLICY,
    STABLE_STABILITIES,
    ContributionInternal,
    Group,
    PressureRoute,
    PruningConfig,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .json_types import JsonValue
    from .structured_pruning_types import Contribution


def parse_contributions(
    contributions: Sequence[Contribution],
) -> tuple[ContributionInternal, ...] | None:
    """Validate and convert raw Hermes contributions into internal values."""
    parsed: list[ContributionInternal] = []
    for index, raw in enumerate(contributions):
        contribution = _parse_contribution(raw, index)
        if contribution is None:
            return None
        parsed.append(contribution)
    return tuple(parsed)


def _parse_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_int(value: object, *, min_value: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if min_value is not None and value < min_value:
        return None
    return value


def _parse_optional_string(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else None


def _parse_contribution(raw: Contribution, index: int) -> ContributionInternal | None:
    id_ = _parse_string(raw["id"])
    kind = _parse_string(raw["kind"])
    provenance = _parse_string(raw["provenance"])
    class_ = _parse_string(raw["class"])
    stability = _parse_string(raw["stability"])
    content_hash = _parse_string(raw["content_hash"])
    cache_scope = _parse_string(raw["cache_scope"])
    prune_policy = _parse_string(raw["prune_policy"])

    if any(
        value is None
        for value in (
            id_,
            kind,
            provenance,
            class_,
            stability,
            content_hash,
            cache_scope,
            prune_policy,
        )
    ):
        return None

    id_ = cast("str", id_)
    kind = cast("str", kind)
    provenance = cast("str", provenance)
    class_ = cast("str", class_)
    stability = cast("str", stability)
    content_hash = cast("str", content_hash)
    cache_scope = cast("str", cache_scope)
    prune_policy = cast("str", prune_policy)

    token_estimate = _parse_int(raw["token_estimate"], min_value=0)
    char_count = _parse_int(raw["char_count"], min_value=0)
    if token_estimate is None or char_count is None:
        return None

    protected_reason = raw.get("protected_reason", "")

    atomic_group_id = _parse_optional_string(raw.get("atomic_group_id"))

    raw_age = raw.get("created_at_epoch_ms")
    if raw_age is None:
        created_at_epoch_ms: int | None = None
    else:
        created_at_epoch_ms = _parse_int(raw_age, min_value=0)
        if created_at_epoch_ms is None:
            return None

    return ContributionInternal(
        id=id_,
        kind=kind,
        provenance=provenance,
        class_=class_,
        stability=stability,
        content_hash=content_hash,
        cache_scope=cache_scope,
        token_estimate=token_estimate,
        char_count=char_count,
        atomic_group_id=atomic_group_id,
        prune_policy=prune_policy,
        protected_reason=protected_reason,
        created_at_epoch_ms=created_at_epoch_ms,
        original_index=index,
        original=raw,
    )


def resolve_now_ms(
    contributions: tuple[ContributionInternal, ...], context: dict[str, JsonValue]
) -> int | None:
    """Return deterministic epoch ms or derive a safe default from contributions."""
    explicit = context.get("now_epoch_ms")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    known_ages = [c.created_at_epoch_ms for c in contributions if c.created_at_epoch_ms is not None]
    if not known_ages:
        return None
    return max(known_ages) + 1000


def build_groups(
    contributions: tuple[ContributionInternal, ...], config: PruningConfig, now_ms: int | None
) -> list[Group]:
    """Group atomic contributions and compute protection/candidate flags."""
    atomic_members: dict[str, list[tuple[int, ContributionInternal]]] = {}
    singletons: list[tuple[int, ContributionInternal]] = []
    for contribution in contributions:
        if contribution.atomic_group_id is None:
            singletons.append((contribution.original_index, contribution))
        else:
            atomic_members.setdefault(contribution.atomic_group_id, []).append(
                (contribution.original_index, contribution)
            )

    groups: list[Group] = []
    for _, contribution in singletons:
        groups.append(
            _build_group(
                (contribution,),
                config,
                now_ms,
                recent_message_protected=False,
                recent_tool_protected=False,
            )
        )
    for members in atomic_members.values():
        contributions_tuple = tuple(
            contribution for _, contribution in sorted(members, key=lambda pair: pair[0])
        )
        groups.append(
            _build_group(
                contributions_tuple,
                config,
                now_ms,
                recent_message_protected=False,
                recent_tool_protected=False,
            )
        )

    groups.sort(key=lambda group: group.index)

    protected_message_indices = _recent_message_indices(
        contributions, config.protect_recent_messages
    )
    protected_tool_group_indices = _recent_tool_group_indices(
        groups, config.protect_recent_tool_interactions
    )

    return [
        _build_group(
            group.contributions,
            config,
            now_ms,
            recent_message_protected=any(
                c.original_index in protected_message_indices for c in group.contributions
            ),
            recent_tool_protected=group.index in protected_tool_group_indices,
        )
        for group in groups
    ]


def _recent_message_indices(
    contributions: tuple[ContributionInternal, ...], count: int
) -> set[int]:
    if count <= 0:
        return set()
    message_indices = [c.original_index for c in contributions if c.kind == "message"]
    return set(message_indices[-count:])


def _recent_tool_group_indices(groups: list[Group], count: int) -> set[int]:
    if count <= 0:
        return set()
    tool_groups = [
        group.index
        for group in groups
        if any(c.kind == "tool_interaction" for c in group.contributions)
    ]
    return set(tool_groups[-count:])


def _build_group(
    contributions: tuple[ContributionInternal, ...],
    config: PruningConfig,
    now_ms: int | None,
    *,
    recent_message_protected: bool,
    recent_tool_protected: bool,
) -> Group:
    index = min(c.original_index for c in contributions)
    savings = sum(c.token_estimate for c in contributions)
    class_protected = any(c.class_ in PROTECTED_CLASSES for c in contributions)
    ttl_protected = any(_is_ttl_protected(c, config, now_ms) for c in contributions)
    oldest_created_at_ms = _oldest_created_at(contributions)

    disposable_members = [
        c for c in contributions if c.class_ in config.classes and c.class_ not in PROTECTED_CLASSES
    ]
    has_disposable = bool(disposable_members) and any(
        c.prune_policy in {SOFT_TRIM_POLICY, HARD_CLEAR_POLICY} for c in disposable_members
    )
    soft_allowed = has_disposable and all(
        c.prune_policy == SOFT_TRIM_POLICY for c in disposable_members
    )
    hard_allowed = has_disposable and all(
        c.prune_policy in {SOFT_TRIM_POLICY, HARD_CLEAR_POLICY} for c in disposable_members
    )

    return Group(
        index=index,
        savings=savings,
        contributions=contributions,
        has_disposable=has_disposable,
        soft_allowed=soft_allowed,
        hard_allowed=hard_allowed,
        class_protected=class_protected,
        ttl_protected=ttl_protected,
        recent_message_protected=recent_message_protected,
        recent_tool_protected=recent_tool_protected,
        oldest_created_at_ms=oldest_created_at_ms,
    )


def _oldest_created_at(contributions: tuple[ContributionInternal, ...]) -> int | None:
    known = [c.created_at_epoch_ms for c in contributions if c.created_at_epoch_ms is not None]
    if not known:
        return None
    return min(known)


def _is_ttl_protected(
    contribution: ContributionInternal, config: PruningConfig, now_ms: int | None
) -> bool:
    if now_ms is None or contribution.created_at_epoch_ms is None:
        return False
    is_prune_candidate = contribution.class_ in config.classes and contribution.prune_policy in {
        SOFT_TRIM_POLICY,
        HARD_CLEAR_POLICY,
    }
    is_cache_stable = (
        contribution.stability in STABLE_STABILITIES
        and contribution.cache_scope in PREFIX_CACHE_SCOPES
    )
    if not is_prune_candidate and not is_cache_stable:
        return False
    age_seconds = max(0, (now_ms - contribution.created_at_epoch_ms) // 1000)
    return age_seconds < config.cache_ttl_seconds


def select_pruned_groups(
    groups: list[Group],
    route: PressureRoute,
    required_savings: int,
    config: PruningConfig,
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
