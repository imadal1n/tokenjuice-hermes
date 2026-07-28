from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .json_types import JsonScalar, JsonValue
    from .structured_pruning_types import Contribution

_MAX_MEMO_ENTRIES: Final[int] = 32
_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "session_id",
    "turn_id",
    "task_id",
    "request_id",
    "conversation_id",
)
_EXCLUDED_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"phase"})
_PRUNING_CONFIG_PREFIXES: Final[tuple[str, ...]] = (
    "tokenjuice_prompt_pruning_",
    "tokenjuice_rescue_",
)
_CONTRIBUTION_KEYS: Final[tuple[str, ...]] = (
    "id",
    "kind",
    "provenance",
    "class",
    "stability",
    "content_hash",
    "token_estimate",
    "char_count",
    "atomic_group_id",
    "prune_policy",
    "protected_reason",
    "created_at_epoch_ms",
)


@dataclass(frozen=True, slots=True)
class StructuredPruningMemoRequest:
    contributions: Sequence[Contribution]
    current_pressure_tokens: int | None
    threshold_tokens: int | None
    config: dict[str, JsonScalar]


@dataclass(slots=True)
class StructuredPruningMemo:
    max_entries: int = _MAX_MEMO_ENTRIES
    _results: OrderedDict[str, dict[str, JsonValue]] = field(default_factory=OrderedDict)

    def reuse_or_compute(
        self,
        request: StructuredPruningMemoRequest,
        compute: Callable[[], dict[str, JsonValue] | None],
    ) -> dict[str, JsonValue] | None:
        key = _fingerprint(request)
        if key is None:
            return compute()

        cached = self._results.get(key)
        if cached is not None:
            self._results.move_to_end(key)
            cached_copy = _copy_result(cached)
            return cached if cached_copy is None else cached_copy

        result = compute()
        if result is None:
            return None
        result_copy = _copy_result(result)
        if result_copy is not None:
            self._results[key] = result_copy
            self._results.move_to_end(key)
            while len(self._results) > max(1, self.max_entries):
                _ = self._results.popitem(last=False)
        return result


def _fingerprint(request: StructuredPruningMemoRequest) -> str | None:
    try:
        payload: dict[str, JsonValue] = {
            "identity": _identity(request.config),
            "current_pressure_tokens": request.current_pressure_tokens,
            "threshold_tokens": request.threshold_tokens,
            "config": _pruning_config(request.config),
            "contributions": _contribution_fingerprints(request.contributions),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (KeyError, TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identity(config: dict[str, JsonScalar]) -> dict[str, JsonValue]:
    return {key: config.get(key) for key in _IDENTITY_KEYS if key in config}


def _pruning_config(config: dict[str, JsonScalar]) -> dict[str, JsonValue]:
    return {
        key: value
        for key, value in sorted(config.items())
        if key not in _EXCLUDED_CONFIG_KEYS and key.startswith(_PRUNING_CONFIG_PREFIXES)
    }


def _contribution_fingerprints(
    contributions: Sequence[Contribution],
) -> list[JsonValue]:
    rows: list[JsonValue] = [
        {key: contribution.get(key) for key in _CONTRIBUTION_KEYS} for contribution in contributions
    ]
    return rows


def _copy_result(result: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    try:
        copied = deepcopy(result)
    except (TypeError, ValueError):
        return None
    return copied
