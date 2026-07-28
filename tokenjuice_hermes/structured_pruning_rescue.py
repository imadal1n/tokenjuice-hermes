"""Rescue-backed replacements for structured pruning candidates."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .rescue_excerpt import build_excerpt
from .rescue_store import BlobStore
from .rescue_types import RESCUE_STORE_PATH_DEFAULT
from .structured_pruning_types import (
    RESCUE_BACKED_CLASSES,
    Contribution,
    ContributionInternal,
    Group,
)

if TYPE_CHECKING:
    from .json_types import JsonValue


class RescueReplacementError(Exception):
    """Raised when selected rescue-backed pruning cannot produce a fetchable handle."""


def apply_pruned_groups(
    parsed_contributions: tuple[ContributionInternal, ...],
    pruned_groups: list[Group],
    context: dict[str, JsonValue],
) -> tuple[list[ContributionInternal], int, int] | tuple[None, int, int]:
    """Apply selected pruning groups, replacing rescue-backed classes with fetchable excerpts."""
    pruned_by_id = {c.id: c for group in pruned_groups for c in group.contributions}
    replacements = _rescue_replacements(pruned_by_id, context)
    if replacements is None:
        return None, 0, 0

    retained: list[ContributionInternal] = []
    saved_tokens = 0
    pruned_count = 0
    for contribution in parsed_contributions:
        selected = pruned_by_id.get(contribution.id)
        if selected is None:
            retained.append(contribution)
            continue

        replacement = replacements.get(contribution.id)
        if replacement is None:
            saved_tokens += contribution.token_estimate
            pruned_count += 1
            continue

        retained.append(replacement)
        saved_tokens += max(0, contribution.token_estimate - replacement.token_estimate)
        pruned_count += 1

    return retained, saved_tokens, pruned_count


def _rescue_replacements(
    pruned_by_id: dict[str, ContributionInternal],
    context: dict[str, JsonValue],
) -> dict[str, ContributionInternal] | None:
    rescue_candidates = [
        contribution
        for contribution in pruned_by_id.values()
        if contribution.class_ in RESCUE_BACKED_CLASSES
    ]
    if not rescue_candidates:
        return {}

    session_id = _string_context(context, "session_id")
    if not session_id or _bool_context(context, "tokenjuice_rescue_fetch_available") is not True:
        return None

    try:
        store = BlobStore({"store_path": _rescue_store_path(context)})
        return {
            contribution.id: _rescue_contribution(contribution, store, session_id)
            for contribution in rescue_candidates
        }
    except (OSError, RescueReplacementError, ValueError):
        return None


def _rescue_contribution(
    contribution: ContributionInternal,
    store: BlobStore,
    session_id: str,
) -> ContributionInternal:
    tool_name = _provider_tool_name(contribution)
    handle = store.put(contribution.original["content"], tool_name=tool_name, session_id=session_id)
    if not handle:
        raise RescueReplacementError

    content = build_excerpt(contribution.original["content"], handle=handle)
    original = dict(contribution.original)
    original["content"] = content
    provider_message = original.get("provider_message")
    if isinstance(provider_message, dict):
        original["provider_message"] = {**provider_message, "content": content}
    return ContributionInternal(
        id=contribution.id,
        kind=contribution.kind,
        provenance=contribution.provenance,
        class_=contribution.class_,
        stability=contribution.stability,
        content_hash=contribution.content_hash,
        cache_scope=contribution.cache_scope,
        token_estimate=_estimated_tokens(content),
        char_count=len(content),
        atomic_group_id=contribution.atomic_group_id,
        prune_policy=contribution.prune_policy,
        protected_reason=contribution.protected_reason,
        created_at_epoch_ms=contribution.created_at_epoch_ms,
        original_index=contribution.original_index,
        original=cast("Contribution", cast("object", original)),
    )


def _provider_tool_name(contribution: ContributionInternal) -> str:
    provider_message = contribution.original.get("provider_message")
    if isinstance(provider_message, dict):
        name = provider_message.get("name")
        if isinstance(name, str) and name:
            return name
    return _tool_name_for_class(contribution.class_)


def _tool_name_for_class(class_: str) -> str:
    if class_ == "terminal_tool_output":
        return "terminal"
    if class_ == "exact_file_read":
        return "read_file"
    return "tool"


def _rescue_store_path(context: dict[str, JsonValue]) -> str:
    value = context.get("tokenjuice_rescue_store_path")
    return value if isinstance(value, str) and value else RESCUE_STORE_PATH_DEFAULT


def _bool_context(context: dict[str, JsonValue], key: str) -> bool | None:
    value = context.get(key)
    return value if isinstance(value, bool) else None


def _string_context(context: dict[str, JsonValue], key: str) -> str:
    value = context.get(key)
    return value if isinstance(value, str) else ""


def _estimated_tokens(content: str) -> int:
    return max(1, len(content) // 4)
