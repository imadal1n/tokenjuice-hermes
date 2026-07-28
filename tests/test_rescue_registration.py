from __future__ import annotations

from typing import TYPE_CHECKING, cast

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


def _function_definitions(host: HermesRegistryHost) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for definition in host.get_tool_definitions():
        function_value = definition["function"]
        assert isinstance(function_value, dict)
        function = cast("dict[str, object]", function_value)
        name_value = function["name"]
        assert isinstance(name_value, str)
        result[name_value] = function
    return result


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


def test_hermes_registry_tools_accept_runtime_context_kwargs(tmp_path: Path) -> None:
    # Given: registry-registered TokenJuice tools and a rescued blob.
    host = HermesRegistryHost(
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

    # When: Hermes dispatches the registered handlers with task-scoped kwargs.
    fetched = host.tools["rescuer_fetch"](
        {"id": blob_id, "mode": "full"},
        session_id="session-a",
        task_id="task-a",
        tool_call_id="call-a",
    )
    status = host.tools["tokenjuice_status"](
        {},
        session_id="session-a",
        task_id="task-a",
    )

    # Then: the runtime context is ignored without changing tool behavior.
    assert isinstance(fetched, str)
    assert "web result line 0001" in fetched
    assert isinstance(status, str)
    assert '"version"' in status


def test_hermes_registry_tool_definitions_expose_fetch_and_status_parameters() -> None:
    # Given: the real Hermes register_tool schema capture surface.
    host = HermesRegistryHost()
    register(host)

    # When: Hermes serializes registered schemas into model-visible functions.
    functions = _function_definitions(host)
    fetch_definition = functions["rescuer_fetch"]
    status_definition = functions["tokenjuice_status"]

    # Then: rescuer_fetch exposes the handle and fetch mode contract to the model.
    fetch_parameters = fetch_definition["parameters"]
    assert isinstance(fetch_parameters, dict)
    fetch_parameters = cast("dict[str, object]", fetch_parameters)
    assert fetch_parameters["type"] == "object"
    assert fetch_parameters["required"] == ["id", "mode"]
    fetch_properties = fetch_parameters["properties"]
    assert isinstance(fetch_properties, dict)
    fetch_properties = cast("dict[str, object]", fetch_properties)
    assert set(fetch_properties) == {"id", "mode", "start", "count", "pattern"}
    assert fetch_definition["description"]

    # And: tokenjuice_status exposes an empty object contract plus model-visible description.
    assert status_definition["description"]
    assert status_definition["parameters"] == {"type": "object", "properties": {}}


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
