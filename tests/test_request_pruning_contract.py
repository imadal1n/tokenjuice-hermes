from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from tests.host_fixtures import HookOnlyHost, MiddlewareHost
from tokenjuice_hermes.json_types import JsonValue, parse_json
from tokenjuice_hermes.plugin import register

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tokenjuice_hermes.structured_pruning_types import Contribution, StructuredPruningResult

RequestObject: TypeAlias = dict[str, JsonValue]
MiddlewareCallback: TypeAlias = Callable[..., RequestObject | str | None]


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def numbered_lines(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {number:02d}" for number in range(1, count + 1))


def request_payload(
    *,
    old_tool_result: str,
    recent_tool_result: str,
    read_file_result: str,
) -> RequestObject:
    return {
        "messages": [
            {"role": "system", "content": "prune request history"},
            {"role": "user", "content": numbered_lines("context", 24)},
            {"role": "tool", "name": "terminal", "content": old_tool_result},
            {"role": "assistant", "content": numbered_lines("reply", 12)},
            {"role": "tool", "name": "terminal", "content": recent_tool_result},
            {"role": "tool", "name": "read_file", "content": read_file_result},
        ],
    }


def responses_payload(
    *,
    old_tool_result: str,
    recent_tool_result: str,
    read_file_result: str,
) -> RequestObject:
    return {
        "input": request_payload(
            old_tool_result=old_tool_result,
            recent_tool_result=recent_tool_result,
            read_file_result=read_file_result,
        )["messages"],
    }


def normalized_request(result: RequestObject | str | None) -> RequestObject:
    assert isinstance(result, str | dict)
    if isinstance(result, str):
        parsed = parse_json(result)
        assert isinstance(parsed, dict)
        return parsed
    replacement = result.get("request")
    assert isinstance(replacement, dict)
    return replacement


def request_messages(request: RequestObject) -> list[dict[str, JsonValue]]:
    messages = request.get("messages", request.get("input"))
    assert isinstance(messages, list)
    result: list[dict[str, JsonValue]] = []
    for message in messages:
        assert isinstance(message, dict)
        result.append(message)
    return result


def message_content(
    messages: list[dict[str, JsonValue]],
    *,
    name: str,
) -> list[str]:
    contents: list[str] = []
    for message in messages:
        if message.get("name") != name:
            continue
        content = message.get("content")
        assert isinstance(content, str)
        contents.append(content)
    return contents


def test_register_adds_llm_request_middleware_when_supported() -> None:
    # Given: a host that supports both hook and middleware registration.
    host = MiddlewareHost()

    # When: the plugin registers itself.
    register(host)

    # Then: the transformer hook and both middlewares are registered.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}


def test_register_stays_compatible_with_hook_only_hosts() -> None:
    # Given: a host that only supports hook registration.
    host = HookOnlyHost()

    # When: the plugin registers itself.
    register(host)

    # Then: hook-only hosts still receive transform_tool_result.
    assert host.hooks == ["transform_tool_result"]


def test_llm_request_prunes_old_terminal_results_under_pressure() -> None:
    # Given: a request with an old terminal result and enough history pressure.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    old_terminal_result = numbered_lines("old stdout", 360)
    recent_terminal_result = numbered_lines("recent stdout", 360)
    read_file_result = load_fixture("read-file.txt")
    request = request_payload(
        old_tool_result=old_terminal_result,
        recent_tool_result=recent_terminal_result,
        read_file_result=read_file_result,
    )

    # When: the llm_request middleware rewrites the request.
    result = normalized_request(middleware(request))

    # Then: older terminal history is pruned instead of copied through unchanged.
    contents = message_content(request_messages(result), name="terminal")
    assert old_terminal_result not in contents
    assert request_messages(result)[0] is not request_messages(request)[0]


def test_llm_request_prunes_responses_input_under_pressure() -> None:
    # Given: a Responses-style request with input instead of messages.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    old_terminal_result = numbered_lines("old stdout", 360)
    request = responses_payload(
        old_tool_result=old_terminal_result,
        recent_tool_result=numbered_lines("recent stdout", 360),
        read_file_result=load_fixture("read-file.txt"),
    )

    # When: the llm_request middleware rewrites the request.
    rewritten = middleware(request)
    result = request if rewritten is None else normalized_request(rewritten)

    # Then: the Responses input is pruned through the same request-copy path.
    contents = message_content(request_messages(result), name="terminal")
    assert old_terminal_result not in contents
    assert "input" in result


def test_llm_request_preserves_recent_tail_terminal_results() -> None:
    # Given: a request with a recent terminal result at the tail of history.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    recent_terminal_result = numbered_lines("recent stdout", 360)
    request = request_payload(
        old_tool_result=numbered_lines("old stdout", 360),
        recent_tool_result=recent_terminal_result,
        read_file_result=load_fixture("read-file.txt"),
    )

    # When: the llm_request middleware rewrites the request.
    result = normalized_request(middleware(request))

    # Then: the recent terminal tail remains available verbatim.
    contents = message_content(request_messages(result), name="terminal")
    assert recent_terminal_result in contents


def test_llm_request_preserves_read_file_results() -> None:
    # Given: a request containing a read_file result alongside terminal history.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    read_file_result = load_fixture("read-file.txt")
    request = request_payload(
        old_tool_result=numbered_lines("old stdout", 360),
        recent_tool_result=numbered_lines("recent stdout", 360),
        read_file_result=read_file_result,
    )

    # When: the llm_request middleware rewrites the request.
    result = normalized_request(middleware(request))

    # Then: read_file results are preserved exactly.
    contents = message_content(request_messages(result), name="read_file")
    assert read_file_result in contents


def test_llm_request_preserves_error_diagnostics() -> None:
    # Given: an old failed terminal JSON payload with exact stderr diagnostics.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    error_payload = (
        '{"command":"pytest","exit":1,"status":"failed","stdout":"'
        + numbered_lines("stdout", 360).replace("\n", "\\n")
        + '","stderr":"Traceback (most recent call last):\\nValueError: boom"}'
    )
    request = request_payload(
        old_tool_result=error_payload,
        recent_tool_result=numbered_lines("recent stdout", 360),
        read_file_result=load_fixture("read-file.txt"),
    )

    # When: request-history pruning runs under pressure.
    rewritten = middleware(request)
    result = request if rewritten is None else normalized_request(rewritten)

    # Then: diagnostic payloads remain exact instead of being truncated.
    contents = message_content(request_messages(result), name="terminal")
    assert error_payload in contents


_STRUCTURED_PRUNING_TEST_CONFIG: dict[str, bool | int | str] = {
    "tokenjuice_prompt_pruning_enabled": True,
    "tokenjuice_prompt_pruning_threshold_tokens": 10_000,
    "tokenjuice_prompt_pruning_trigger_ratio": 80,
    "tokenjuice_prompt_pruning_target_ratio": 75,
    "tokenjuice_prompt_pruning_soft_target_ratio": 75,
    "tokenjuice_prompt_pruning_hard_target_ratio": 65,
    "tokenjuice_prompt_pruning_min_saved_tokens": 256,
    "tokenjuice_prompt_pruning_cache_ttl_seconds": 3600,
    "tokenjuice_prompt_pruning_protect_recent_messages": 8,
    "tokenjuice_prompt_pruning_protect_recent_tool_interactions": 2,
    "tokenjuice_prompt_pruning_classes": "terminal_tool_output",
    "tokenjuice_prompt_pruning_accounting_enabled": True,
}


_DETERMINISTIC_EPOCH_MS: int = 10_000_000_000


def _prune_structured_context(
    contributions: Sequence[Contribution],
    *,
    current_pressure_tokens: int,
    threshold_tokens: int,
    **kwargs: JsonValue,
) -> StructuredPruningResult | None:
    from tokenjuice_hermes.structured_pruning import prune_structured_context  # noqa: PLC0415

    return prune_structured_context(
        contributions,
        current_pressure_tokens,
        threshold_tokens,
        **kwargs,
    )


def _contribution(  # noqa: PLR0913
    *,
    contribution_id: str,
    kind: str,
    provenance: str,
    class_: str,
    stability: str,
    token_estimate: int,
    prune_policy: str,
    atomic_group_id: str | None = None,
    age_seconds: int = 0,
    content: str = "",
    tool_calls: list[JsonValue] | None = None,
    protected_reason: str = "",
    cache_scope: str = "body",
) -> Contribution:
    full_content = content
    if tool_calls is not None:
        full_content = json.dumps({"content": content, "tool_calls": tool_calls})
    return {
        "id": contribution_id,
        "kind": kind,
        "provenance": provenance,
        "class": class_,
        "stability": stability,
        "cache_scope": cache_scope,
        "token_estimate": token_estimate,
        "char_count": len(full_content),
        "content_hash": hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
        "atomic_group_id": atomic_group_id,
        "prune_policy": prune_policy,
        "protected_reason": protected_reason,
        "created_at_epoch_ms": max(0, _DETERMINISTIC_EPOCH_MS - (age_seconds * 1000)),
        "content": content,
        "tool_calls": tool_calls if tool_calls is not None else [],
    }


def _trigger_tokens(cfg: dict[str, bool | int | str]) -> int:
    threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
    assert isinstance(threshold, int)
    ratio = cfg["tokenjuice_prompt_pruning_trigger_ratio"]
    assert isinstance(ratio, int)
    return int(threshold * ratio / 100)


def _target_tokens(cfg: dict[str, bool | int | str], *, hard: bool = False) -> int:
    threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
    assert isinstance(threshold, int)
    key = (
        "tokenjuice_prompt_pruning_hard_target_ratio"
        if hard
        else "tokenjuice_prompt_pruning_soft_target_ratio"
    )
    ratio = cfg[key]
    assert isinstance(ratio, int)
    return int(threshold * ratio / 100)


def _small_token_estimate(cfg: dict[str, bool | int | str]) -> int:
    min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
    assert isinstance(min_saved, int)
    return max(1, min_saved // 10)


def _pressure_over_threshold(cfg: dict[str, bool | int | str]) -> int:
    threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
    min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
    assert isinstance(threshold, int)
    assert isinstance(min_saved, int)
    return threshold + min_saved


class TestStructuredPruningPolicy:
    def test_disabled_by_default_returns_none(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_enabled"] = False
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        contributions = [
            _contribution(
                contribution_id="c1",
                kind="message",
                provenance="conversation_history",
                class_="terminal_tool_output",
                stability="volatile",
                token_estimate=threshold,
                prune_policy="hard_clear_allowed",
                content="x" * threshold,
            ),
        ]
        result = _prune_structured_context(
            contributions,
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None

    def test_invalid_config_fails_open(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        cfg["tokenjuice_prompt_pruning_trigger_ratio"] = "not-a-number"

        contributions = [
            _contribution(
                contribution_id="c1",
                kind="message",
                provenance="conversation_history",
                class_="terminal_tool_output",
                stability="volatile",
                token_estimate=threshold,
                prune_policy="hard_clear_allowed",
                content="x" * threshold,
            ),
        ]
        result = _prune_structured_context(
            contributions,
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None

    def test_cache_ttl_gates_young_prunable_contribution(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        ttl_seconds = cfg["tokenjuice_prompt_pruning_cache_ttl_seconds"]
        assert isinstance(ttl_seconds, int)
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(min_saved, int)

        young_size = _small_token_estimate(cfg)
        young_prunable = _contribution(
            contribution_id="young-terminal",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="stable_prefix",
            token_estimate=young_size,
            prune_policy="hard_clear_allowed",
            content="x" * young_size,
            age_seconds=ttl_seconds // 2,
            cache_scope="prefix",
        )
        old_disposable = _contribution(
            contribution_id="old-terminal",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=min_saved,
            prune_policy="hard_clear_allowed",
            content="y" * min_saved,
            age_seconds=ttl_seconds * 2,
        )
        current_pressure = threshold + min_saved
        result = _prune_structured_context(
            [young_prunable, old_disposable],
            current_pressure_tokens=current_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert young_prunable["id"] in kept_ids
        assert old_disposable["id"] not in kept_ids

    def test_soft_pressure_uses_only_soft_trim_candidates(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        soft_pressure = _trigger_tokens(cfg) + _small_token_estimate(cfg)

        soft = _contribution(
            contribution_id="soft",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="soft_trim",
            content="x" * threshold,
        )
        hard = _contribution(
            contribution_id="hard",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="y" * threshold,
        )
        result = _prune_structured_context(
            [soft, hard],
            current_pressure_tokens=soft_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert soft["id"] not in kept_ids
        assert hard["id"] in kept_ids

    def test_hard_pressure_allows_hard_clear_candidates(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        hard_target = _target_tokens(cfg, hard=True)
        hard_pressure = _pressure_over_threshold(cfg)

        hard = _contribution(
            contribution_id="hard",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [hard],
            current_pressure_tokens=hard_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        assert result["accounting"]["saved_tokens"] >= hard_pressure - hard_target

    def test_recent_messages_are_protected(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        protect_recent = cfg["tokenjuice_prompt_pruning_protect_recent_messages"]
        assert isinstance(protect_recent, int)
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(min_saved, int)

        recent_token_estimate = _small_token_estimate(cfg)
        old_msg = _contribution(
            contribution_id="old",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=max(threshold, min_saved),
            prune_policy="hard_clear_allowed",
            content="x" * max(threshold, min_saved),
        )
        recent_messages = [
            _contribution(
                contribution_id=f"recent-{i}",
                kind="message",
                provenance="conversation_history",
                class_="terminal_tool_output",
                stability="volatile",
                token_estimate=recent_token_estimate,
                prune_policy="hard_clear_allowed",
                content="r",
            )
            for i in range(protect_recent + 1)
        ]
        total_recent_tokens = (protect_recent + 1) * recent_token_estimate
        result = _prune_structured_context(
            [old_msg, *recent_messages],
            current_pressure_tokens=old_msg["token_estimate"] + total_recent_tokens + min_saved,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert old_msg["id"] not in kept_ids
        for recent in recent_messages[-protect_recent:]:
            assert recent["id"] in kept_ids

    def test_unknown_class_is_protected(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        unknown = _contribution(
            contribution_id="unknown",
            kind="message",
            provenance="conversation_history",
            class_="unknown",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [unknown],
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None or unknown["id"] in {
            c["id"] for c in result["effective_contributions"]
        }

    def test_atomic_tool_interaction_is_pruned_together(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        call = _contribution(
            contribution_id="call-1",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="assistant_message",
            stability="turn_ephemeral",
            token_estimate=_small_token_estimate(cfg),
            prune_policy="never",
            atomic_group_id="call-1",
            content="call",
        )
        result = _contribution(
            contribution_id="result-1",
            kind="tool_interaction",
            provenance="tool_result",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            atomic_group_id="call-1",
            content="x" * threshold,
        )
        min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(min_saved, int)
        pruned = _prune_structured_context(
            [call, result],
            current_pressure_tokens=threshold + min_saved,
            threshold_tokens=threshold,
            **cfg,
        )
        assert pruned is not None
        kept_ids = {c["id"] for c in pruned["effective_contributions"]}
        assert (call["id"] in kept_ids) == (result["id"] in kept_ids)

    def test_lcp_prefers_later_mutation_over_larger_savings(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        target = _target_tokens(cfg, hard=True)
        min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(min_saved, int)
        current_pressure = threshold + min_saved
        needed_savings = current_pressure - target

        small = _small_token_estimate(cfg)
        system_part = _contribution(
            contribution_id="sys",
            kind="system_part",
            provenance="system_prompt",
            class_="system_instruction",
            stability="stable_prefix",
            token_estimate=small,
            prune_policy="never",
            content="system",
        )
        user = _contribution(
            contribution_id="user",
            kind="message",
            provenance="conversation_history",
            class_="user_message",
            stability="stable_prefix",
            token_estimate=small,
            prune_policy="never",
            content="user",
        )
        early_large = _contribution(
            contribution_id="early-large",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=needed_savings + min_saved,
            prune_policy="hard_clear_allowed",
            content="x" * (needed_savings + min_saved),
        )
        filler = _contribution(
            contribution_id="filler",
            kind="message",
            provenance="conversation_history",
            class_="assistant_message",
            stability="turn_ephemeral",
            token_estimate=small,
            prune_policy="never",
            content="filler",
        )
        later_small = _contribution(
            contribution_id="later-small",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=needed_savings,
            prune_policy="hard_clear_allowed",
            content="y" * needed_savings,
        )
        result = _prune_structured_context(
            [system_part, user, early_large, filler, later_small],
            current_pressure_tokens=current_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert early_large["id"] in kept_ids
        assert later_small["id"] not in kept_ids

    def test_config_overrides_defaults(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_classes"] = "terminal_tool_output,custom_class"
        cfg["tokenjuice_prompt_pruning_min_saved_tokens"] = 512

        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        custom = _contribution(
            contribution_id="custom",
            kind="message",
            provenance="conversation_history",
            class_="custom_class",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [custom],
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        assert custom["id"] not in {c["id"] for c in result["effective_contributions"]}

    def test_default_classes_only_terminal_tool_output(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        other = _contribution(
            contribution_id="other",
            kind="message",
            provenance="conversation_history",
            class_="assistant_message",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [other],
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None or other["id"] in {
            c["id"] for c in result["effective_contributions"]
        }

    def test_omitted_enabled_defaults_to_disabled(self) -> None:
        cfg = {
            key: value
            for key, value in _STRUCTURED_PRUNING_TEST_CONFIG.items()
            if key != "tokenjuice_prompt_pruning_enabled"
        }
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        disposable = _contribution(
            contribution_id="disposable",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [disposable],
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None

    def test_omitted_classes_default_only_prunes_terminal_tool_output(self) -> None:
        cfg = {
            key: value
            for key, value in _STRUCTURED_PRUNING_TEST_CONFIG.items()
            if key != "tokenjuice_prompt_pruning_classes"
        }
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        custom = _contribution(
            contribution_id="custom",
            kind="message",
            provenance="conversation_history",
            class_="custom_class",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
        )
        result = _prune_structured_context(
            [custom],
            current_pressure_tokens=_pressure_over_threshold(cfg),
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is None or custom["id"] in {
            c["id"] for c in result["effective_contributions"]
        }

    def test_override_precedence_changes_outcome(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        default_min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(default_min_saved, int)
        override_min_saved = default_min_saved + 100
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        mid_sized = _contribution(
            contribution_id="mid-sized",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=default_min_saved + 50,
            prune_policy="hard_clear_allowed",
            content="x" * (default_min_saved + 50),
        )
        base_pressure = threshold + default_min_saved

        default_cfg = dict(cfg)
        default_cfg["tokenjuice_prompt_pruning_min_saved_tokens"] = default_min_saved
        default_result = _prune_structured_context(
            [mid_sized],
            current_pressure_tokens=base_pressure,
            threshold_tokens=threshold,
            **default_cfg,
        )
        assert default_result is not None
        assert mid_sized["id"] not in {
            c["id"] for c in default_result["effective_contributions"]
        }

        cfg["tokenjuice_prompt_pruning_min_saved_tokens"] = override_min_saved
        override_result = _prune_structured_context(
            [mid_sized],
            current_pressure_tokens=base_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert override_result is None or mid_sized["id"] in {
            c["id"] for c in override_result["effective_contributions"]
        }


def test_llm_request_fails_open_on_invalid_request_shape() -> None:
    # Given: a malformed request object.
    host = MiddlewareHost()
    register(host)
    middleware = host.middlewares["llm_request"]
    invalid_request: RequestObject = {"messages": "not-a-message-list"}

    # When: the llm_request middleware sees an invalid shape.
    result = middleware(invalid_request)

    # Then: invalid input fails open without forcing a rewrite.
    assert result is None or result == invalid_request
