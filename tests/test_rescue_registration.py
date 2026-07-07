from __future__ import annotations

from typing import TYPE_CHECKING

from tests.host_fixtures import (
    FailingToolHost,
    HermesRegistryHost,
    HookOnlyHost,
    ToolHost,
    big_web_content,
    extract_hex_handle,
    web_result,
)
from tokenjuice_hermes.plugin import register

if TYPE_CHECKING:
    from pathlib import Path


def test_register_adds_rescuer_fetch_on_tool_host() -> None:
    # Given: a host that exposes tool registration.
    host = ToolHost()

    # When: the plugin registers itself.
    register(host)

    # Then: the rescuer_fetch tool is registered alongside the hook and middlewares.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}
    assert "rescuer_fetch" in host.tools


def test_register_adds_rescuer_fetch_on_hermes_registry_host() -> None:
    # Given: a host with the real Hermes PluginContext.register_tool shape.
    host = HermesRegistryHost()

    # When: the plugin registers itself.
    register(host)

    # Then: fetch and status tools are registered and the transform hook is wrapped.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}
    assert "rescuer_fetch" in host.tools
    assert "tokenjuice_status" in host.tools


def test_tool_host_fetch_redeems_stored_blob(tmp_path: Path) -> None:
    # Given: an oversized web result and a tool-capable host.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    transform = host.callbacks["transform_tool_result"]
    original = web_result(big_web_content())

    # When: the registered transform hook rescues the result.
    result = transform(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )

    # Then: a model-visible handle is emitted and the fetch tool redeems it.
    assert result is not None
    blob_id = extract_hex_handle(result)
    assert blob_id is not None
    fetch = host.tools["rescuer_fetch"]
    fetched = fetch(args={"id": blob_id, "mode": "full"}, session_id="session-a")
    assert isinstance(fetched, str)
    assert "web result line 0001" in fetched
    assert "web result line 1200" in fetched


def test_hook_only_host_does_not_emit_rescue_handle() -> None:
    # Given: an oversized eligible result on a hook-only host.
    host = HookOnlyHost()
    register(host)
    transform = host.callbacks["transform_tool_result"]
    original = web_result(big_web_content())

    # When: the hook-only transform runs with a stable session ID.
    result = transform(
        original,
        tool_name="web_search",
        session_id="session-a",
    )

    # Then: no model-visible handle is emitted because fetch is unavailable.
    assert result is None or extract_hex_handle(result) is None


def test_tool_host_without_session_id_emits_no_handle(tmp_path: Path) -> None:
    # Given: an oversized eligible result without a stable session ID.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    transform = host.callbacks["transform_tool_result"]
    original = web_result(big_web_content())

    # When: the transform runs without a session ID.
    result = transform(
        original,
        tool_name="web_search",
        tokenjuice_rescue_store_path=str(tmp_path),
    )

    # Then: no opaque handle is exposed to the model.
    assert result is None or extract_hex_handle(result) is None


def test_failing_tool_host_still_registers_hook_and_middleware() -> None:
    # Given: a host whose register_tool raises.
    host = FailingToolHost()

    # When: the plugin registers itself.
    register(host)

    # Then: the transform hook and both middlewares are still registered.
    assert host.hooks == ["transform_tool_result"]
    assert set(host.middlewares) == {"llm_request", "tool_request"}
