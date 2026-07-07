from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from tests.host_fixtures import (
    HookOnlyHost,
    ToolHost,
    big_web_content,
    extract_hex_handle,
    web_result,
)
from tokenjuice_hermes.compaction import transform_tool_result
from tokenjuice_hermes.observability import (
    reset_stats,
    status_snapshot,
    tokenjuice_status,
)
from tokenjuice_hermes.passref import make_passref_middleware
from tokenjuice_hermes.plugin import register
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_counters() -> None:  # pyright: ignore[reportUnusedFunction]
    reset_stats()


@pytest.fixture
def tmp_store(tmp_path: Path) -> BlobStore:
    return BlobStore({"store_path": str(tmp_path)})


def _status_dict(store_path: str = "") -> dict[str, object]:
    return status_snapshot(store_path)


def _json_object(text: str) -> dict[str, object]:
    value = cast("object", json.loads(text))
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def test_tokenjuice_status_returns_aggregate_counters() -> None:
    # Given: a fresh plugin process.
    # When: status is requested through the formatted tool surface.
    result = tokenjuice_status({})

    # Then: the result is a JSON object with only aggregate, non-sensitive fields.
    parsed = _json_object(result)
    assert "version" in parsed
    assert parsed.get("passref_enabled") is False
    assert "compaction_count" in parsed
    assert "rescue_count" in parsed
    assert "fetch_count" in parsed
    assert "passref_denied_count" in parsed
    assert "store" in parsed


def test_status_redacts_private_context(tmp_path: Path) -> None:
    # Given: a rescued blob exists in a temp store.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put(
        "sensitive blob content",
        tool_name="web_search",
        session_id="session-secret",
    )
    assert handle

    # When: status is requested.
    snapshot = status_snapshot(store_path=str(tmp_path))

    # Then: no raw content, session IDs, private paths, or per-session rows appear.
    text = json.dumps(snapshot)
    forbidden = {
        "sensitive blob content",
        "session-secret",
        str(tmp_path),
        "/private/example",
        "PRIVATE_TOKEN_SHOULD_NOT_LEAK",
    }
    assert not any(token in text for token in forbidden)
    assert "blob_count" in text or "live_blob_count" in text


def test_compaction_increments_counters_and_chars_saved() -> None:
    # Given: a long terminal payload eligible for head_tail compaction.
    lines = "\n".join(f"line {number:03d}" for number in range(1, 101))
    original = transform_tool_result(
        json.dumps({"stdout": lines, "stderr": "", "exit": 0, "status": "ok"}),
        tool_name="terminal",
    )
    assert original is not None
    reset_stats()

    # When: compaction runs.
    compacted = transform_tool_result(
        json.dumps({"stdout": lines, "stderr": "", "exit": 0, "status": "ok"}),
        tool_name="terminal",
    )
    assert compacted is not None

    # Then: counters record the event and positive character savings.
    status = _status_dict()
    assert status["compaction_count"] == 1
    assert cast("int", status["compaction_chars_saved"]) > 0


def test_rescue_increments_counters_and_chars_saved(tmp_path: Path) -> None:
    # Given: an oversized web_search result.
    original = web_result(big_web_content())

    # When: the rescue path transforms it.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
    )
    assert result is not None

    # Then: rescue counters record the event and savings.
    status = _status_dict()
    assert status["rescue_count"] == 1
    assert cast("int", status["rescue_chars_saved"]) > 0


def test_fetch_increments_mode_counters(tmp_path: Path) -> None:
    # Given: a rescued blob and a registered fetch tool.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    transform = host.callbacks["transform_tool_result"]
    original = web_result(big_web_content())
    result = transform(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
    )
    handle = extract_hex_handle(result or "")
    assert handle

    # When: fetch is called in different modes.
    fetch = host.tools["rescuer_fetch"]
    _ = fetch(args={"id": handle, "mode": "stat"}, session_id="session-a")
    _ = fetch(args={"id": handle, "mode": "range", "start": 0, "count": 5}, session_id="session-a")

    # Then: fetch counter and mode breakdowns are recorded.
    status = _status_dict()
    assert status["fetch_count"] == 2
    modes = cast("dict[str, int]", status["fetch_modes"])
    assert modes.get("stat") == 1
    assert modes.get("range") == 1


def test_fetch_invalid_mode_is_bucketed_and_does_not_leak_input(tmp_path: Path) -> None:
    # Given: a host with fetch registered and an arbitrary caller-supplied mode string.
    host = ToolHost()
    host.config = {"tokenjuice_rescue_store_path": str(tmp_path)}
    register(host)
    private_mode = "/private/example/mode-token-MUST_NOT_LEAK"

    # When: fetch is called with an unsupported mode.
    fetch = host.tools["rescuer_fetch"]
    _ = fetch(args={"id": "000000000000", "mode": private_mode}, session_id="session-a")

    # Then: the raw mode string is never reflected in status; it is bucketed as invalid.
    status = _status_dict()
    assert status["fetch_count"] == 1
    modes = cast("dict[str, int]", status["fetch_modes"])
    assert private_mode not in modes
    assert modes.get("invalid") == 1


def test_passref_disabled_by_default_reports_enabled_false() -> None:
    # Given: default plugin config.
    host = ToolHost()
    host.config = {}
    register(host)

    # When: status is requested.
    result = tokenjuice_status({})
    parsed = _json_object(result)

    # Then: passref remains off and no expansion counters are falsely reported.
    assert parsed["passref_enabled"] is False


def test_passref_denied_for_unallowed_tool(tmp_path: Path) -> None:
    # Given: passref enabled but with an empty allowlist.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("blob content", tool_name="web_search", session_id="session-a")
    middleware = make_passref_middleware(
        lambda: store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": [],
        },
    )

    # When: an unallowed tool request carries a handle.
    denied = middleware(
        tool_name="some_tool",
        args={"query": f"see tla:{handle}"},
        session_id="session-a",
    )
    assert denied is not None

    # Then: the denial is recorded without exposing the handle or session.
    status = _status_dict()
    assert status["passref_denied_count"] == 1
    assert status["passref_expansion_count"] == 0


def test_passref_expansion_for_allowed_tool_records_chars_and_truncation(tmp_path: Path) -> None:
    # Given: passref enabled for a specific tool with a small per-blob cap.
    store = BlobStore({"store_path": str(tmp_path)})
    long_content = "x" * 10_000
    handle = store.put(long_content, tool_name="web_search", session_id="session-a")
    middleware = make_passref_middleware(
        lambda: store,
        {
            "tokenjuice_passref_enabled": True,
            "tokenjuice_passref_allowed_tools": ["allowed_tool"],
            "tokenjuice_passref_max_chars": 100,
            "tokenjuice_passref_total_max_chars": 1_000_000,
        },
    )

    # When: an allowed tool request carries the handle.
    result = middleware(
        tool_name="allowed_tool",
        args={"query": f"see tla:{handle}"},
        session_id="session-a",
    )
    assert result is not None

    # Then: expansion counters and truncation are recorded.
    status = _status_dict()
    assert status["passref_expansion_count"] == 1
    assert cast("int", status["passref_chars_expanded"]) >= 100
    assert status["passref_truncated_count"] == 1


def test_store_stats_computed_on_demand(tmp_path: Path) -> None:
    # Given: a temp store with one live blob.
    store = BlobStore({"store_path": str(tmp_path)})
    _ = store.put("hello world", tool_name="web_search", session_id="session-a")

    # When: status is requested.
    snapshot = status_snapshot(store_path=str(tmp_path))

    # Then: aggregate store stats are present and non-negative.
    store_stats = cast("dict[str, int]", snapshot["store"])
    assert store_stats["live_blob_count"] == 1
    assert store_stats["total_blob_bytes"] > 0
    assert store_stats["tombstone_count"] == 0


def test_observability_fail_open_does_not_change_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an eligible rescue transform and a stats recorder that raises.
    original = web_result(big_web_content())

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(
        "tokenjuice_hermes.observability.record_rescue",
        _explode,
    )

    # When: the rescue transform runs.
    result = transform_tool_result(
        original,
        tool_name="web_search",
        session_id="session-a",
        tokenjuice_rescue_store_path=str(tmp_path),
        tokenjuice_rescue_fetch_available=True,
    )

    # Then: the transform still produces a rescued handle despite the stats failure.
    assert result is not None
    assert extract_hex_handle(result) is not None


def test_tokenjuice_status_tool_registered_on_capable_host() -> None:
    # Given: a host that exposes register_tool.
    host = ToolHost()
    host.config = {}
    register(host)

    # When: the plugin registers.
    # Then: the read-only status tool is available.
    assert "tokenjuice_status" in host.tools
    status_result = host.tools["tokenjuice_status"]({})
    assert isinstance(status_result, str)
    parsed = _json_object(status_result)
    assert "version" in parsed


def test_status_tool_degrades_on_hook_only_host() -> None:
    # Given: a host without tool registration.
    host = HookOnlyHost()
    register(host)

    # Then: registration succeeds but no status tool is emitted.
    assert not hasattr(host, "tools")
