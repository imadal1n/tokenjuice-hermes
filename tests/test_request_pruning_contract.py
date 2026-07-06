from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from tokenjuice_hermes.json_types import JsonValue, parse_json
from tokenjuice_hermes.plugin import register

RequestObject: TypeAlias = dict[str, JsonValue]
MiddlewareCallback: TypeAlias = Callable[..., RequestObject | str | None]


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def numbered_lines(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {number:02d}" for number in range(1, count + 1))


class HookOnlyHost:
    def __init__(self) -> None:
        self.hooks: list[str] = []

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        _ = callback
        self.hooks.append(name)


class MiddlewareHost(HookOnlyHost):
    def __init__(self) -> None:
        super().__init__()
        self.middlewares: dict[str, MiddlewareCallback] = {}

    def register_middleware(
        self,
        name: str,
        callback: MiddlewareCallback,
    ) -> None:
        self.middlewares[name] = callback


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

    # Then: the transformer hook and llm_request middleware are both registered.
    assert host.hooks == ["transform_tool_result"]
    assert list(host.middlewares) == ["llm_request"]


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
