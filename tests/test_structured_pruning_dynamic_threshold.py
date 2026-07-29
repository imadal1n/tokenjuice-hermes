from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from tests.host_fixtures import HermesHost
from tokenjuice_hermes.observability import reset_stats, status_snapshot
from tokenjuice_hermes.plugin import register

if TYPE_CHECKING:
    from tokenjuice_hermes.json_types import JsonValue
    from tokenjuice_hermes.structured_pruning_types import Contribution, StructuredPruningResult


_DETERMINISTIC_EPOCH_MS: int = 10_000_000_000
_GPT_5_6_THRESHOLD_TOKENS: int = 316_200
_GPT_5_6_TRIGGER_TOKENS: int = 252_960
_GPT_5_6_TARGET_TOKENS: int = 237_150


def _config_without_static_threshold() -> dict[str, JsonValue]:
    return {
        "tokenjuice_prompt_pruning_enabled": True,
        "tokenjuice_prompt_pruning_trigger_ratio": 80,
        "tokenjuice_prompt_pruning_target_ratio": 75,
        "tokenjuice_prompt_pruning_soft_target_ratio": 75,
        "tokenjuice_prompt_pruning_hard_target_ratio": 75,
        "tokenjuice_prompt_pruning_min_saved_tokens": 256,
        "tokenjuice_prompt_pruning_cache_ttl_seconds": 3600,
        "tokenjuice_prompt_pruning_protect_recent_messages": 0,
        "tokenjuice_prompt_pruning_protect_recent_tool_interactions": 0,
        "tokenjuice_prompt_pruning_classes": "terminal_tool_output",
        "tokenjuice_prompt_pruning_accounting_enabled": True,
    }


def _user_contribution(*, token_estimate: int) -> Contribution:
    return _contribution(
        {
            "id": "user",
            "kind": "message",
            "class": "user_message",
            "token_estimate": token_estimate,
            "prune_policy": "never",
            "content": "question",
            "stability": "session_stable",
        }
    )


def _terminal_contribution(
    *,
    contribution_id: str,
    token_estimate: int,
    prune_policy: str,
) -> Contribution:
    return _contribution(
        {
            "id": contribution_id,
            "kind": "tool_interaction",
            "class": "terminal_tool_output",
            "token_estimate": token_estimate,
            "prune_policy": prune_policy,
            "content": f"{contribution_id} output",
            "stability": "turn_ephemeral",
        }
    )


def _contribution(fields: dict[str, str | int]) -> Contribution:
    content = str(fields["content"])
    kind = str(fields["kind"])
    class_ = str(fields["class"])
    return {
        "id": str(fields["id"]),
        "kind": kind,
        "provenance": "conversation_history",
        "class": class_,
        "stability": str(fields["stability"]),
        "cache_scope": "body",
        "token_estimate": int(fields["token_estimate"]),
        "char_count": len(content),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "atomic_group_id": None,
        "prune_policy": str(fields["prune_policy"]),
        "protected_reason": "",
        "created_at_epoch_ms": _DETERMINISTIC_EPOCH_MS - 7_200_000,
        "content": content,
        "tool_calls": [],
        "provider_message": _provider_message(kind=kind, class_=class_, content=content),
    }


def _provider_message(*, kind: str, class_: str, content: str) -> dict[str, JsonValue]:
    if class_ == "user_message":
        return {"role": "user", "content": content}
    if kind == "tool_interaction":
        return {
            "role": "tool",
            "tool_call_id": "call-terminal",
            "name": "terminal",
            "content": content,
        }
    return {"role": "assistant", "content": content}


def _callback_result(
    contributions: list[Contribution],
    *,
    current_pressure_tokens: int,
    target_tokens: int = _GPT_5_6_TARGET_TOKENS,
) -> StructuredPruningResult | None:
    host = HermesHost(config=_config_without_static_threshold(), session_id="session-a")
    register(host)
    callback = host.callbacks["structured_context_prune"]

    result = callback(
        contributions,
        phase="preflight",
        session_id="session-a",
        turn_id="turn-a",
        current_pressure_tokens=current_pressure_tokens,
        threshold_tokens=_GPT_5_6_THRESHOLD_TOKENS,
        trigger_tokens=_GPT_5_6_TRIGGER_TOKENS,
        target_tokens=target_tokens,
        now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
    )
    if result is None:
        return None
    assert isinstance(result, dict)
    return cast("StructuredPruningResult", cast("object", result))


def test_registered_callback_uses_dynamic_threshold_when_config_omits_static_threshold() -> None:
    # Given: Hermes supplies threshold_tokens dynamically, not duplicated static config.
    reset_stats()
    user = _user_contribution(token_estimate=236_960)
    terminal = _terminal_contribution(
        contribution_id="old-terminal",
        token_estimate=16_000,
        prune_policy="soft_trim",
    )

    # When: pressure reaches the GPT-5.6 TokenJuice trigger exactly.
    result = _callback_result(
        [user, terminal],
        current_pressure_tokens=_GPT_5_6_TRIGGER_TOKENS,
    )

    # Then: structured pruning returns an effective view through the real callback.
    assert result is not None
    assert result["accounting"]["saved_tokens"] == 16_000
    assert result["effective_messages"] == [{"role": "user", "content": "question"}]
    snapshot = status_snapshot()
    assert snapshot["structured_pruning_attempted_count"] == 1
    assert snapshot["structured_pruning_saved_tokens"] == 16_000


def test_registered_callback_records_insufficient_savings_before_native_fallback() -> None:
    # Given: pressure exceeds threshold but eligible terminal output cannot clear it.
    reset_stats()
    user = _user_contribution(token_estimate=321_090)
    terminal = _terminal_contribution(
        contribution_id="small-terminal",
        token_estimate=5_000,
        prune_policy="hard_clear_allowed",
    )

    # When: dynamic threshold parsing succeeds but TokenJuice cannot save enough.
    result = _callback_result(
        [user, terminal],
        current_pressure_tokens=326_090,
    )

    # Then: TokenJuice fails open and records only aggregate counters.
    assert result is None
    snapshot = status_snapshot()
    assert snapshot["structured_pruning_count"] == 0
    assert snapshot["structured_pruning_saved_tokens"] == 0
    assert snapshot["structured_pruning_insufficient_eligible_savings"] == 9_890
