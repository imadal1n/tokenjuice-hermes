from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.host_fixtures import ToolHost, big_web_content, extract_hex_handle, web_result
from tokenjuice_hermes.compaction import transform_tool_result
from tokenjuice_hermes.plugin import register
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def tmp_store(tmp_path: Path) -> BlobStore:
    return BlobStore({"store_path": str(tmp_path)})


def test_per_tool_threshold_applies_different_limits(tmp_path: Path) -> None:
    # Given: oversized web_search and browser_snapshot results with per-tool thresholds.
    web_content = "x" * 3_000
    browser_content = "y" * 3_000
    kwargs = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_rescue_fetch_available": True,
        "tokenjuice_rescue_tool_min_text_chars": "web_search=2000,browser_snapshot=5000",
    }

    # When: both tools are transformed with the same session.
    web_result_text = transform_tool_result(
        web_result(web_content),
        tool_name="web_search",
        session_id="session-a",
        **kwargs,
    )
    browser_result_text = transform_tool_result(
        json.dumps({"snapshot": browser_content}),
        tool_name="browser_snapshot",
        session_id="session-a",
        **kwargs,
    )

    # Then: web_search is rescued (3000 >= 2000) but browser_snapshot is not (3000 < 5000).
    assert web_result_text is not None
    assert extract_hex_handle(web_result_text) is not None
    assert browser_result_text is None


def test_per_tool_threshold_falls_back_to_global(tmp_path: Path) -> None:
    # Given: a web_search result and a per-tool threshold only for mcp_tool.
    content = "x" * 3_000
    kwargs = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_rescue_fetch_available": True,
        "tokenjuice_rescue_min_text_chars": 2_500,
        "tokenjuice_rescue_tool_min_text_chars": "mcp_tool=5000",
    }

    # When: web_search is transformed.
    result = transform_tool_result(
        web_result(content),
        tool_name="web_search",
        session_id="session-a",
        **kwargs,
    )

    # Then: web_search uses the global threshold (3000 >= 2500) and is rescued.
    assert result is not None
    assert extract_hex_handle(result) is not None


def test_malformed_per_tool_threshold_fails_open(tmp_path: Path) -> None:
    # Given: an oversized web_search result with a malformed per-tool threshold string.
    original = web_result(big_web_content())
    kwargs = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_rescue_fetch_available": True,
        "tokenjuice_rescue_tool_min_text_chars": "web_search=not_a_number",
    }

    # When: the plugin transforms the result.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        **kwargs,
    )

    # Then: malformed config fails open: no replacement and no dead handle.
    assert result is None or result == original
    if result is not None:
        assert extract_hex_handle(result) is None


def test_full_fetch_refusal_names_config_keys_and_safe_alternatives(
    tmp_path: Path,
) -> None:
    # Given: a rescued blob larger than the full-fetch cap with refusal enabled.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_rescue_full_fetch_max_chars": 100,
        "tokenjuice_rescue_refuse_full_fetch": True,
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    content = "line " * 1_200  # rescued (over default threshold) and over the full cap
    result = transform(
        web_result(content),
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    handle = extract_hex_handle(result or "")
    assert handle

    fetch = host.tools["rescuer_fetch"]
    refusal = fetch(args={"id": handle, "mode": "full"}, session_id="session-a")
    assert isinstance(refusal, str)

    # Then: the refusal names the config keys and gives exact safe alternatives.
    lowered = refusal.lower()
    assert "tokenjuice_rescue_full_fetch_max_chars" in lowered
    assert "tokenjuice_rescue_refuse_full_fetch" in lowered
    assert "mode='range'" in lowered or 'mode="range"' in lowered
    assert "start" in lowered
    assert "count" in lowered
    assert "mode='grep'" in lowered or 'mode="grep"' in lowered
    assert "pattern" in lowered


def test_per_tool_threshold_does_not_affect_exact_protected_outputs() -> None:
    # Given: a large read_file payload with per-tool thresholds that might rescue it.
    original = json.dumps({"content": big_web_content()})

    # When: read_file is transformed with aggressive per-tool thresholds.
    result = transform_tool_result(
        original,
        tool_name="read_file",
        session_id="session-a",
        tokenjuice_rescue_store_path="/ignored",
        tokenjuice_rescue_fetch_available=True,
        tokenjuice_rescue_tool_min_text_chars="read_file=1",
    )

    # Then: exact file reads remain protected and unchanged.
    assert result is None


@pytest.mark.parametrize(
    ("tool_name", "field_name"),
    [("terminal", "stdout"), ("execute_code", "output")],
)
def test_configured_terminal_tools_rescue_before_compaction(
    tmp_path: Path,
    tool_name: str,
    field_name: str,
) -> None:
    # Given: the agent's large terminal-like output is explicitly rescue-eligible at 2500 chars.
    content = "\n".join(f"agent-output-line-{line:03d}" for line in range(140))
    original = json.dumps({field_name: content})

    # When: the terminal-like result is transformed below the global rescue threshold.
    result = transform_tool_result(
        original,
        tool_name=tool_name,
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
        tokenjuice_rescue_min_text_chars=4_000,
        tokenjuice_rescue_tool_names="web_search,mcp_tool,browser_snapshot,terminal,execute_code",
        tokenjuice_rescue_tool_min_text_chars="terminal=2500,execute_code=2500",
    )

    # Then: rescue creates a fetchable handle and clearly marks the inline text as a preview.
    assert result is not None
    assert extract_hex_handle(result) is not None
    assert "tool result rescued" in result
    assert "Preview only" in result


def test_registered_transform_uses_persistent_rescue_config(tmp_path: Path) -> None:
    # Given: the host config persistently enables terminal rescue at 2500 chars.
    host = ToolHost()
    host.config = {
        "tokenjuice_rescue_store_path": str(tmp_path),
        "tokenjuice_rescue_min_text_chars": 4_000,
        "tokenjuice_rescue_tool_names": (
            "web_search,mcp_tool,browser_snapshot,terminal,execute_code"
        ),
        "tokenjuice_rescue_tool_min_text_chars": "terminal=2500,execute_code=2500",
        "tokenjuice_passref_enabled": False,
    }
    register(host)
    transform = host.callbacks["transform_tool_result"]
    content = "\n".join(f"agent-terminal-line-{line:03d}" for line in range(140))

    # When: Hermes invokes the registered hook without repeating config kwargs.
    result = transform(
        json.dumps({"stdout": content}),
        tool_name="terminal",
        session_id="session-a",
    )

    # Then: the persistent config is enough to produce a fetchable rescue handle.
    assert result is not None
    assert "tool result rescued" in result
    assert extract_hex_handle(result) is not None


def test_registered_transform_uses_hermes_config_file_without_ctx_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a Hermes config file has terminal rescue thresholds but ctx.config is absent.
    config_file = tmp_path / "config.yaml"
    _ = config_file.write_text(
        f"""plugins:
  entries:
    tokenjuice-hermes:
      config:
        tokenjuice_rescue_store_path: '{tmp_path}'
        tokenjuice_rescue_min_text_chars: 4000
        tokenjuice_rescue_tool_names: 'web_search,mcp_tool,browser_snapshot,terminal,execute_code'
        tokenjuice_rescue_tool_min_text_chars: 'terminal=2500,execute_code=2500'
        tokenjuice_passref_enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_file))

    host = ToolHost()
    # Real Hermes does not expose ctx.config for this plugin.
    del host.config
    register(host)
    transform = host.callbacks["transform_tool_result"]

    content = "\n".join(f"agent-terminal-line-{line:03d}" for line in range(140))
    assert 2_500 < len(content) < 4_000

    # When: Hermes invokes the registered hook without repeating config kwargs.
    result = transform(
        json.dumps({"stdout": content}),
        tool_name="terminal",
        session_id="session-a",
    )

    # Then: the Hermes config file alone is enough to produce a rescue handle.
    assert result is not None
    assert "tool result rescued" in result
    assert extract_hex_handle(result) is not None


def test_registered_transform_uses_top_level_hermes_config_without_ctx_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: live Hermes-style config stores tokenjuice keys at top level.
    config_file = tmp_path / "config.yaml"
    _ = config_file.write_text(
        f"""plugins:
  enabled:
    - tokenjuice-hermes

tokenjuice_rescue_store_path: '{tmp_path}'
tokenjuice_rescue_min_text_chars: 4000
tokenjuice_rescue_tool_names: web_search,mcp_tool,browser_snapshot,terminal,execute_code
tokenjuice_rescue_tool_min_text_chars: terminal=2500,execute_code=2500
tokenjuice_passref_enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_file))

    host = ToolHost()
    del host.config
    register(host)
    transform = host.callbacks["transform_tool_result"]

    content = "\n".join(f"agent-terminal-line-{line:03d}" for line in range(140))
    assert 2_500 < len(content) < 4_000

    # When: Hermes invokes the hook with no repeated config kwargs.
    result = transform(
        json.dumps({"stdout": content}),
        tool_name="terminal",
        session_id="session-a",
    )

    # Then: top-level persisted config is enough to produce a rescue handle.
    assert result is not None
    assert "tool result rescued" in result
    assert extract_hex_handle(result) is not None
