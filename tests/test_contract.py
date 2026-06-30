from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypeAlias

import pytest
from pydantic import JsonValue, TypeAdapter

from tokenjuice_hermes.compaction import transform_tool_result
from tokenjuice_hermes.plugin import register

JsonScalar: TypeAlias = None | bool | int | float | str
FlatJsonObject: TypeAlias = dict[str, JsonScalar]
TerminalJsonObject: TypeAlias = dict[str, JsonValue]
HookCallback: TypeAlias = Callable[..., str | None]
FLAT_JSON_ADAPTER = TypeAdapter(FlatJsonObject)
TERMINAL_JSON_ADAPTER = TypeAdapter(TerminalJsonObject)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACKAGE = Path(__file__).resolve().parents[1] / "tokenjuice_hermes"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_object(name: str) -> FlatJsonObject:
    return FLAT_JSON_ADAPTER.validate_json(load_fixture(name))


def parse_result(result: str | None) -> TerminalJsonObject:
    assert isinstance(result, str)
    return TERMINAL_JSON_ADAPTER.validate_json(result)


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_manifest_declares_transform_tool_result_hook() -> None:
    # Given: the contract manifest fixture.
    manifest = load_fixture("manifest.json")

    # When: the manifest is inspected for hook declarations.
    plugin_name_found = '"name": "tokenjuice-hermes"' in manifest
    hook_found = '"transform_tool_result"' in manifest

    # Then: the plugin advertises transform_tool_result.
    assert plugin_name_found
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
    for name in ["__init__.py", "compaction.py", "plugin.py", "plugin.yaml", "py.typed"]:
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
    # Given: a long terminal JSON result that needs compaction.
    original = load_fixture("terminal-long.json")

    # When: the plugin transforms the terminal result.
    result = transform_tool_result(original, tool_name="terminal")

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


def test_execute_code_uses_default_terminal_compaction() -> None:
    # Given: execute_code emits the same terminal-like JSON shape.
    original = load_fixture("terminal-long.json")

    # When: the plugin transforms the execute_code result.
    result = transform_tool_result(original, tool_name="execute_code")

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
        original, tool_name="shell", tokenjuice_tool_aliases="shell,bash"
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
