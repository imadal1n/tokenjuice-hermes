from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypeAlias, cast

from tests.host_fixtures import (
    HermesHost,
    HermesMiddlewarelessHost,
    HermesToollessHost,
    big_web_content,
    extract_hex_handle,
    web_result,
)
from tokenjuice_hermes.json_types import JsonValue, parse_json
from tokenjuice_hermes.plugin import register

if TYPE_CHECKING:
    from pathlib import Path

RequestObject: TypeAlias = dict[str, JsonValue]


def _json_object(text: str) -> dict[str, object]:
    value = cast("object", json.loads(text))
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


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


def test_hermes_host_registers_all_surfaces(tmp_path: Path) -> None:
    # Given: a realistic Hermes-like host with a temp store configured.
    host = HermesHost(config={"tokenjuice_rescue_store_path": str(tmp_path)})

    # When: the plugin registers itself.
    register(host)

    # Then: all expected hooks, middlewares, and tools are recorded.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}
    assert "rescuer_fetch" in host.tools
    assert "tokenjuice_status" in host.tools


def test_hermes_host_transform_and_fetch_redeem_blob(tmp_path: Path) -> None:
    # Given: a Hermes host with a session and an oversized web result.
    host = HermesHost(
        config={"tokenjuice_rescue_store_path": str(tmp_path)},
        session_id="session-a",
    )
    register(host)

    # When: the registered transform hook rescues the result.
    result = host.invoke_hook(
        "transform_tool_result",
        web_result(big_web_content()),
        tool_name="web_search",
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None

    # Then: the fetch tool redeems the handle for the full content.
    fetched = host.invoke_tool(
        "rescuer_fetch",
        {"id": blob_id, "mode": "full"},
    )
    assert isinstance(fetched, str)
    assert "web result line 0001" in fetched
    assert "web result line 1200" in fetched


def test_hermes_host_llm_request_prunes_under_pressure() -> None:
    # Given: a request with enough history pressure to trigger pruning.
    host = HermesHost()
    register(host)
    old_terminal = numbered_lines("old stdout", 360)
    request = request_payload(
        old_tool_result=old_terminal,
        recent_tool_result=numbered_lines("recent stdout", 360),
        read_file_result=numbered_lines("file content", 50),
    )

    # When: the llm_request middleware is invoked with pressure kwargs.
    rewritten = host.invoke_middleware(
        "llm_request",
        request,
        request_pressure_tokens=10_000,
        threshold_tokens=10_000,
    )

    # Then: the request is rewritten and old terminal history is removed.
    assert rewritten is not None
    normalized = _normalize_request(rewritten)
    terminal_contents = _message_contents(normalized, name="terminal")
    assert old_terminal not in terminal_contents


def test_hermes_host_tool_request_passref_expands_when_enabled(
    tmp_path: Path,
) -> None:
    # Given: passref explicitly enabled for an allowlisted tool.
    host = HermesHost(
        config={
            "tokenjuice_rescue_store_path": str(tmp_path),
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
        session_id="session-a",
    )
    register(host)

    # When: a web_search result is rescued and then passed to an allowlisted tool.
    result = host.invoke_hook(
        "transform_tool_result",
        web_result(big_web_content()),
        tool_name="web_search",
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None

    expanded = host.invoke_middleware(
        "tool_request",
        tool_name="summarise",
        args={"query": f"summarise tla:{blob_id}"},
    )

    # Then: the handle expands to the rescued content in the same session.
    assert expanded is not None
    assert isinstance(expanded, dict)
    args = cast("dict[str, JsonValue]", expanded["args"])
    query = cast("str", args["query"])
    assert "web result line 0001" in query
    assert "web result line 1200" in query


def test_hermes_host_passref_default_off_leaves_args_untouched(
    tmp_path: Path,
) -> None:
    # Given: a host with passref left at its default disabled state.
    host = HermesHost(
        config={"tokenjuice_rescue_store_path": str(tmp_path)},
        session_id="session-a",
    )
    register(host)

    result = host.invoke_hook(
        "transform_tool_result",
        web_result(big_web_content()),
        tool_name="web_search",
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None

    # When: an allowlisted-looking tool receives the handle with passref off.
    untouched = host.invoke_middleware(
        "tool_request",
        tool_name="summarise",
        args={"query": f"summarise tla:{blob_id}"},
    )

    # Then: the middleware returns None and does not expand or rewrite args.
    assert untouched is None


def test_hermes_host_status_tool_reports_safe_aggregates(tmp_path: Path) -> None:
    # Given: a host that has rescued one blob.
    host = HermesHost(
        config={"tokenjuice_rescue_store_path": str(tmp_path)},
        session_id="session-a",
    )
    register(host)
    _ = host.invoke_hook(
        "transform_tool_result",
        web_result(big_web_content()),
        tool_name="web_search",
    )

    # When: the status tool is invoked.
    status_text = host.invoke_tool("tokenjuice_status", {})

    # Then: the result contains only safe, aggregate fields.
    assert isinstance(status_text, str)
    parsed = _json_object(status_text)
    assert parsed.get("passref_enabled") is False
    assert "compaction_count" in parsed
    assert "rescue_count" in parsed
    assert "store" in parsed


def test_hermes_host_missing_tool_surface_degrades_without_dead_handle(
    tmp_path: Path,
) -> None:
    # Given: a Hermes-like host without a tool registration surface.
    host = HermesToollessHost(
        config={"tokenjuice_rescue_store_path": str(tmp_path)},
        session_id="session-a",
    )
    register(host)

    # When: an eligible oversized result is transformed.
    result = host.invoke_hook(
        "transform_tool_result",
        web_result(big_web_content()),
        tool_name="web_search",
    )

    # Then: no model-visible handle is emitted because fetch is unavailable.
    assert result is None or extract_hex_handle(result) is None


def test_hermes_host_missing_middleware_surface_keeps_hook() -> None:
    # Given: a Hermes-like host without a middleware registration surface.
    host = HermesMiddlewarelessHost()
    register(host)

    # When/Then: the transform hook still registers and no middleware is added.
    assert host.hooks == ["transform_tool_result"]
    assert "llm_request" not in host.middlewares
    assert "tool_request" not in host.middlewares


def test_hermes_host_tool_registration_exception_still_registers_hook_and_middleware() -> None:
    # Given: a host whose register_tool raises.
    host = HermesHost(tool_registration_error=True)
    register(host)

    # When/Then: hook and middlewares still register, but no tools do.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}
    assert not host.tools


def test_hermes_host_middleware_registration_exception_still_registers_hook_and_tools() -> None:
    # Given: a host whose register_middleware raises.
    host = HermesHost(middleware_registration_error=True)
    register(host)

    # When/Then: the transform hook and tool surface still register.
    assert host.hooks == ["transform_tool_result"]
    assert not host.middlewares
    assert "rescuer_fetch" in host.tools
    assert "tokenjuice_status" in host.tools


def _normalize_request(result: RequestObject | str | None) -> RequestObject:
    assert isinstance(result, str | dict)
    if isinstance(result, str):
        parsed = parse_json(result)
        assert isinstance(parsed, dict)
        return parsed
    replacement = result.get("request")
    assert isinstance(replacement, dict)
    return replacement


def _message_contents(request: RequestObject, *, name: str) -> list[str]:
    messages = request.get("messages", request.get("input"))
    assert isinstance(messages, list)
    contents: list[str] = []
    for message in messages:
        assert isinstance(message, dict)
        if message.get("name") != name:
            continue
        content = message.get("content")
        assert isinstance(content, str)
        contents.append(content)
    return contents
