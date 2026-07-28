from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

from tests.host_fixtures import (
    HermesHost,
    HermesToollessHost,
    HookOnlyHost,
    MiddlewareHost,
    extract_hex_handle,
)
from tokenjuice_hermes.json_types import JsonValue, parse_json
from tokenjuice_hermes.plugin import register
from tokenjuice_hermes.rescue_store import BlobStore

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
_PRODUCTION_PRESSURE_TOKENS: int = 332_661
_PRODUCTION_THRESHOLD_TOKENS: int = 316_200
_PRODUCTION_TARGET_TOKENS: int = 237_150


def _prune_structured_context(
    contributions: Sequence[Contribution],
    *,
    current_pressure_tokens: int,
    threshold_tokens: int,
    **kwargs: JsonValue,
) -> StructuredPruningResult | None:
    from tokenjuice_hermes.structured_pruning import prune_structured_context  # noqa: PLC0415

    _ = kwargs.setdefault("now_epoch_ms", _DETERMINISTIC_EPOCH_MS)
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
    age_seconds: int = 7200,
    content: str = "",
    tool_calls: list[JsonValue] | None = None,
    provider_message: dict[str, JsonValue] | None = None,
    provider_tool: dict[str, JsonValue] | None = None,
    protected_reason: str = "",
    cache_scope: str = "body",
) -> Contribution:
    full_content = content
    if tool_calls is not None:
        full_content = json.dumps({"content": content, "tool_calls": tool_calls})
    contribution: Contribution = {
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
    if provider_message is not None:
        contribution["provider_message"] = provider_message
    if provider_tool is not None:
        contribution["provider_tool"] = provider_tool
    return contribution


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


def _spread_tokens(total: int, count: int) -> list[int]:
    base = total // count
    remainder = total % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _production_snapshot_contributions() -> list[Contribution]:
    contributions: list[Contribution] = []
    assistant_tokens = _spread_tokens(3_617, 115)
    diagnostic_tokens = _spread_tokens(83_900, 79)
    for index, tokens in enumerate(assistant_tokens[:79]):
        group_id = f"diagnostic-call-{index}"
        diagnostic_content = numbered_lines(
            f"diagnostic {index}",
            max(80, diagnostic_tokens[index] // 5),
        )
        contributions.append(
            _contribution(
                contribution_id=f"assistant-{index}",
                kind="message",
                provenance="conversation_history",
                class_="assistant_message",
                stability="turn_ephemeral",
                token_estimate=tokens,
                prune_policy="never",
                atomic_group_id=group_id,
                content="",
                provider_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": group_id,
                            "type": "function",
                            "function": {"name": "diagnostics", "arguments": "{}"},
                        }
                    ],
                },
            )
        )
        contributions.append(
            _contribution(
                contribution_id=f"diagnostic-{index}",
                kind="tool_interaction",
                provenance="conversation_history",
                class_="diagnostic",
                stability="turn_ephemeral",
                token_estimate=diagnostic_tokens[index],
                prune_policy="never",
                atomic_group_id=group_id,
                content=diagnostic_content,
                provider_message={
                    "role": "tool",
                    "tool_call_id": group_id,
                    "name": "diagnostics",
                    "content": diagnostic_content,
                },
            )
        )
    for index, tokens in enumerate(assistant_tokens[79:], start=79):
        read_index = index - 79
        group_id = f"read-call-{read_index}"
        provider_message: dict[str, JsonValue] | None = None
        atomic_group_id: str | None = None
        if read_index < 33:
            atomic_group_id = group_id
            provider_message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": group_id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        contributions.append(
            _contribution(
                contribution_id=f"assistant-{index}",
                kind="message",
                provenance="conversation_history",
                class_="assistant_message",
                stability="turn_ephemeral",
                token_estimate=tokens,
                prune_policy="never",
                atomic_group_id=atomic_group_id,
                content="" if provider_message is not None else f"assistant {index}",
                provider_message=provider_message,
            )
        )
    for index, tokens in enumerate(_spread_tokens(14_537, 19)):
        contributions.append(
            _contribution(
                contribution_id=f"user-{index}",
                kind="message",
                provenance="conversation_history",
                class_="user_message",
                stability="session_stable",
                token_estimate=tokens,
                prune_policy="never",
                content=f"user {index}",
            )
        )
    for index, tokens in enumerate(_spread_tokens(71_529, 33)):
        contributions.append(
            _contribution(
                contribution_id=f"read-file-{index}",
                kind="tool_interaction",
                provenance="conversation_history",
                class_="exact_file_read",
                stability="stable_prefix",
                token_estimate=tokens,
                prune_policy="never",
                atomic_group_id=f"read-call-{index}",
                content=f"exact file bytes {index}",
                provider_message={
                    "role": "tool",
                    "tool_call_id": f"read-call-{index}",
                    "name": "read_file",
                    "content": f"exact file bytes {index}",
                },
            )
        )
    for index, tokens in enumerate(_spread_tokens(2_965, 25)):
        group_id = f"terminal-call-{index}"
        contributions.append(
            _contribution(
                contribution_id=f"terminal-assistant-{index}",
                kind="message",
                provenance="conversation_history",
                class_="assistant_message",
                stability="turn_ephemeral",
                token_estimate=0,
                prune_policy="never",
                atomic_group_id=group_id,
                content="",
                provider_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": group_id,
                            "type": "function",
                            "function": {"name": "terminal", "arguments": "{}"},
                        }
                    ],
                },
            )
        )
        contributions.append(
            _contribution(
                contribution_id=f"terminal-{index}",
                kind="tool_interaction",
                provenance="conversation_history",
                class_="terminal_tool_output",
                stability="turn_ephemeral",
                token_estimate=tokens,
                prune_policy="hard_clear_allowed",
                atomic_group_id=group_id,
                content=f"terminal {index}",
                provider_message={
                    "role": "tool",
                    "tool_call_id": group_id,
                    "name": "terminal",
                    "content": f"terminal {index}",
                },
            )
        )
    return contributions


def _assert_no_orphan_tool_results(messages: list[dict[str, JsonValue]]) -> None:
    seen_tool_calls: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict):
                        tool_call_id = tool_call.get("id")
                        if isinstance(tool_call_id, str):
                            seen_tool_calls.add(tool_call_id)
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        assert isinstance(tool_call_id, str)
        assert tool_call_id in seen_tool_calls


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
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
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
            [old_disposable, young_prunable],
            current_pressure_tokens=current_pressure,
            threshold_tokens=threshold,
            **cfg,
        )
        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert young_prunable["id"] in kept_ids
        assert old_disposable["id"] not in kept_ids

    def test_cache_ttl_gates_young_turn_ephemeral_terminal_candidate(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        ttl_seconds = cfg["tokenjuice_prompt_pruning_cache_ttl_seconds"]
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        min_saved = cfg["tokenjuice_prompt_pruning_min_saved_tokens"]
        assert isinstance(ttl_seconds, int)
        assert isinstance(threshold, int)
        assert isinstance(min_saved, int)

        young_terminal = _contribution(
            contribution_id="young-terminal",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=min_saved,
            prune_policy="hard_clear_allowed",
            content="x" * min_saved,
            age_seconds=ttl_seconds // 2,
        )
        old_terminal = _contribution(
            contribution_id="old-terminal",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="y" * threshold,
            age_seconds=ttl_seconds * 2,
        )

        result = _prune_structured_context(
            [old_terminal, young_terminal],
            current_pressure_tokens=threshold + min_saved,
            threshold_tokens=threshold,
            **cfg,
        )

        assert result is not None
        kept_ids = {c["id"] for c in result["effective_contributions"]}
        assert young_terminal["id"] in kept_ids
        assert old_terminal["id"] not in kept_ids

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

    def test_hard_pressure_rescues_diagnostics_to_cross_threshold(self, tmp_path: Path) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        cfg["tokenjuice_rescue_store_path"] = str(tmp_path)
        cfg["tokenjuice_rescue_fetch_available"] = True
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        terminal = _contribution(
            contribution_id="terminal-small",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=1_000,
            prune_policy="hard_clear_allowed",
            content="terminal output",
        )
        diagnostic_content = numbered_lines("diagnostic traceback", 400)
        diagnostic = _contribution(
            contribution_id="diagnostic-large",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="diagnostic",
            stability="turn_ephemeral",
            token_estimate=4_000,
            prune_policy="hard_clear_allowed",
            content=diagnostic_content,
            provider_message={
                "role": "tool",
                "tool_call_id": "diagnostic-call",
                "name": "diagnostics",
                "content": diagnostic_content,
            },
        )
        read_file = _contribution(
            contribution_id="read-file",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="exact_file_read",
            stability="stable_prefix",
            token_estimate=4_000,
            prune_policy="never",
            content="exact file bytes",
            provider_message={
                "role": "tool",
                "tool_call_id": "read-call",
                "name": "read_file",
                "content": "exact file bytes",
            },
        )

        result = _prune_structured_context(
            [terminal, diagnostic, read_file],
            current_pressure_tokens=threshold + 2_500,
            threshold_tokens=threshold,
            session_id="session-a",
            **cfg,
        )

        assert result is not None
        messages = result["effective_messages"]
        diagnostic_messages = [
            message for message in messages if message.get("name") == "diagnostics"
        ]
        assert len(diagnostic_messages) == 1
        rescued_content = diagnostic_messages[0].get("content")
        assert isinstance(rescued_content, str)
        assert diagnostic_content not in rescued_content
        handle = extract_hex_handle(rescued_content)
        assert handle is not None
        assert (
            BlobStore({"store_path": str(tmp_path)}).fetch(
                handle,
                "full",
                session_id="session-a",
            )
            == diagnostic_content
        )
        read_messages = [message for message in messages if message.get("name") == "read_file"]
        assert len(read_messages) == 1
        assert read_messages[0].get("content") == "exact file bytes"
        assert result["accounting"]["saved_tokens"] >= 2_500

    def test_production_snapshot_rescues_live_diagnostics_below_threshold(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_threshold_tokens"] = _PRODUCTION_THRESHOLD_TOKENS
        cfg["tokenjuice_prompt_pruning_hard_target_ratio"] = 75
        cfg["tokenjuice_rescue_store_path"] = str(tmp_path)
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg), session_id="session-a")
        register(host)
        callback = host.callbacks["structured_context_prune"]
        contributions = _production_snapshot_contributions()
        original_contributions = deepcopy(contributions)

        result = callback(
            contributions,
            phase="pre_api",
            session_id="session-a",
            current_pressure_tokens=_PRODUCTION_PRESSURE_TOKENS,
            threshold_tokens=_PRODUCTION_THRESHOLD_TOKENS,
            target_tokens=_PRODUCTION_TARGET_TOKENS,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        assert isinstance(result, dict)
        pruned = cast("StructuredPruningResult", cast("object", result))
        accounting = pruned["accounting"]
        saved_tokens = accounting["saved_tokens"]
        assert saved_tokens < _PRODUCTION_PRESSURE_TOKENS - _PRODUCTION_TARGET_TOKENS
        assert _PRODUCTION_PRESSURE_TOKENS - saved_tokens < _PRODUCTION_THRESHOLD_TOKENS
        assert accounting["rescued_count"] > 0
        assert accounting["attempted_count"] > 0

        messages = pruned["effective_messages"]
        _assert_no_orphan_tool_results(messages)
        diagnostic_messages = [
            message for message in messages if message.get("name") == "diagnostics"
        ]
        assert diagnostic_messages
        rescued_content = next(
            content
            for content in (message.get("content") for message in diagnostic_messages)
            if isinstance(content, str) and extract_hex_handle(content) is not None
        )
        handle = extract_hex_handle(rescued_content)
        assert handle is not None
        fetched = BlobStore({"store_path": str(tmp_path)}).fetch(
            handle,
            "full",
            session_id="session-a",
        )
        assert any(
            contribution["content"] == fetched
            for contribution in contributions
            if contribution["class"] == "diagnostic"
        )

        read_file_messages = [message for message in messages if message.get("name") == "read_file"]
        assert len(read_file_messages) == 33
        assert read_file_messages[0].get("content") == "exact file bytes 0"
        assert contributions == original_contributions

        second_result = callback(
            pruned["effective_contributions"],
            phase="pre_api",
            session_id="session-a",
            current_pressure_tokens=_PRODUCTION_PRESSURE_TOKENS,
            threshold_tokens=_PRODUCTION_THRESHOLD_TOKENS,
            target_tokens=_PRODUCTION_TARGET_TOKENS,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )
        if second_result is not None:
            assert isinstance(second_result, dict)
            second = cast("StructuredPruningResult", cast("object", second_result))
            second_messages = second["effective_messages"]
            _assert_no_orphan_tool_results(second_messages)
            assert [
                message.get("content")
                for message in second_messages
                if message.get("name") == "diagnostics"
            ] == [message.get("content") for message in diagnostic_messages]

    def test_production_snapshot_fails_open_without_fetch_tool(self, tmp_path: Path) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_threshold_tokens"] = _PRODUCTION_THRESHOLD_TOKENS
        cfg["tokenjuice_rescue_store_path"] = str(tmp_path)
        host = HermesToollessHost(config=cast("dict[str, JsonValue]", cfg), session_id="session-a")
        register(host)
        callback = host.callbacks["structured_context_prune"]
        contributions = _production_snapshot_contributions()

        result = callback(
            contributions,
            phase="pre_api",
            session_id="session-a",
            current_pressure_tokens=_PRODUCTION_PRESSURE_TOKENS,
            threshold_tokens=_PRODUCTION_THRESHOLD_TOKENS,
            target_tokens=_PRODUCTION_TARGET_TOKENS,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        assert result is None
        assert contributions == _production_snapshot_contributions()

    def test_production_snapshot_fails_open_when_rescue_store_fails(self, tmp_path: Path) -> None:
        store_file = tmp_path / "not-a-directory"
        _ = store_file.write_text("occupied", encoding="utf-8")
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_threshold_tokens"] = _PRODUCTION_THRESHOLD_TOKENS
        cfg["tokenjuice_rescue_store_path"] = str(store_file)
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg), session_id="session-a")
        register(host)
        callback = host.callbacks["structured_context_prune"]
        contributions = _production_snapshot_contributions()

        result = callback(
            contributions,
            phase="pre_api",
            session_id="session-a",
            current_pressure_tokens=_PRODUCTION_PRESSURE_TOKENS,
            threshold_tokens=_PRODUCTION_THRESHOLD_TOKENS,
            target_tokens=_PRODUCTION_TARGET_TOKENS,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        assert result is None
        assert contributions == _production_snapshot_contributions()

    def test_rescued_atomic_diagnostic_preserves_assistant_tool_call(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_threshold_tokens"] = 10_000
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        cfg["tokenjuice_rescue_store_path"] = str(tmp_path)
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg), session_id="session-a")
        register(host)
        callback = host.callbacks["structured_context_prune"]
        diagnostic_content = numbered_lines("atomic diagnostic", 800)
        group_id = "call-1"
        assistant = _contribution(
            contribution_id="assistant-call",
            kind="message",
            provenance="conversation_history",
            class_="assistant_message",
            stability="turn_ephemeral",
            token_estimate=100,
            prune_policy="never",
            atomic_group_id=group_id,
            content="",
            provider_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": group_id,
                        "type": "function",
                        "function": {"name": "diagnostics", "arguments": "{}"},
                    }
                ],
            },
        )
        diagnostic = _contribution(
            contribution_id="diagnostic-result",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="diagnostic",
            stability="turn_ephemeral",
            token_estimate=3_000,
            prune_policy="never",
            atomic_group_id=group_id,
            content=diagnostic_content,
            provider_message={
                "role": "tool",
                "tool_call_id": group_id,
                "name": "diagnostics",
                "content": diagnostic_content,
            },
        )

        result = callback(
            [assistant, diagnostic],
            phase="pre_api",
            session_id="session-a",
            current_pressure_tokens=11_000,
            threshold_tokens=10_000,
            target_tokens=7_500,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        assert isinstance(result, dict)
        pruned = cast("StructuredPruningResult", cast("object", result))
        messages = pruned["effective_messages"]
        assert [message.get("role") for message in messages] == ["assistant", "tool"]
        _assert_no_orphan_tool_results(messages)
        rescued_content = messages[1].get("content")
        assert isinstance(rescued_content, str)
        handle = extract_hex_handle(rescued_content)
        assert handle is not None
        assert (
            BlobStore({"store_path": str(tmp_path)}).fetch(
                handle,
                "full",
                session_id="session-a",
            )
            == diagnostic_content
        )

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

    def test_multi_call_tool_interaction_is_pruned_atomically(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        group_id = "tool-batch:call-a,call-b"

        assistant = _contribution(
            contribution_id="assistant-batch",
            kind="message",
            provenance="conversation_history",
            class_="assistant_message",
            stability="turn_ephemeral",
            token_estimate=_small_token_estimate(cfg),
            prune_policy="never",
            atomic_group_id=group_id,
            content="",
            provider_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    },
                    {
                        "id": "call-b",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    },
                ],
            },
        )
        result_a = _contribution(
            contribution_id="result-a",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            atomic_group_id=group_id,
            content="a" * threshold,
            provider_message={
                "role": "tool",
                "tool_call_id": "call-a",
                "name": "terminal",
                "content": "a" * threshold,
            },
        )
        result_b = _contribution(
            contribution_id="result-b",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            atomic_group_id=group_id,
            content="b" * threshold,
            provider_message={
                "role": "tool",
                "tool_call_id": "call-b",
                "name": "terminal",
                "content": "b" * threshold,
            },
        )

        pruned = _prune_structured_context(
            [assistant, result_a, result_b],
            current_pressure_tokens=threshold * 2,
            threshold_tokens=threshold,
            **cfg,
        )

        assert pruned is not None
        kept_ids = {c["id"] for c in pruned["effective_contributions"]}
        assert {assistant["id"], result_a["id"], result_b["id"]}.isdisjoint(kept_ids)

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

    def test_soft_and_hard_target_ratio_overrides_change_outcome(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        cfg["tokenjuice_prompt_pruning_soft_target_ratio"] = 79
        cfg["tokenjuice_prompt_pruning_hard_target_ratio"] = 99
        cfg["tokenjuice_prompt_pruning_min_saved_tokens"] = 1
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        soft_candidate = _contribution(
            contribution_id="soft-candidate",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=200,
            prune_policy="soft_trim",
            content="s" * 200,
        )
        soft_result = _prune_structured_context(
            [soft_candidate],
            current_pressure_tokens=int(threshold * 0.80),
            threshold_tokens=threshold,
            **cfg,
        )
        assert soft_result is not None
        assert soft_candidate["id"] not in {c["id"] for c in soft_result["effective_contributions"]}

        hard_candidate = _contribution(
            contribution_id="hard-candidate",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=50,
            prune_policy="hard_clear_allowed",
            content="h" * 50,
        )
        hard_result = _prune_structured_context(
            [hard_candidate],
            current_pressure_tokens=threshold,
            threshold_tokens=threshold,
            **cfg,
        )
        assert hard_result is not None
        assert hard_candidate["id"] not in {c["id"] for c in hard_result["effective_contributions"]}

    def test_target_ratio_backfills_omitted_phase_target_ratios(self) -> None:
        cfg = {
            key: value
            for key, value in _STRUCTURED_PRUNING_TEST_CONFIG.items()
            if key
            not in {
                "tokenjuice_prompt_pruning_soft_target_ratio",
                "tokenjuice_prompt_pruning_hard_target_ratio",
            }
        }
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        cfg["tokenjuice_prompt_pruning_target_ratio"] = 90
        cfg["tokenjuice_prompt_pruning_min_saved_tokens"] = 1
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        candidate = _contribution(
            contribution_id="fallback-target-candidate",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=1200,
            prune_policy="hard_clear_allowed",
            content="h" * 1200,
        )
        result = _prune_structured_context(
            [candidate],
            current_pressure_tokens=threshold,
            threshold_tokens=threshold,
            **cfg,
        )

        assert result is not None
        assert candidate["id"] not in {c["id"] for c in result["effective_contributions"]}

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
        assert result is None or other["id"] in {c["id"] for c in result["effective_contributions"]}

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
        assert mid_sized["id"] not in {c["id"] for c in default_result["effective_contributions"]}

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


class TestStructuredPruningHermesSeam:
    """Contract tests that mirror the Hermes agent/context_pruning seam."""

    def test_register_adds_structured_pruning_hook_when_enabled(self) -> None:
        # Given: a host with structured pruning enabled in its config.
        host = HermesHost(config=cast("dict[str, JsonValue]", _STRUCTURED_PRUNING_TEST_CONFIG))

        # When: the plugin registers itself.
        register(host)

        # Then: the structured_context_prune hook is registered.
        assert "structured_context_prune" in host.hooks

    def test_explicit_target_tokens_prune_without_current_pressure_tokens(self) -> None:
        # Given: hook context fields with explicit pressure and target token bounds.
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg))
        register(host)
        callback = host.callbacks["structured_context_prune"]
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        trigger_ratio = cfg["tokenjuice_prompt_pruning_trigger_ratio"]
        assert isinstance(trigger_ratio, int)
        target_ratio = cfg["tokenjuice_prompt_pruning_target_ratio"]
        assert isinstance(target_ratio, int)

        system_part = _contribution(
            contribution_id="system",
            kind="system_part",
            provenance="system_prompt",
            class_="system_instruction",
            stability="stable_prefix",
            token_estimate=100,
            prune_policy="never",
            content="system instruction",
        )
        user = _contribution(
            contribution_id="user",
            kind="message",
            provenance="conversation_history",
            class_="user_message",
            stability="session_stable",
            token_estimate=50,
            prune_policy="never",
            content="user question",
        )
        old_terminal = _contribution(
            contribution_id="old-terminal",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="old tool output",
            age_seconds=60,
        )
        terminal_tool = json.dumps(
            {
                "type": "function",
                "function": {"name": "terminal", "description": "Run shell commands"},
            }
        )
        tool_schema = _contribution(
            contribution_id="tool-schema-0",
            kind="tool_schema",
            provenance="tool_schema",
            class_="tool_schema",
            stability="stable_prefix",
            token_estimate=100,
            prune_policy="never",
            content=terminal_tool,
        )
        contributions = [system_part, user, old_terminal, tool_schema]
        tool_schema_tokens = 100

        # When: the hook is invoked with explicit threshold/trigger/target tokens.
        raw_result = callback(
            contributions,
            phase="pre_api",
            session_id="session-a",
            turn_id="turn-1",
            api_request_id="",
            task_id="task-1",
            model="test-model",
            provider="test-provider",
            api_mode="chat",
            compression_enabled=True,
            context_length=0,
            threshold_tokens=threshold,
            trigger_tokens=int(threshold * trigger_ratio / 100),
            target_tokens=int(threshold * target_ratio / 100),
            tool_schema_tokens=tool_schema_tokens,
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        # Then: a valid result is returned, effective_messages are provider-shaped,
        # the old terminal output is pruned, and the system/user/tool schema remain.
        assert isinstance(raw_result, dict)
        result = cast("StructuredPruningResult", cast("object", raw_result))
        effective_messages = result["effective_messages"]
        assert len(effective_messages) > 0
        message_contents: set[str] = set()
        for message in effective_messages:
            assert isinstance(message, dict)
            assert "role" in message
            assert "content" in message
            assert "id" not in message
            assert "class" not in message
            content = message.get("content")
            assert isinstance(content, str)
            message_contents.add(content)
        assert "old tool output" not in message_contents
        assert "system instruction" in message_contents
        assert "user question" in message_contents
        assert result["effective_system_prompt"] == ""

        effective_tools = result["effective_tools"]
        assert effective_tools == [
            {
                "type": "function",
                "function": {"name": "terminal", "description": "Run shell commands"},
            }
        ]

    def test_hermes_seam_preserves_provider_messages_exactly(self) -> None:
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        cfg["tokenjuice_prompt_pruning_protect_recent_tool_interactions"] = 0
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg))
        register(host)
        callback = host.callbacks["structured_context_prune"]
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)
        system_message: dict[str, JsonValue] = {"role": "system", "content": "SYSTEM"}
        multimodal_user: dict[str, JsonValue] = {
            "role": "user",
            "content": [
                {"type": "text", "text": "see image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
            "metadata": {"kept": True},
        }
        terminal = _contribution(
            contribution_id="old-terminal",
            kind="tool_interaction",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="turn_ephemeral",
            token_estimate=threshold,
            prune_policy="hard_clear_allowed",
            content="x" * threshold,
            provider_message={
                "role": "tool",
                "tool_call_id": "call-old",
                "name": "terminal",
                "content": "x" * threshold,
            },
            age_seconds=7200,
        )
        contributions = [
            _contribution(
                contribution_id="system-message",
                kind="message",
                provenance="conversation_history",
                class_="unknown",
                stability="turn_ephemeral",
                token_estimate=10,
                prune_policy="never",
                content="SYSTEM",
                provider_message=system_message,
            ),
            _contribution(
                contribution_id="multimodal-user",
                kind="message",
                provenance="conversation_history",
                class_="user_message",
                stability="session_stable",
                token_estimate=10,
                prune_policy="never",
                content="multimodal",
                provider_message=multimodal_user,
            ),
            terminal,
        ]

        raw_result = callback(
            contributions,
            phase="pre_api",
            threshold_tokens=threshold,
            trigger_tokens=int(threshold * 0.8),
            now_epoch_ms=_DETERMINISTIC_EPOCH_MS,
        )

        assert isinstance(raw_result, dict)
        result = cast("StructuredPruningResult", cast("object", raw_result))
        assert result["effective_messages"] == [system_message, multimodal_user]
        assert result["effective_system_prompt"] == ""

    def test_hermes_seam_returns_none_when_pressure_cannot_be_derived(self) -> None:
        # Given: no current_pressure_tokens and no contribution estimates from which
        # to derive true request pressure.
        host = HermesHost(config=cast("dict[str, JsonValue]", _STRUCTURED_PRUNING_TEST_CONFIG))
        register(host)
        callback = host.callbacks["structured_context_prune"]

        empty = _contribution(
            contribution_id="empty",
            kind="system_part",
            provenance="system_prompt",
            class_="system_instruction",
            stability="stable_prefix",
            token_estimate=0,
            prune_policy="never",
            content="",
        )

        # When: the seam context has threshold but no usable pressure signal.
        result = callback(
            [empty],
            phase="pre_api",
            threshold_tokens=None,
        )

        # Then: fail-open by returning None.
        assert result is None

    def test_hermes_seam_ignores_context_length_as_pressure(self) -> None:
        # Given: a huge Hermes context_length but tiny real contribution estimates.
        cfg = dict(_STRUCTURED_PRUNING_TEST_CONFIG)
        cfg["tokenjuice_prompt_pruning_protect_recent_messages"] = 0
        host = HermesHost(config=cast("dict[str, JsonValue]", cfg))
        register(host)
        callback = host.callbacks["structured_context_prune"]
        threshold = cfg["tokenjuice_prompt_pruning_threshold_tokens"]
        assert isinstance(threshold, int)

        small_terminal = _contribution(
            contribution_id="small-terminal",
            kind="message",
            provenance="conversation_history",
            class_="terminal_tool_output",
            stability="volatile",
            token_estimate=10,
            prune_policy="hard_clear_allowed",
            content="tiny output",
        )

        # When: the seam passes a large context_length (model capacity), not pressure.
        result = callback(
            [small_terminal],
            phase="pre_api",
            context_length=1_000_000,
            threshold_tokens=threshold,
            trigger_tokens=int(threshold * 0.8),
            target_tokens=int(threshold * 0.75),
            tool_schema_tokens=0,
        )

        # Then: fail-open because true request pressure is tiny; context_length is not pressure.
        assert result is None
