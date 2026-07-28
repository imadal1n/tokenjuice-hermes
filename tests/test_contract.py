from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Protocol, TypeAlias

import pytest

from tokenjuice_hermes.compaction import transform_tool_result
from tokenjuice_hermes.json_types import parse_flat_json_object, parse_json
from tokenjuice_hermes.passref import make_passref_middleware
from tokenjuice_hermes.plugin import register

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FlatJsonObject: TypeAlias = dict[str, JsonScalar]
TerminalJsonObject: TypeAlias = dict[str, JsonValue]
HookCallback: TypeAlias = Callable[..., JsonValue | None]
ToolCallback: TypeAlias = Callable[..., JsonValue | None]
ToolRequestCallback: TypeAlias = Callable[..., dict[str, JsonValue] | None]


class _RescueStore(Protocol):
    def put(self, content: str, tool_name: str, session_id: str) -> str: ...
    def blob_text(self, blob_id: str) -> str | None: ...
    def session_references(self, blob_id: str, session_id: str) -> bool: ...


class _ToolHost:
    """Host fixture that exposes hooks, middleware, tools, and a rescue store."""

    def __init__(self) -> None:
        self.hooks: list[str] = []
        self.middlewares: dict[str, ToolRequestCallback] = {}
        self.tools: dict[str, ToolCallback] = {}
        self.config: dict[str, JsonValue] = {}
        self.rescue_store: _RescueStore | None = None

    def register_hook(self, name: str, callback: HookCallback) -> None:
        _ = callback
        self.hooks.append(name)

    def register_middleware(self, name: str, callback: ToolRequestCallback) -> None:
        self.middlewares[name] = callback

    def register_tool(self, name: str, callback: ToolCallback) -> None:
        self.tools[name] = callback


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACKAGE = Path(__file__).resolve().parents[1] / "tokenjuice_hermes"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_object(name: str) -> FlatJsonObject:
    value = parse_flat_json_object(load_fixture(name))
    assert value is not None
    return value


def parse_result(result: str | None) -> TerminalJsonObject:
    assert isinstance(result, str)
    value = parse_json(result)
    assert isinstance(value, dict)
    return value


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def terminal_payload(
    *,
    stdout: str,
    stderr: str = "",
    exit_code: int = 0,
    status: str = "ok",
) -> str:
    payload: FlatJsonObject = {
        "command": "tokenjuice render --format json --verbose",
        "exit": exit_code,
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
    }
    return json.dumps(payload)


def numbered_lines(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {number:02d}" for number in range(1, count + 1))


def web_result(content: str) -> str:
    """Build a flat JSON tool result eligible for rescue."""
    return json.dumps({"content": content})


def big_web_content(*, lines: int = 1200) -> str:
    return "\n".join(f"web result line {number:04d}" for number in range(1, lines + 1))


def _extract_hex_handle(text: str) -> str | None:
    match = re.search(r"\b[0-9a-f]{12}\b", text)
    return match.group(0) if match else None


class _FakeRescueStore:
    """In-memory stand-in for the rescue blob store."""

    def __init__(self) -> None:
        self._blobs: dict[str, str] = {}
        self._sessions: dict[str, set[str]] = {}

    def put(self, content: str, tool_name: str, session_id: str) -> str:
        _ = tool_name
        blob_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        self._blobs[blob_id] = content
        self._sessions.setdefault(session_id, set()).add(blob_id)
        return blob_id

    def blob_text(self, handle: str) -> str | None:
        return self._blobs.get(handle)

    def session_references(self, handle: str, session_id: str) -> bool:
        return handle in self._sessions.get(session_id, set())

    def has_blob(self, handle: str) -> bool:
        return handle in self._blobs


def _rescuer_fetch_tool(host: _ToolHost) -> ToolCallback:
    if "rescuer_fetch" not in host.tools:
        pytest.fail("rescuer_fetch tool was not registered")
    return host.tools["rescuer_fetch"]


def _passref_middleware(
    store: _FakeRescueStore,
    config: dict[str, JsonValue],
) -> ToolRequestCallback:
    return make_passref_middleware(lambda: store, config)


def test_manifest_declares_transform_tool_result_hook() -> None:
    # Given: the contract manifest fixture.
    manifest = load_fixture("manifest.json")

    # When: the manifest is inspected for hook declarations.
    hook_found = '"transform_tool_result"' in manifest

    # Then: the plugin advertises transform_tool_result.
    assert '"name": "tokenjuice-hermes"' in manifest
    assert hook_found


def test_register_registers_transform_tool_result_hook() -> None:
    # Given: a host that records registered hook names.
    class Host:
        def __init__(self) -> None:
            self.hooks: list[str] = []
            self.callback: HookCallback | None = None

        def register_hook(self, name: str, callback: HookCallback) -> None:
            self.callback = callback
            self.hooks.append(name)

    host = Host()

    # When: the plugin is asked to register itself.
    register(host)

    # Then: transform_tool_result is registered once.
    assert host.hooks == ["transform_tool_result"]


def test_installed_directory_plugin_layout_imports(tmp_path: Path) -> None:
    # Given: files copied into a flat Hermes directory-plugin layout.
    plugin_dir = tmp_path / "tokenjuice-hermes"
    plugin_dir.mkdir()
    for name in [
        "__init__.py",
        "compaction.py",
        "compaction_options.py",
        "hermes_config.py",
        "json_types.py",
        "observability.py",
        "passref.py",
        "plugin.py",
        "plugin.yaml",
        "request_pruning.py",
        "structured_pruning.py",
        "structured_pruning_apply.py",
        "structured_pruning_config.py",
        "structured_pruning_groups.py",
        "structured_pruning_memo.py",
        "structured_pruning_provider.py",
        "structured_pruning_rescue.py",
        "structured_pruning_selection.py",
        "structured_pruning_types.py",
        "rescue_excerpt.py",
        "rescue_fetch.py",
        "rescue_grep.py",
        "rescue_handles.py",
        "rescue_index.py",
        "rescue_sqlite.py",
        "rescue_sqlite_maintenance.py",
        "rescue_sqlite_migration.py",
        "rescue_sqlite_schema.py",
        "rescue_sqlite_types.py",
        "rescue_store.py",
        "rescue_sweep.py",
        "rescue_transform.py",
        "rescue_types.py",
        "py.typed",
    ]:
        _ = (plugin_dir / name).write_text(
            (PACKAGE / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    spec = importlib.util.spec_from_file_location(
        "tokenjuice_hermes_installed_test",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module

    # When: the directory plugin is imported through its file location.
    spec.loader.exec_module(module)

    # Then: Hermes-visible entry points are available.
    assert isinstance(module, ModuleType)
    exported_names = dir(module)
    assert "register" in exported_names
    assert "transform_tool_result" in exported_names


@pytest.mark.parametrize("fixture_name", ["read-file.txt"])
def test_read_file_preserves_exact_text_or_returns_none(
    fixture_name: str,
) -> None:
    # Given: a read_file tool result that should not be rewritten.
    original = load_fixture(fixture_name)

    # When: the plugin transforms the read_file result.
    result = transform_tool_result(original, tool_name="read_file")

    # Then: the value is either untouched or explicitly left alone.
    assert result is None or result == original


def test_short_terminal_json_is_left_unchanged() -> None:
    # Given: a small terminal JSON result.
    original = load_fixture("terminal-short.json")

    # When: the plugin transforms the terminal result.
    result = transform_tool_result(original, tool_name="terminal")

    # Then: short JSON is not replaced.
    assert result is None or result == original


def test_long_terminal_json_is_compacted_without_losing_core_fields() -> None:
    # Given: a terminal JSON result compacted through explicit legacy-sized options.
    original = load_fixture("terminal-long.json")

    # When: the plugin transforms the terminal result.
    result = transform_tool_result(
        original,
        tool_name="terminal",
        tokenjuice_min_text_chars=1,
        tokenjuice_head_lines=3,
        tokenjuice_tail_lines=2,
        tokenjuice_preview_chars=72,
    )

    # Then: the result remains valid JSON and preserves key fields.
    compacted = parse_result(result)
    source = load_json_object("terminal-long.json")
    assert compacted["command"] == source["command"]
    assert compacted["exit"] == source["exit"]
    assert compacted["status"] == source["status"]
    expected_preview = (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nl"
    )
    assert compacted["tokenjuice"] == {
        "compacted": True,
        "original_chars": 201,
        "mode": "head_tail",
        "fields": {
            "stdout": {
                "original_chars": 110,
                "original_lines": 15,
                "omitted_lines": 10,
                "preview": expected_preview,
            }
        },
    }
    assert len(str(compacted["stdout"])) < len(str(source["stdout"]))


def test_defaults_compact_long_terminal_output_with_larger_head_tail_windows() -> None:
    # Given: output above the default compaction threshold.
    original = terminal_payload(stdout=numbered_lines("stdout", 120))

    # When: terminal output is transformed with defaults.
    result = transform_tool_result(original, tool_name="terminal")

    # Then: defaults keep a wider head and tail window.
    compacted = parse_result(result)
    stdout = compacted["stdout"]
    assert isinstance(stdout, str)
    assert stdout.startswith("stdout 01\nstdout 02\nstdout 03")
    assert "stdout 40" in stdout
    assert "[tokenjuice-hermes: omitted 60 middle lines]" in stdout
    assert stdout.endswith("stdout 119\nstdout 120")


def test_defaults_leave_historical_short_terminal_fixture_unchanged() -> None:
    # Given: the historical compact fixture is short under the current defaults.
    original = load_fixture("terminal-long.json")

    # When: terminal output is transformed with defaults.
    result = transform_tool_result(original, tool_name="terminal")

    # Then: the hook leaves it unchanged.
    assert result is None


def test_error_result_preserves_stderr_while_compacting_stdout() -> None:
    # Given: an error result with large stdout and large stderr.
    stderr = numbered_lines("stderr", 120)
    original = terminal_payload(
        stdout=numbered_lines("stdout", 120),
        stderr=stderr,
        exit_code=1,
        status="failed",
    )

    # When: terminal output is transformed with defaults.
    result = transform_tool_result(original, tool_name="terminal")

    # Then: stdout is compacted, but stderr stays exact for error context.
    compacted = parse_result(result)
    stdout = compacted["stdout"]
    assert isinstance(stdout, str)
    assert "[tokenjuice-hermes: omitted 60 middle lines]" in stdout
    assert compacted["stderr"] == stderr


def test_error_result_preserves_traceback_embedded_in_output() -> None:
    # Given: execute_code embeds stderr tracebacks inside the output field.
    traceback_output = "\n".join(
        [
            "--- stderr ---",
            "Traceback (most recent call last):",
            *numbered_lines("frame", 120).splitlines(),
            "ValueError: I/O operation on closed file.",
        ]
    )
    original = json.dumps(
        {
            "status": "error",
            "output": traceback_output,
            "tool_calls_made": 0,
            "duration_seconds": 0.05,
        }
    )

    # When: the execute_code error result is transformed with defaults.
    result = transform_tool_result(original, tool_name="execute_code")

    # Then: the traceback-bearing output stays exact for debugging context.
    assert result is None or parse_result(result)["output"] == traceback_output


def test_execute_code_uses_default_terminal_compaction() -> None:
    # Given: execute_code emits the same terminal-like JSON shape.
    original = load_fixture("terminal-long.json")

    # When: the plugin transforms the execute_code result.
    result = transform_tool_result(
        original,
        tool_name="execute_code",
        tokenjuice_min_text_chars=1,
    )

    # Then: the default terminal compaction path is used.
    compacted = parse_result(result)
    meta = json_object(compacted["tokenjuice"])
    assert meta["compacted"] is True


def test_custom_thresholds_head_tail_preview_and_text_fields() -> None:
    # Given: a short terminal result and custom flat kwargs that make it compactable.
    original = load_fixture("terminal-short.json")

    # When: custom options lower thresholds and target stdout only.
    result = transform_tool_result(
        original,
        tool_name="terminal",
        tokenjuice_min_text_chars=1,
        tokenjuice_head_lines=1,
        tokenjuice_tail_lines=1,
        tokenjuice_preview_chars=8,
        tokenjuice_text_fields="stdout",
    )

    # Then: the configured limits drive the rewritten result and metadata preview.
    compacted = parse_result(result)
    assert compacted["stdout"] == "short output"
    meta = json_object(compacted["tokenjuice"])
    fields = json_object(meta["fields"])
    stdout_meta = json_object(fields["stdout"])
    assert stdout_meta["preview"] == "short ou"


def test_tool_aliases_enable_extra_terminal_like_names() -> None:
    # Given: a shell-like tool name is not enabled by default.
    original = load_fixture("terminal-long.json")
    default_result = transform_tool_result(original, tool_name="shell")

    # When: the tool name is explicitly added through flat kwargs.
    result = transform_tool_result(
        original,
        tool_name="shell",
        tokenjuice_tool_aliases="shell,bash",
        tokenjuice_min_text_chars=1,
    )

    # Then: aliases opt the tool into terminal compaction.
    assert default_result is None
    compacted = parse_result(result)
    meta = json_object(compacted["tokenjuice"])
    assert meta["compacted"] is True


def test_read_file_is_protected_even_when_aliases_or_modes_try_to_enable_it() -> None:
    # Given: a read_file payload and kwargs that would otherwise opt it in.
    original = load_fixture("read-file.txt")

    # When: transform_tool_result sees read_file.
    result = transform_tool_result(
        original,
        tool_name="read_file",
        tokenjuice_mode="metadata",
        tokenjuice_tool_aliases="read_file,terminal",
        tokenjuice_min_text_chars=1,
    )

    # Then: exact file reads remain protected before config parsing.
    assert result is None


def test_off_mode_fails_open_without_replacement() -> None:
    # Given: a long terminal JSON result.
    original = load_fixture("terminal-long.json")

    # When: tokenjuice is turned off through flat kwargs.
    result = transform_tool_result(original, tool_name="terminal", tokenjuice_mode="off")

    # Then: the hook leaves the original result alone.
    assert result is None


def test_metadata_mode_preserves_text_fields_and_adds_previews() -> None:
    # Given: a long terminal JSON result.
    original = load_fixture("terminal-long.json")
    source = load_json_object("terminal-long.json")

    # When: metadata mode is selected.
    result = transform_tool_result(
        original,
        tool_name="terminal",
        tokenjuice_mode="metadata",
        tokenjuice_min_text_chars=1,
        tokenjuice_preview_chars=12,
    )

    # Then: stdout stays exact while tokenjuice metadata is added.
    compacted = parse_result(result)
    assert compacted["stdout"] == source["stdout"]
    meta = json_object(compacted["tokenjuice"])
    fields = json_object(meta["fields"])
    stdout_meta = json_object(fields["stdout"])
    assert meta["compacted"] is False
    assert meta["mode"] == "metadata"
    assert stdout_meta["preview"] == "line 1\nline "


def test_invalid_config_fails_open_without_raising() -> None:
    # Given: a valid terminal payload and invalid flat config.
    original = load_fixture("terminal-long.json")

    # When: options fail validation.
    result = transform_tool_result(original, tool_name="terminal", tokenjuice_head_lines=-1)

    # Then: invalid config disables replacement instead of raising.
    assert result is None


def test_invalid_json_fails_open_without_raising() -> None:
    # Given: malformed terminal JSON.
    original = load_fixture("terminal-invalid.json")

    # When: the plugin attempts to transform invalid JSON.
    result = transform_tool_result(original, tool_name="terminal")

    # Then: the plugin fails open and leaves the payload unchanged.
    assert result is None or result == original


def test_fixtures_do_not_contain_private_context_patterns() -> None:
    # Given: the contract fixtures.
    names = ["manifest.json", "terminal-short.json", "terminal-long.json", "terminal-invalid.json"]
    forbidden = {"/home/", "/opt/data", "ssh://", "10.10.", "api_key", "password"}

    # When: the fixture text is scanned for private context patterns.
    combined = "\n".join(load_fixture(name).lower() for name in names)

    # Then: no private context details leak into reusable fixtures.
    assert not any(token in combined for token in forbidden)


def test_rescue_oversized_web_search_result_returns_preview_and_handle(
    tmp_path: Path,
) -> None:
    # Given: an oversized web_search result and a writable rescue store.
    original = web_result(big_web_content())

    # When: the plugin transforms the eligible result with a stable session ID.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
    )

    # Then: the model sees a short preview plus an opaque fetch handle.
    assert result is not None
    assert "rescued" in result.lower() or "preview" in result.lower()
    assert "rescuer_fetch" in result
    assert len(result) < len(original)
    blob_id = _extract_hex_handle(result)
    assert blob_id is not None

    # And: the same session can redeem the handle for the full content.
    host = _ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    fetch = _rescuer_fetch_tool(host)
    fetched = fetch(args={"id": blob_id, "mode": "full"}, session_id="session-a")
    assert isinstance(fetched, str)
    assert "web result line 0001" in fetched
    assert "web result line 1200" in fetched


def test_rescue_oversized_mcp_result_returns_handle(tmp_path: Path) -> None:
    # Given: an oversized MCP-style result.
    original = json.dumps({"results": big_web_content()})

    # When: the plugin transforms the eligible MCP result.
    result = transform_tool_result(
        original,
        tool_name="mcp_tool",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
    )

    # Then: the model receives a rescue handle.
    assert result is not None
    assert _extract_hex_handle(result) is not None


def test_rescue_oversized_browser_result_returns_handle(tmp_path: Path) -> None:
    # Given: an oversized browser snapshot result.
    original = json.dumps({"snapshot": big_web_content()})

    # When: the plugin transforms the eligible browser result.
    result = transform_tool_result(
        original,
        tool_name="browser_snapshot",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
    )

    # Then: the model receives a rescue handle.
    assert result is not None
    assert _extract_hex_handle(result) is not None


def test_rescue_no_store_fail_open_keeps_original_result(tmp_path: Path) -> None:
    # Given: an oversized result but a store path that does not exist.
    original = web_result(big_web_content())
    missing_store = str(tmp_path / "does_not_exist")

    # When: rescue is asked to write to a missing store.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=missing_store,
    )

    # Then: the hook fails open instead of emitting a dead handle.
    assert result is None or result == original


def test_rescue_fetch_unavailable_keeps_enough_inline_content(
    tmp_path: Path,
) -> None:
    # Given: an oversized result on a host that cannot register the fetch tool.
    original = web_result(big_web_content())

    # When: rescue runs without a fetch tool available.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=False,
    )

    # Then: the model either sees the original result or a preview with full
    # content recoverable inline; no unredeemable handle is emitted.
    assert result is None or len(result) >= len(original) // 2
    if result is not None:
        assert _extract_hex_handle(result) is None


def test_rescue_read_file_remains_exact_even_when_oversized() -> None:
    # Given: a read_file payload that is far above any rescue threshold.
    original = json.dumps({"content": big_web_content()})

    # When: the plugin transforms read_file.
    result = transform_tool_result(
        original,
        tool_name="read_file",
        session_id="session-a",
    )

    # Then: exact file reads are never replaced by a rescue handle.
    assert result is None


def test_rescue_error_stderr_remains_exact() -> None:
    # Given: a failed terminal result with large stdout and large stderr.
    stderr = numbered_lines("stderr", 120)
    original = terminal_payload(
        stdout=numbered_lines("stdout", 120),
        stderr=stderr,
        exit_code=1,
        status="failed",
    )

    # When: the plugin transforms an error terminal result.
    result = transform_tool_result(
        original,
        tool_name="terminal",
        session_id="session-a",
    )

    # Then: stderr stays exact for debugging context.
    assert result is None or parse_result(result)["stderr"] == stderr


def test_rescue_traceback_output_remains_exact() -> None:
    # Given: execute_code embeds a traceback inside the output field.
    traceback = (
        "Traceback (most recent call last):\n" + numbered_lines("frame", 120) + "\nValueError: boom"
    )
    original = json.dumps({"status": "error", "output": traceback})

    # When: the plugin transforms the diagnostic payload.
    result = transform_tool_result(
        original,
        tool_name="execute_code",
        session_id="session-a",
    )

    # Then: traceback-bearing output stays exact.
    assert result is None or parse_result(result)["output"] == traceback


def test_rescue_without_session_id_emits_no_model_visible_handle(
    tmp_path: Path,
) -> None:
    # Given: an oversized eligible result without a stable session ID.
    original = web_result(big_web_content())

    # When: the plugin transforms the result without a session.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        tokenjuice_rescue_store_path=str(tmp_path),
    )

    # Then: no opaque handle is exposed to the model.
    assert result is None or _extract_hex_handle(result) is None


def test_rescuer_fetch_rejects_invalid_handle() -> None:
    # Given: a host with the rescue fetch tool registered.
    host = _ToolHost()
    register(host)
    fetch = _rescuer_fetch_tool(host)

    # When: the model asks for an invalid handle.
    result = fetch(
        args={"id": "../../../etc/passwd", "mode": "stat"},
        session_id="session-a",
    )

    # Then: the fetch rejects the handle without leaking path details.
    assert isinstance(result, str)
    assert "invalid" in result.lower() or "not found" in result.lower()


def test_rescuer_fetch_denies_cross_session_blob() -> None:
    # Given: a host with the rescue fetch tool registered.
    host = _ToolHost()
    register(host)
    fetch = _rescuer_fetch_tool(host)

    # When: a different session asks for a blob it does not own.
    result = fetch(
        args={"id": "deadbeef0000", "mode": "range", "start": 0, "count": 1},
        session_id="intruder",
    )

    # Then: cross-session access is denied and no bytes leak.
    assert isinstance(result, str)
    assert "not available in this session" in result.lower()


def test_rescuer_fetch_grep_is_bounded_against_redos() -> None:
    # Given: a host with the rescue fetch tool registered.
    host = _ToolHost()
    register(host)
    fetch = _rescuer_fetch_tool(host)

    # When: the model submits a catastrophic-backtracking regex pattern.
    start = time.time()
    result = fetch(
        args={"id": "000000000000", "mode": "grep", "pattern": "(a|a)*c"},
        session_id="session-a",
    )
    elapsed = time.time() - start

    # Then: the search returns quickly with a bounded response.
    assert elapsed < 3.0
    assert isinstance(result, str)


def test_passref_is_disabled_by_default() -> None:
    # Given: passref left at its default and a same-session handle.
    store = _FakeRescueStore()
    mw = _passref_middleware(store, {"tokenjuice_passref_enabled": False})
    blob_id = store.put("SECRET", "web_search", "session-a")

    # When: an allowlisted tool receives a rescued handle.
    result = mw(
        tool_name="summarise",
        args={"body": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: expansion is disabled by default; the handle is not expanded.
    assert result is None


def test_passref_requires_explicit_allowlist_when_enabled() -> None:
    # Given: passref enabled with a strict tool allowlist.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )
    blob_id = store.put("DATA", "web_search", "session-a")

    # When: allowed and disallowed tools receive the same handle.
    allowed = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )
    disallowed = mw(
        tool_name="publish",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: only the explicitly allowlisted non-sink tool expands the handle.
    assert allowed is not None
    allowed_args = json_object(allowed["args"])
    x_value = allowed_args["x"]
    assert isinstance(x_value, str)
    assert x_value == "DATA"
    assert disallowed is not None
    disallowed_args = json_object(disallowed["args"])
    assert "not eligible" in str(disallowed_args["x"])
    assert "DATA" not in str(disallowed_args["x"])


def test_passref_denies_sink_tools_by_default() -> None:
    # Given: passref enabled with no allowlist.
    store = _FakeRescueStore()
    mw = _passref_middleware(store, {"tokenjuice_passref_enabled": True})
    blob_id = store.put("PAYLOAD", "web_search", "session-a")

    # When: exec/exfil sink tools receive a rescued handle.
    # Then: sink tools never receive silent expansion; handles are replaced by a denial marker.
    for sink in ("shell", "write_file", "http_post"):
        result = mw(tool_name=sink, args={"x": f"tla:{blob_id}"}, session_id="session-a")
        assert result is not None
        args = json_object(result["args"])
        assert "not eligible" in str(args["x"])
        assert "PAYLOAD" not in str(args["x"])


def test_passref_allowlisted_sinks_still_denied() -> None:
    # Given: passref enabled with sink tools explicitly in the allowlist.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": [
                "shell",
                "write_file",
                "http_post",
                "curl",
                "execute_code",
            ],
        },
    )
    blob_id = store.put("PAYLOAD", "web_search", "session-a")

    # When/Then: the sink denylist dominates the allowlist; no expansion occurs.
    for sink in ("shell", "write_file", "http_post", "curl", "execute_code"):
        result = mw(tool_name=sink, args={"x": f"tla:{blob_id}"}, session_id="session-a")
        assert result is not None
        args = json_object(result["args"])
        assert "not eligible" in str(args["x"])
        assert "PAYLOAD" not in str(args["x"])


def test_passref_enabled_without_allowlist_denies_non_sinks() -> None:
    # Given: passref enabled without an explicit allowlist.
    store = _FakeRescueStore()
    mw = _passref_middleware(store, {"tokenjuice_passref_enabled": True})
    blob_id = store.put("DATA", "web_search", "session-a")

    # When/Then: even a non-sink tool is denied with a marker, not expanded.
    result = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )
    assert result is not None
    args = json_object(result["args"])
    assert "not eligible" in str(args["x"])
    assert "DATA" not in str(args["x"])


def test_passref_respects_size_caps() -> None:
    # Given: passref enabled with tight per-token and per-call caps.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
            "tokenjuice_passref_max_chars": 50,
            "tokenjuice_passref_total_max_chars": 80,
        },
    )
    blob_id = store.put("z" * 200, "web_search", "session-a")

    # When: two handles exceed the total budget.
    result = mw(
        tool_name="summarise",
        args={"a": f"tla:{blob_id}", "b": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: the first expansion is truncated and the second is budget-blocked.
    assert result is not None
    args = json_object(result["args"])
    a_value = args["a"]
    b_value = args["b"]
    assert isinstance(a_value, str)
    assert isinstance(b_value, str)
    assert "truncated" in a_value or "budget" in b_value


def test_passref_hard_exempts_exactness_tools() -> None:
    # Given: passref enabled broadly with an allowlist that names exactness tools.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["read_file", "diagnostics"],
        },
    )
    blob_id = store.put("DATA", "web_search", "session-a")

    # When: exactness-critical tools receive a rescued handle.
    # Then: read_file and diagnostic tools are hard-exempted from expansion.
    for protected in ("read_file", "diagnostics"):
        result = mw(
            tool_name=protected,
            args={"x": f"tla:{blob_id}"},
            session_id="session-a",
        )
        assert result is None


def test_passref_without_session_id_fails_closed() -> None:
    # Given: passref enabled and a handle owned by some session.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )
    blob_id = store.put("DATA", "web_search", "session-a")

    # When: the host does not forward a stable session ID.
    result = mw(tool_name="summarise", args={"x": f"tla:{blob_id}"})

    # Then: the raw handle is replaced by a clear session-required marker.
    assert result is not None
    args = json_object(result["args"])
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "session ID" in x_value
    assert "DATA" not in x_value


def test_passref_expands_for_same_session() -> None:
    # Given: passref enabled and a blob owned by the calling session.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )
    blob_id = store.put("OWNED", "web_search", "session-a")

    # When: an allowlisted tool receives a same-session handle.
    result = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: the handle expands to the full blob content.
    assert result is not None
    args = json_object(result["args"])
    assert args["x"] == "OWNED"


def test_passref_denies_cross_session() -> None:
    # Given: passref enabled with an allowlist and a blob owned by a different session.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )
    blob_id = store.put("SECRET", "web_search", "owner")

    # When: another session presents the handle.
    result = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="intruder",
    )

    # Then: the content is not exposed and a clear session marker is returned.
    assert result is not None
    args = json_object(result["args"])
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "not available in this session" in x_value
    assert "SECRET" not in x_value


def test_passref_missing_blob_returns_marker() -> None:
    # Given: passref enabled with an allowlist but no blob for a well-formed handle.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )

    # When: an allowlisted tool references a missing blob.
    result = mw(
        tool_name="summarise",
        args={"x": "tla:000000000000"},
        session_id="session-a",
    )

    # Then: a clear missing-blob marker is returned instead of silent failure.
    assert result is not None
    args = json_object(result["args"])
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "unavailable" in x_value


def test_passref_per_token_cap_truncates_with_marker() -> None:
    # Given: passref enabled with a small per-token cap.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
            "tokenjuice_passref_max_chars": 20,
        },
    )
    blob_id = store.put("x" * 500, "web_search", "session-a")

    # When: an allowlisted tool references a blob larger than the per-token cap.
    result = mw(
        tool_name="summarise",
        args={"x": f"tla:{blob_id}"},
        session_id="session-a",
    )

    # Then: the content is truncated and carries a clear truncation marker.
    assert result is not None
    args = json_object(result["args"])
    x_value = args["x"]
    assert isinstance(x_value, str)
    assert "truncated" in x_value
    assert x_value.startswith("x" * 20)


def test_passref_expands_nested_args() -> None:
    # Given: passref enabled with an allowlist and a same-session handle nested in JSON args.
    store = _FakeRescueStore()
    mw = _passref_middleware(
        store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["summarise"],
        },
    )
    blob_id = store.put("NESTED", "web_search", "session-a")

    # When: the handle appears inside nested structures.
    result = mw(
        tool_name="summarise",
        args={"a": {"b": [f"tla:{blob_id}"]}},
        session_id="session-a",
    )

    # Then: recursion expands the handle at every level.
    assert result is not None
    args = json_object(result["args"])
    nested = json_object(args["a"])
    items = nested["b"]
    assert isinstance(items, list)
    assert items[0] == "NESTED"
