from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING

from tokenjuice_hermes.compaction import transform_tool_result
from tokenjuice_hermes.rescue_index import idx_path
from tokenjuice_hermes.rescue_sqlite_types import MIGRATION_MARKER
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


def _legacy_blob(blob_dir: Path, content: str) -> tuple[str, str, int]:
    raw = content.encode("utf-8")
    full_hash = hashlib.sha256(raw).hexdigest()
    handle = full_hash[:12]
    _ = (blob_dir / handle).write_bytes(raw)
    return handle, full_hash, len(raw)


def test_migrates_exact_legacy_safe_sid_json_and_fails_closed(tmp_path: Path) -> None:
    short_sid = "20260728_054800_f7e89c3d"
    long_sid = "very-long-session-id-that-exceeds-thirty-two-characters-for-safe-sid"
    blob_dir = tmp_path / "blobs"
    session_dir = tmp_path / "sessions"
    blob_dir.mkdir()
    session_dir.mkdir()
    live, live_hash, live_size = _legacy_blob(blob_dir, "live data")
    shared, shared_hash, shared_size = _legacy_blob(blob_dir, "shared data")
    missing, missing_hash, missing_size = _legacy_blob(blob_dir, "missing data")
    mismatch, mismatch_hash, mismatch_size = _legacy_blob(blob_dir, "mismatch data")
    conflict, _conflict_hash, conflict_size = _legacy_blob(blob_dir, "alpha")
    _ = idx_path(tmp_path / "sessions", short_sid).write_text(
        json.dumps(
            {
                "blobs": {
                    live: {"t": 1.0, "tool": "web_search", "size": live_size, "hash": live_hash},
                    shared: {
                        "t": 2.0,
                        "tool": "web_search",
                        "size": shared_size,
                        "hash": shared_hash,
                    },
                    missing: {
                        "t": 3.0,
                        "tool": "web_search",
                        "size": missing_size,
                        "hash": missing_hash,
                    },
                    mismatch: {
                        "t": 4.0,
                        "tool": "web_search",
                        "size": mismatch_size,
                        "hash": mismatch_hash,
                    },
                    conflict: {
                        "t": 5.0,
                        "tool": "web_search",
                        "size": conflict_size,
                        "hash": hashlib.sha256(b"bravo").hexdigest(),
                    },
                    "111111111111": {"swept_at": 6.0, "tool": "web_search", "size": 20},
                }
            }
        ),
        encoding="utf-8",
    )
    _ = idx_path(tmp_path / "sessions", long_sid).write_text(
        json.dumps(
            {
                "blobs": {
                    shared: {"t": 7.0, "tool": "browser", "size": shared_size, "hash": shared_hash}
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "blobs" / missing).unlink()
    _ = (tmp_path / "blobs" / mismatch).write_text("MISMATCH", encoding="utf-8")
    _ = (tmp_path / "sessions" / "corrupt-safe-sid.json").write_text("{not-json", encoding="utf-8")

    migrated = BlobStore({"store_path": str(tmp_path)})

    assert migrated.fetch(live, mode="full", session_id=short_sid) == "live data"
    assert migrated.fetch(shared, mode="full", session_id=short_sid) == "shared data"
    assert migrated.fetch(shared, mode="full", session_id=long_sid) == "shared data"
    assert "not available in this session" in migrated.fetch(live, mode="full", session_id="other")
    assert "[Swept]" in migrated.fetch("111111111111", mode="full", session_id=short_sid)
    assert "not available in this session" in migrated.fetch(
        missing, mode="full", session_id=short_sid
    )
    assert "not available in this session" in migrated.fetch(
        mismatch, mode="full", session_id=short_sid
    )
    assert "not available in this session" in migrated.fetch(
        conflict, mode="full", session_id=short_sid
    )
    assert not (tmp_path / "blobs" / "111111111111").exists()
    assert (tmp_path / "sessions" / "corrupt-safe-sid.json").exists()
    assert (tmp_path / "migration-quarantine" / "corrupt-safe-sid.json").exists()
    assert (tmp_path / MIGRATION_MARKER).exists()
    assert (
        BlobStore({"store_path": str(tmp_path)}).fetch(live, mode="full", session_id=short_sid)
        == "live data"
    )


def test_migration_preserves_live_blob_when_tombstone_index_is_seen_first(
    tmp_path: Path,
) -> None:
    first_sid = "session-a"
    second_sid = "session-b"
    blob_dir = tmp_path / "blobs"
    session_dir = tmp_path / "sessions"
    blob_dir.mkdir()
    session_dir.mkdir()
    handle, full_hash, size = _legacy_blob(blob_dir, "shared live")
    _ = idx_path(session_dir, first_sid).write_text(
        json.dumps({"blobs": {handle: {"swept_at": 1.0, "tool": "web_search", "size": size}}}),
        encoding="utf-8",
    )
    _ = idx_path(session_dir, second_sid).write_text(
        json.dumps(
            {"blobs": {handle: {"t": 2.0, "tool": "web_search", "size": size, "hash": full_hash}}}
        ),
        encoding="utf-8",
    )

    migrated = BlobStore({"store_path": str(tmp_path)})

    assert "[Swept]" in migrated.fetch(handle, mode="full", session_id=first_sid)
    assert migrated.fetch(handle, mode="full", session_id=second_sid) == "shared live"


def test_migration_preserves_tombstone_when_live_index_is_seen_first(tmp_path: Path) -> None:
    first_sid = "session-a"
    second_sid = "session-b"
    blob_dir = tmp_path / "blobs"
    session_dir = tmp_path / "sessions"
    blob_dir.mkdir()
    session_dir.mkdir()
    handle, full_hash, size = _legacy_blob(blob_dir, "shared live")
    _ = idx_path(session_dir, first_sid).write_text(
        json.dumps(
            {"blobs": {handle: {"t": 1.0, "tool": "web_search", "size": size, "hash": full_hash}}}
        ),
        encoding="utf-8",
    )
    _ = idx_path(session_dir, second_sid).write_text(
        json.dumps({"blobs": {handle: {"swept_at": 2.0, "tool": "web_search", "size": size}}}),
        encoding="utf-8",
    )

    migrated = BlobStore({"store_path": str(tmp_path)})

    assert migrated.fetch(handle, mode="full", session_id=first_sid) == "shared live"
    assert "[Swept]" in migrated.fetch(handle, mode="full", session_id=second_sid)


def test_empty_migration_marker_does_not_skip_legacy_migration(tmp_path: Path) -> None:
    session_id = "session-a"
    blob_dir = tmp_path / "blobs"
    session_dir = tmp_path / "sessions"
    blob_dir.mkdir()
    session_dir.mkdir()
    handle, full_hash, size = _legacy_blob(blob_dir, "live after crash")
    _ = idx_path(session_dir, session_id).write_text(
        json.dumps(
            {"blobs": {handle: {"t": 1.0, "tool": "web_search", "size": size, "hash": full_hash}}}
        ),
        encoding="utf-8",
    )
    _ = (tmp_path / MIGRATION_MARKER).write_text("", encoding="utf-8")

    migrated = BlobStore({"store_path": str(tmp_path)})

    assert migrated.fetch(handle, mode="full", session_id=session_id) == "live after crash"


def test_rescue_transform_fails_open_when_store_put_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    payload = json.dumps({"content": "x" * 50})

    def fail_put(*_args: object, **_kwargs: object) -> str:
        message = "database is locked"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr("tokenjuice_hermes.rescue_store.BlobStore.put", fail_put)

    assert (
        transform_tool_result(
            payload,
            tool_name="web_search",
            session_id="session-a",
            tokenjuice_rescue_store_path=str(tmp_path),
            tokenjuice_rescue_fetch_available=True,
            tokenjuice_rescue_min_text_chars=10,
        )
        is None
    )
