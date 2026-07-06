from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tokenjuice_hermes.json_types import JsonValue

from tests.host_fixtures import (
    FailingMiddlewareHost,
    HookOnlyHost,
    MiddlewareHost,
    ToolHost,
    big_web_content,
    extract_hex_handle,
    web_result,
)
from tokenjuice_hermes.plugin import register


def _tool_request_middleware(host: MiddlewareHost) -> Callable[..., dict[str, JsonValue] | None]:
    mw = host.middlewares.get("tool_request")
    if mw is None:
        pytest.fail("tool_request middleware was not registered")
    return cast("Callable[..., dict[str, JsonValue] | None]", mw)


def test_middleware_host_registers_passref_middleware(tmp_path: Path) -> None:
    # Given: a host that exposes middleware registration and a writable store path.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}

    # When: the plugin registers itself.
    register(host)

    # Then: tool_request middleware is registered alongside the other surfaces.
    assert "tool_request" in host.middlewares
    assert "llm_request" in host.middlewares
    assert "rescuer_fetch" in host.tools
    assert host.hooks == ["transform_tool_result"]


def test_hook_only_host_does_not_register_passref_middleware() -> None:
    # Given: a host with no middleware surface at all.
    host = HookOnlyHost()

    # When: the plugin registers itself.
    register(host)

    # Then: the transform hook is registered, but no middleware is recorded.
    assert "tool_request" not in getattr(host, "middlewares", {})
    assert "llm_request" not in getattr(host, "middlewares", {})
    assert host.hooks == ["transform_tool_result"]


def test_failing_middleware_host_still_registers_hook_and_llm_middleware() -> None:
    # Given: a host whose register_middleware raises for every middleware name.
    host = FailingMiddlewareHost()

    # When: the plugin registers itself.
    register(host)

    # Then: the transform hook still registers and no middleware is recorded.
    assert host.hooks == ["transform_tool_result"]
    assert "tool_request" not in host.middlewares
    assert "llm_request" not in host.middlewares


def test_passref_default_off_prevents_expansion(tmp_path: Path) -> None:
    # Given: a tool host with passref left at its default (disabled).
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    transform = host.callbacks["transform_tool_result"]
    result = transform(
        web_result(big_web_content()),
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None
    mw = _tool_request_middleware(host)

    # When: an allowlisted tool receives a same-session handle.
    out = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: default-off keeps the args unchanged; no expansion occurs.
    assert out is None


def test_passref_enabled_expands_same_session_allowlisted_handle(tmp_path: Path) -> None:
    # Given: a tool host with passref explicitly enabled and allowlisted.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_passref_enabled": True,
        "tokenjuice_passref_allowed_tools": ["summarise"],
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    result = transform(
        web_result(big_web_content()),
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None
    mw = _tool_request_middleware(host)

    # When: an allowlisted tool receives a same-session handle.
    out = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: the handle expands to the full rescued content.
    assert out is not None
    args = out["args"]
    assert isinstance(args, dict)
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "web result line 0001" in x_value
    assert "web result line 1200" in x_value


def test_passref_enabled_denies_sink_tools_even_when_allowlisted(tmp_path: Path) -> None:
    # Given: passref enabled with sink tools explicitly in the allowlist.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_passref_enabled": True,
        "tokenjuice_passref_allowed_tools": ["shell"],
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    result = transform(
        web_result(big_web_content()),
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None
    mw = _tool_request_middleware(host)

    # When/Then: the sink denylist dominates the allowlist.
    out = mw(
        tool_name="shell",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )
    assert out is not None
    args = out["args"]
    assert isinstance(args, dict)
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "not eligible" in x_value
    assert "web result line" not in x_value


def test_passref_enabled_without_session_id_fails_closed(tmp_path: Path) -> None:
    # Given: passref enabled and allowlisted, but the host omits session_id.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_passref_enabled": True,
        "tokenjuice_passref_allowed_tools": ["summarise"],
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    result = transform(
        web_result(big_web_content()),
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None
    mw = _tool_request_middleware(host)

    # When: no session_id is forwarded to the middleware.
    out = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
    )

    # Then: the raw handle is replaced by a session-required marker.
    assert out is not None
    args = out["args"]
    assert isinstance(args, dict)
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "session ID" in x_value
    assert "web result line" not in x_value


def test_passref_registration_preserves_rescuer_fetch_registration(tmp_path: Path) -> None:
    # Given: a host that supports both tools and middleware.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}

    # When: the plugin registers passref alongside the rescue fetch tool.
    register(host)

    # Then: Todo 5's guarded rescuer_fetch registration is preserved.
    assert "rescuer_fetch" in host.tools
    assert "tool_request" in host.middlewares
    assert "llm_request" in host.middlewares
    assert host.hooks == ["transform_tool_result"]


def test_passref_enabled_denies_cross_session_handle(tmp_path: Path) -> None:
    # Given: a blob owned by one session and passref enabled for an allowlisted tool.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_passref_enabled": True,
        "tokenjuice_passref_allowed_tools": ["summarise"],
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    result = transform(
        web_result(big_web_content()),
        tool_name="web_search",
        session_id="owner",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    blob_id = extract_hex_handle(result or "")
    assert blob_id is not None
    mw = _tool_request_middleware(host)

    # When: a different session presents the handle.
    out = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="intruder",
    )

    # Then: the content is not exposed and a clear session marker is returned.
    assert out is not None
    args = out["args"]
    assert isinstance(args, dict)
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "not available in this session" in x_value
    assert "web result line" not in x_value


def test_passref_middleware_without_store_path_uses_default_and_is_registered() -> None:
    # Given: a host that does not configure a rescue store path.
    host = ToolHost()

    # When: the plugin registers itself.
    register(host)

    # Then: passref still registers; get_store will fall back to the default path
    # at runtime if expansion is ever requested.
    assert "tool_request" in host.middlewares


def test_passref_does_not_strip_or_expand_on_host_without_tool_request(
    tmp_path: Path,
) -> None:
    # Given: a hook-only host (no middleware surface).
    host = HookOnlyHost()
    register(host)

    # When: the plugin registers without tool_request support.
    # Then: existing transform behavior is preserved and no middleware is added.
    assert "tool_request" not in getattr(host, "middlewares", {})
    assert "llm_request" not in getattr(host, "middlewares", {})
    assert host.hooks == ["transform_tool_result"]
    transform = host.callbacks["transform_tool_result"]
    original = web_result(big_web_content())
    result = transform(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    assert result is None or extract_hex_handle(result) is None
