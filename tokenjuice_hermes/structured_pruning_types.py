"""Shared types and constants for structured context pruning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict

from .json_types import JsonValue

DEFAULT_ENABLED: Final[bool] = False
DEFAULT_TRIGGER_RATIO: Final[int] = 80
DEFAULT_TARGET_RATIO: Final[int] = 75
DEFAULT_SOFT_TARGET_RATIO: Final[int] = 75
DEFAULT_HARD_TARGET_RATIO: Final[int] = 65
DEFAULT_MIN_SAVED_TOKENS: Final[int] = 256
DEFAULT_CACHE_TTL_SECONDS: Final[int] = 3600
DEFAULT_PROTECT_RECENT_MESSAGES: Final[int] = 8
DEFAULT_PROTECT_RECENT_TOOL_INTERACTIONS: Final[int] = 2
DEFAULT_CLASSES: Final[str] = "terminal_tool_output"
DEFAULT_ACCOUNTING_ENABLED: Final[bool] = True

STRUCTURED_PRUNING_MARKER: Final[str] = "[tokenjuice-hermes: structured context pruning"

SOFT_TRIM_POLICY: Final[str] = "soft_trim"
HARD_CLEAR_POLICY: Final[str] = "hard_clear_allowed"

PROTECTED_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "identity",
        "system_instruction",
        "project_context",
        "memory_context",
        "skill_context",
        "developer_instruction",
        "user_message",
        "tool_schema",
        "exact_file_read",
        "diagnostic",
        "unknown",
    }
)
STABLE_STABILITIES: Final[frozenset[str]] = frozenset({"stable_prefix", "session_stable"})
PREFIX_CACHE_SCOPES: Final[frozenset[str]] = frozenset({"prefix", "body"})


Contribution = TypedDict(
    "Contribution",
    {
        "id": str,
        "kind": str,
        "provenance": str,
        "class": str,
        "stability": str,
        "content_hash": str,
        "cache_scope": str,
        "token_estimate": int,
        "char_count": int,
        "atomic_group_id": str | None,
        "prune_policy": str,
        "protected_reason": str,
        "created_at_epoch_ms": int | None,
        "content": str,
        "tool_calls": list[JsonValue],
    },
)


class StructuredPruningAccounting(TypedDict):
    """Redacted accounting for a structured pruning decision."""

    saved_tokens: int
    pruned_count: int
    pruned_groups: int


class StructuredPruningResult(TypedDict):
    """Effective pruned prompt view plus redacted accounting."""

    effective_contributions: list[Contribution]
    effective_messages: list[Contribution]
    effective_tools: list[Contribution]
    accounting: StructuredPruningAccounting


@dataclass(frozen=True, slots=True)
class PruningConfig:
    enabled: bool
    threshold_tokens: int
    trigger_ratio: int
    target_ratio: int
    soft_target_ratio: int
    hard_target_ratio: int
    min_saved_tokens: int
    cache_ttl_seconds: int
    protect_recent_messages: int
    protect_recent_tool_interactions: int
    classes: frozenset[str]
    accounting_enabled: bool


@dataclass(frozen=True, slots=True)
class ContributionInternal:
    id: str
    kind: str
    provenance: str
    class_: str
    stability: str
    content_hash: str
    cache_scope: str
    token_estimate: int
    char_count: int
    atomic_group_id: str | None
    prune_policy: str
    protected_reason: str
    created_at_epoch_ms: int | None
    original_index: int
    original: Contribution


@dataclass(frozen=True, slots=True)
class Group:
    index: int
    savings: int
    contributions: tuple[ContributionInternal, ...]
    has_disposable: bool
    soft_allowed: bool
    hard_allowed: bool
    class_protected: bool
    ttl_protected: bool
    recent_message_protected: bool
    recent_tool_protected: bool
    oldest_created_at_ms: int | None


class PressureRoute(StrEnum):
    SOFT = "soft"
    HARD = "hard"
