from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from tokenjuice_hermes.rescue_excerpt import build_excerpt
from tokenjuice_hermes.rescue_fetch import rescuer_fetch
from tokenjuice_hermes.rescue_handles import is_valid_handle, normalize_handle
from tokenjuice_hermes.rescue_store import BlobStore
from tokenjuice_hermes.rescue_types import (
    RESCUE_HOST_STATE_VOLUME,
    RESCUE_STORE_GID,
    RESCUE_STORE_MODE,
    RESCUE_STORE_PATH_DEFAULT,
    RESCUE_STORE_UID,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_rescue_store_put_returns_valid_handle(tmp_path: Path) -> None:
    # Given: a writable rescue store and a session.
    store = BlobStore({"store_path": str(tmp_path)})
    content = "line one\nline two\nline three"

    # When: content is stored with a stable session ID.
    handle = store.put(content, tool_name="web_search", session_id="session-a")

    # Then: the handle is an opaque 12-hex string and the blob is retrievable.
    assert is_valid_handle(handle)
    assert store.blob_text(handle) == content


def test_rescue_store_fetch_full_round_trip(tmp_path: Path) -> None:
    # Given: a stored blob.
    store = BlobStore({"store_path": str(tmp_path)})
    content = "\n".join(f"row {number:04d}" for number in range(1, 201))
    handle = store.put(content, tool_name="web_search", session_id="session-a")

    # When: the same session fetches the full blob.
    fetched = store.fetch(handle, mode="full", session_id="session-a")

    # Then: the exact content is returned.
    assert fetched == content


def test_rescue_store_fetch_stat_includes_metadata(tmp_path: Path) -> None:
    # Given: a stored blob.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("hello world", tool_name="browser_snapshot", session_id="session-a")

    # When: stat mode is requested.
    stat = store.fetch(handle, mode="stat", session_id="session-a")

    # Then: the response names the handle, size, and source tool.
    assert handle in stat
    assert "browser_snapshot" in stat
    assert "bytes" in stat


def test_rescue_store_fetch_rejects_invalid_handle(tmp_path: Path) -> None:
    # Given: a rescue store.
    store = BlobStore({"store_path": str(tmp_path)})

    # When: a path-traversal handle is requested.
    result = store.fetch("../../../etc/passwd", mode="stat", session_id="session-a")

    # Then: the request is rejected without leaking path details.
    assert "invalid" in result.lower()
    assert "passwd" not in result


def test_rescue_store_fetch_denies_cross_session_handle(tmp_path: Path) -> None:
    # Given: a blob owned by session-a.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("secret data", tool_name="mcp_tool", session_id="session-a")

    # When: a different session asks for the same handle.
    result = store.fetch(handle, mode="full", session_id="intruder")

    # Then: access is denied and no bytes leak.
    assert "not available in this session" in result.lower()
    assert "secret data" not in result


def test_rescue_store_put_without_session_id_emits_no_handle(
    tmp_path: Path,
) -> None:
    # Given: an eligible result but no stable session ID.
    store = BlobStore({"store_path": str(tmp_path)})
    content = "oversized result that would normally be rescued"

    # When: put is called without a session ID.
    handle = store.put(content, tool_name="web_search", session_id="")

    # Then: no model-visible handle is created and the inline content remains recoverable.
    assert handle == ""
    assert store.blob_text(normalize_handle("deadbeef0000") or "") is None


def test_rescue_store_no_session_does_not_index_blob(tmp_path: Path) -> None:
    # Given: a store and content without a session.
    store = BlobStore({"store_path": str(tmp_path)})
    content = "unowned content"

    # When: content is stored without a session ID.
    _ = store.put(content, tool_name="web_search", session_id="")

    # Then: no session index file is created.
    assert not any(store.meta_dir.iterdir())


def test_rescue_store_tombstone_after_sweep(tmp_path: Path) -> None:
    # Given: a blob stored with an immediate TTL.
    store = BlobStore(
        {
            "store_path": str(tmp_path),
            "ttl_hours": 0,
            "tombstone_ttl_hours": 24,
        }
    )
    handle = store.put("old data", tool_name="web_search", session_id="session-a")
    assert store.fetch(handle, mode="full", session_id="session-a") == "old data"

    # When: enough time passes and the sweep runs.
    time.sleep(0.01)
    store.lazy_sweep()

    # Then: the blob is gone but a tombstone message remains for the owning session.
    result = store.fetch(handle, mode="full", session_id="session-a")
    assert "[Swept]" in result
    assert "web_search" in result
    assert "old data" not in result


def test_rescue_store_sweep_removes_blob_when_no_session_references(
    tmp_path: Path,
) -> None:
    # Given: a blob indexed only by session-a.
    store = BlobStore({"store_path": str(tmp_path), "ttl_hours": 0})
    handle = store.put("data", tool_name="web_search", session_id="session-a")

    # When: the blob expires and is swept.
    time.sleep(0.01)
    store.lazy_sweep()

    # Then: the on-disk blob file is removed.
    assert (tmp_path / "blobs" / handle).exists() is False


def test_rescue_store_path_constants_declare_canonical_paths() -> None:
    # Given: the rescue store wiring constants.

    # Then: they match the persistent runtime-volume layout for later Nix wiring.
    assert RESCUE_STORE_PATH_DEFAULT == "/opt/data/tokenjuice-hermes/rescue-blobs"
    assert RESCUE_HOST_STATE_VOLUME == RESCUE_STORE_PATH_DEFAULT
    assert RESCUE_STORE_UID == 1000
    assert RESCUE_STORE_GID == 100
    assert RESCUE_STORE_MODE == 0o700


def test_rescue_store_does_not_write_outside_configured_path(tmp_path: Path) -> None:
    # Given: a store configured to a specific path.
    store = BlobStore({"store_path": str(tmp_path)})

    # When: content is stored.
    _ = store.put("data", tool_name="web_search", session_id="session-a")

    # Then: all written files live under the configured base directory.
    for path in tmp_path.rglob("*"):
        assert str(path.resolve()).startswith(str(tmp_path.resolve()))


def test_rescue_store_range_returns_requested_lines(tmp_path: Path) -> None:
    # Given: a stored multi-line blob.
    store = BlobStore({"store_path": str(tmp_path)})
    content = "\n".join(f"line {number}" for number in range(1, 21))
    handle = store.put(content, tool_name="web_search", session_id="session-a")

    # When: a range is fetched.
    result = store.fetch(handle, mode="range", session_id="session-a", start=5, count=3)

    # Then: the requested lines are returned with range metadata.
    assert "line 6" in result
    assert "line 7" in result
    assert "line 8" in result
    assert "of 20" in result


def test_rescue_handle_validation_accepts_only_12_hex() -> None:
    # Given: a set of candidate handle strings.

    # Then: only 12-character lowercase hex strings are valid.
    assert is_valid_handle("deadbeef0000") is True
    assert is_valid_handle("DEADBEEF0000") is False
    assert is_valid_handle("deadbeef") is False
    assert is_valid_handle("deadbeef000g") is False
    assert is_valid_handle("../../../etc/passwd") is False
    assert normalize_handle("deadbeef0000") == "deadbeef0000"
    assert normalize_handle("../../../etc/passwd") is None


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("full", {}),
        ("stat", {}),
        ("range", {"start": 0, "count": 1}),
    ],
)
def test_rescue_store_fetch_without_session_id_fails_closed(
    tmp_path: Path,
    mode: str,
    extra: dict[str, int],
) -> None:
    # Given: a blob owned by a session.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("data", tool_name="web_search", session_id="session-a")

    # When: fetched without a session ID.
    result = store.fetch(handle, mode=mode, session_id="", **extra)

    # Then: the fetch is denied because ownership cannot be proven.
    assert (
        "session" in result.lower()
        or "not available" in result.lower()
        or "not found" in result.lower()
    )


def test_rescue_store_full_mode_refused_over_cap(tmp_path: Path) -> None:
    # Given: a blob larger than the full-fetch cap.
    store = BlobStore(
        {
            "store_path": str(tmp_path),
            "full_fetch_max_chars": 50,
            "refuse_full_fetch": True,
        }
    )
    content = "x" * 200
    handle = store.put(content, tool_name="web_search", session_id="session-a")

    # When: full mode is requested.
    result = store.fetch(handle, mode="full", session_id="session-a")

    # Then: the fetch is refused and suggests range mode.
    assert "Refused" in result
    assert "range" in result.lower()


def test_rescue_store_grep_finds_matching_lines(tmp_path: Path) -> None:
    # Given: a stored blob and a literal pattern.
    store_path = str(tmp_path)
    content = "\n".join(f"line {number:03d}" for number in range(1, 101))
    handle = BlobStore({"store_path": store_path}).put(
        content, tool_name="web_search", session_id="session-a"
    )

    # When: grep mode searches for a literal substring.
    result = rescuer_fetch(
        {"id": handle, "mode": "grep", "pattern": "line 042"},
        session_id="session-a",
        config={"store_path": store_path},
    )

    # Then: the matching line is returned.
    assert "line 042" in result


def test_rescue_store_grep_refuses_regex_metacharacters(tmp_path: Path) -> None:
    # Given: a stored blob.
    store_path = str(tmp_path)
    handle = BlobStore({"store_path": store_path}).put(
        "hello world", tool_name="web_search", session_id="session-a"
    )

    # When: a pattern with regex metacharacters is submitted.
    result = rescuer_fetch(
        {"id": handle, "mode": "grep", "pattern": "^hello$"},
        session_id="session-a",
        config={"store_path": store_path},
    )

    # Then: the literal-only grep refuses it safely.
    assert "regex" in result.lower() or "literal" in result.lower()


def test_rescue_store_redos_pattern_completes_fast(tmp_path: Path) -> None:
    # Given: a large blob and a catastrophic regex-like pattern.
    store_path = str(tmp_path)
    content = "a" * 1000 + "c"
    handle = BlobStore({"store_path": store_path}).put(
        content, tool_name="web_search", session_id="session-a"
    )

    # When: a redos-prone pattern is submitted.
    start = time.time()
    result = rescuer_fetch(
        {"id": handle, "mode": "grep", "pattern": "(a|a)*c"},
        session_id="session-a",
        config={"store_path": store_path},
    )
    elapsed = time.time() - start

    # Then: the search returns quickly, even if it refuses the pattern.
    assert elapsed < 3.0
    assert isinstance(result, str)


def test_rescue_store_excerpt_says_preview_not_full() -> None:
    # Given: oversized content.
    content = "\n".join(f"line {number:04d}" for number in range(1, 201))

    # When: an excerpt is built.
    excerpt = build_excerpt(content)

    # Then: the excerpt explicitly states it is a preview, not the full content.
    assert "Preview only" in excerpt
    assert "NOT the full content" in excerpt
    assert len(excerpt) < len(content)
