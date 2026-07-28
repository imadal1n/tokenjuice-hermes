from __future__ import annotations

import hashlib
import sqlite3
from typing import TYPE_CHECKING

from tokenjuice_hermes.rescue_index import safe_sid
from tokenjuice_hermes.rescue_sqlite_types import DB_NAME
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path


def _legacy_blob(blob_dir: Path, content: str) -> tuple[str, str, int]:
    raw = content.encode("utf-8")
    full_hash = hashlib.sha256(raw).hexdigest()
    handle = full_hash[:12]
    _ = (blob_dir / handle).write_bytes(raw)
    return handle, full_hash, len(raw)


def _origin_main_schema() -> str:
    return """
CREATE TABLE sessions (
  session_key TEXT PRIMARY KEY,
  created_at REAL NOT NULL
);
CREATE TABLE blobs (
  handle TEXT PRIMARY KEY CHECK(length(handle) = 12),
  full_hash TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE ownership (
  session_key TEXT NOT NULL,
  handle TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('live', 'tombstone')),
  tool TEXT NOT NULL,
  created_at REAL NOT NULL,
  accessed_at REAL,
  swept_at REAL,
  reason TEXT,
  PRIMARY KEY (session_key, handle),
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE,
  FOREIGN KEY (handle) REFERENCES blobs(handle) ON DELETE RESTRICT
);
CREATE INDEX ownership_state_idx ON ownership(state, swept_at);
"""


def test_origin_main_sqlite_schema_reopens_with_live_and_tombstone_rows(
    tmp_path: Path,
) -> None:
    blob_dir = tmp_path / "blobs"
    session_dir = tmp_path / "sessions"
    blob_dir.mkdir()
    session_dir.mkdir()
    live_handle, live_hash, live_size = _legacy_blob(blob_dir, "before upgrade")
    tombstone_handle = "111111111111"
    with sqlite3.connect(tmp_path / DB_NAME) as conn:
        _ = conn.executescript(_origin_main_schema())
        _ = conn.executemany(
            "INSERT INTO sessions(session_key, created_at) VALUES (?, ?)",
            [(safe_sid("session-live"), 1.0), (safe_sid("session-tomb"), 2.0)],
        )
        _ = conn.executemany(
            "INSERT INTO blobs(handle, full_hash, size, created_at) VALUES (?, ?, ?, ?)",
            [(live_handle, live_hash, live_size, 1.0), (tombstone_handle, "0" * 64, 20, 2.0)],
        )
        _ = conn.executemany(
            """
            INSERT INTO ownership(
              session_key, handle, state, tool, created_at, accessed_at, swept_at, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (safe_sid("session-live"), live_handle, "live", "web_search", 1.0, 1.0, None, None),
                (
                    safe_sid("session-tomb"),
                    tombstone_handle,
                    "tombstone",
                    "web_search",
                    2.0,
                    None,
                    2.0,
                    "legacy_tombstone",
                ),
            ],
        )

    upgraded = BlobStore({"store_path": str(tmp_path)})
    new_handle = upgraded.put("after upgrade", tool_name="web_search", session_id="session-live")
    no_blob_tombstone = "222222222222"
    with sqlite3.connect(tmp_path / DB_NAME) as conn:
        conn.execute("PRAGMA foreign_keys=ON").close()
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_key, created_at) VALUES (?, ?)",
            (safe_sid("session-no-blob"), 3.0),
        ).close()
        conn.execute(
            """
            INSERT INTO ownership(
              session_key, handle, state, tool, size, created_at, accessed_at, swept_at, reason
            )
            VALUES (?, ?, 'tombstone', 'web_search', 30, 3.0, NULL, 3.0, 'test_tombstone')
            """,
            (safe_sid("session-no-blob"), no_blob_tombstone),
        ).close()
    reopened = BlobStore({"store_path": str(tmp_path)})

    assert upgraded.fetch(live_handle, mode="full", session_id="session-live") == "before upgrade"
    assert "[Swept]" in upgraded.fetch(tombstone_handle, mode="full", session_id="session-tomb")
    assert reopened.fetch(new_handle, mode="full", session_id="session-live") == "after upgrade"
    assert "[Swept]" in reopened.fetch(no_blob_tombstone, mode="full", session_id="session-no-blob")
