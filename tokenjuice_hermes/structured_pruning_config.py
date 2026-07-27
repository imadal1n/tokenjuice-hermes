"""Configuration parsing for structured context pruning."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from .structured_pruning_types import (
    DEFAULT_ACCOUNTING_ENABLED,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_CLASSES,
    DEFAULT_ENABLED,
    DEFAULT_HARD_TARGET_RATIO,
    DEFAULT_MIN_SAVED_TOKENS,
    DEFAULT_PROTECT_RECENT_MESSAGES,
    DEFAULT_PROTECT_RECENT_TOOL_INTERACTIONS,
    DEFAULT_SOFT_TARGET_RATIO,
    DEFAULT_TARGET_RATIO,
    DEFAULT_TRIGGER_RATIO,
    PruningConfig,
)

if TYPE_CHECKING:
    from .json_types import JsonValue


TOKENJUICE_PROMPT_PRUNING_ENABLED: str = "tokenjuice_prompt_pruning_enabled"
TOKENJUICE_PROMPT_PRUNING_THRESHOLD_TOKENS: str = "tokenjuice_prompt_pruning_threshold_tokens"
TOKENJUICE_PROMPT_PRUNING_TRIGGER_RATIO: str = "tokenjuice_prompt_pruning_trigger_ratio"
TOKENJUICE_PROMPT_PRUNING_TARGET_RATIO: str = "tokenjuice_prompt_pruning_target_ratio"
TOKENJUICE_PROMPT_PRUNING_SOFT_TARGET_RATIO: str = "tokenjuice_prompt_pruning_soft_target_ratio"
TOKENJUICE_PROMPT_PRUNING_HARD_TARGET_RATIO: str = "tokenjuice_prompt_pruning_hard_target_ratio"
TOKENJUICE_PROMPT_PRUNING_MIN_SAVED_TOKENS: str = "tokenjuice_prompt_pruning_min_saved_tokens"
TOKENJUICE_PROMPT_PRUNING_CACHE_TTL_SECONDS: str = "tokenjuice_prompt_pruning_cache_ttl_seconds"
TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_MESSAGES: str = (
    "tokenjuice_prompt_pruning_protect_recent_messages"
)
TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_TOOL_INTERACTIONS: str = (
    "tokenjuice_prompt_pruning_protect_recent_tool_interactions"
)
TOKENJUICE_PROMPT_PRUNING_CLASSES: str = "tokenjuice_prompt_pruning_classes"
TOKENJUICE_PROMPT_PRUNING_ACCOUNTING_ENABLED: str = "tokenjuice_prompt_pruning_accounting_enabled"

MAX_PERCENTAGE: Final[int] = 100


class ConfigParseError(Exception):
    """Raised when a structured pruning configuration value is invalid."""

    key: str

    def __init__(self, key: str, message: str) -> None:
        """Record the invalid key and explanatory message."""
        super().__init__(message)
        self.key = key


def parse_config(context: dict[str, JsonValue]) -> PruningConfig | None:
    """Parse structured pruning config from flat keyword args."""
    try:
        return _parse_config(context)
    except ConfigParseError:
        return None


def _parse_config(context: dict[str, JsonValue]) -> PruningConfig:
    enabled = _parse_bool(context, TOKENJUICE_PROMPT_PRUNING_ENABLED, default=DEFAULT_ENABLED)
    threshold_tokens = _parse_positive_int(
        context, TOKENJUICE_PROMPT_PRUNING_THRESHOLD_TOKENS, None
    )
    trigger_ratio = _parse_ratio(
        context, TOKENJUICE_PROMPT_PRUNING_TRIGGER_RATIO, DEFAULT_TRIGGER_RATIO
    )
    target_ratio = _parse_ratio(
        context, TOKENJUICE_PROMPT_PRUNING_TARGET_RATIO, DEFAULT_TARGET_RATIO
    )
    soft_target_ratio = _parse_ratio(
        context, TOKENJUICE_PROMPT_PRUNING_SOFT_TARGET_RATIO, DEFAULT_SOFT_TARGET_RATIO
    )
    hard_target_ratio = _parse_ratio(
        context, TOKENJUICE_PROMPT_PRUNING_HARD_TARGET_RATIO, DEFAULT_HARD_TARGET_RATIO
    )
    min_saved_tokens = _parse_nonnegative_int(
        context, TOKENJUICE_PROMPT_PRUNING_MIN_SAVED_TOKENS, DEFAULT_MIN_SAVED_TOKENS
    )
    cache_ttl_seconds = _parse_nonnegative_int(
        context, TOKENJUICE_PROMPT_PRUNING_CACHE_TTL_SECONDS, DEFAULT_CACHE_TTL_SECONDS
    )
    protect_recent_messages = _parse_nonnegative_int(
        context, TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_MESSAGES, DEFAULT_PROTECT_RECENT_MESSAGES
    )
    protect_recent_tool_interactions = _parse_nonnegative_int(
        context,
        TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_TOOL_INTERACTIONS,
        DEFAULT_PROTECT_RECENT_TOOL_INTERACTIONS,
    )
    classes = _parse_classes(context, TOKENJUICE_PROMPT_PRUNING_CLASSES, DEFAULT_CLASSES)
    accounting_enabled = _parse_bool(
        context, TOKENJUICE_PROMPT_PRUNING_ACCOUNTING_ENABLED, default=DEFAULT_ACCOUNTING_ENABLED
    )

    for value, key in (
        (enabled, TOKENJUICE_PROMPT_PRUNING_ENABLED),
        (threshold_tokens, TOKENJUICE_PROMPT_PRUNING_THRESHOLD_TOKENS),
        (trigger_ratio, TOKENJUICE_PROMPT_PRUNING_TRIGGER_RATIO),
        (target_ratio, TOKENJUICE_PROMPT_PRUNING_TARGET_RATIO),
        (soft_target_ratio, TOKENJUICE_PROMPT_PRUNING_SOFT_TARGET_RATIO),
        (hard_target_ratio, TOKENJUICE_PROMPT_PRUNING_HARD_TARGET_RATIO),
        (min_saved_tokens, TOKENJUICE_PROMPT_PRUNING_MIN_SAVED_TOKENS),
        (cache_ttl_seconds, TOKENJUICE_PROMPT_PRUNING_CACHE_TTL_SECONDS),
        (protect_recent_messages, TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_MESSAGES),
        (
            protect_recent_tool_interactions,
            TOKENJUICE_PROMPT_PRUNING_PROTECT_RECENT_TOOL_INTERACTIONS,
        ),
        (classes, TOKENJUICE_PROMPT_PRUNING_CLASSES),
        (accounting_enabled, TOKENJUICE_PROMPT_PRUNING_ACCOUNTING_ENABLED),
    ):
        if value is None:
            raise ConfigParseError(key, f"invalid value for {key}")

    return PruningConfig(
        enabled=cast("bool", enabled),
        threshold_tokens=cast("int", threshold_tokens),
        trigger_ratio=cast("int", trigger_ratio),
        target_ratio=cast("int", target_ratio),
        soft_target_ratio=cast("int", soft_target_ratio),
        hard_target_ratio=cast("int", hard_target_ratio),
        min_saved_tokens=cast("int", min_saved_tokens),
        cache_ttl_seconds=cast("int", cache_ttl_seconds),
        protect_recent_messages=cast("int", protect_recent_messages),
        protect_recent_tool_interactions=cast("int", protect_recent_tool_interactions),
        classes=cast("frozenset[str]", classes),
        accounting_enabled=cast("bool", accounting_enabled),
    )


def _parse_bool(context: dict[str, JsonValue], key: str, *, default: bool) -> bool | None:
    value = context.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "on", "1"}
    return None


def _parse_nonnegative_int(
    context: dict[str, JsonValue], key: str, default: int | None
) -> int | None:
    value = context.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_positive_int(context: dict[str, JsonValue], key: str, default: int | None) -> int | None:
    value = _parse_nonnegative_int(context, key, default)
    if value is not None and value <= 0:
        return None
    return value


def _parse_ratio(context: dict[str, JsonValue], key: str, default: int) -> int | None:
    value = _parse_nonnegative_int(context, key, default)
    if value is None or value > MAX_PERCENTAGE:
        return None
    return value


def _parse_classes(context: dict[str, JsonValue], key: str, default: str) -> frozenset[str] | None:
    value = context.get(key)
    if value is None:
        return frozenset(item.strip() for item in default.split(",") if item.strip())
    if not isinstance(value, str):
        return None
    classes = tuple(item.strip() for item in value.split(",") if item.strip())
    if not classes:
        return None
    return frozenset(classes)
