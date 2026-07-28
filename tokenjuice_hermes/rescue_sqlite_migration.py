from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from .rescue_index import (
    HASH_KEY,
    SIZE_KEY,
    SWEPT_KEY,
    TOOL_KEY,
    BlobEntry,
    read_idx_file,
)
from .rescue_sqlite_types import MIGRATION_MARKER, BlobWrite

if TYPE_CHECKING:
    from pathlib import Path


WriteOperation: TypeAlias = Callable[[sqlite3.Connection], None]
PutBlob: TypeAlias = Callable[[BlobWrite], bool]
BlobMatches: TypeAlias = Callable[["Path", str, int], bool]


@dataclass(frozen=True, slots=True)
class MigrationContext:
    root: Path
    blob_dir: Path
    meta_dir: Path
    put_blob: PutBlob
    write_tx: Callable[[WriteOperation], None]
    blob_matches: BlobMatches


def migrate_legacy_indexes(store: MigrationContext) -> None:
    marker = store.root / MIGRATION_MARKER
    if marker.exists():
        return
    quarantine = store.root / "migration-quarantine"
    quarantine.mkdir(exist_ok=True)
    for index_file in sorted(store.meta_dir.glob("*.json")):
        try:
            idx = read_idx_file(index_file)
            if not idx and index_file.exists() and index_file.read_text(encoding="utf-8").strip():
                _ = shutil.copy2(index_file, quarantine / index_file.name)
                continue
        except OSError:
            continue
        for handle, entry in idx.get("blobs", {}).items():
            _migrate_entry(store, index_file.stem, handle, entry)
    _ = marker.write_text("ok\n", encoding="utf-8")


def _migrate_entry(
    store: MigrationContext, session_key: str, handle: str, entry: BlobEntry
) -> None:
    if SWEPT_KEY in entry:
        _insert_tombstone(store, session_key, handle, entry)
        return
    blob = store.blob_dir / handle
    full_hash = str(entry.get(HASH_KEY, ""))
    size = int(entry.get(SIZE_KEY, 0))
    if (
        not blob.exists()
        or not full_hash.startswith(handle)
        or not store.blob_matches(blob, full_hash, size)
    ):
        return
    raw = blob.read_bytes()
    _ = store.put_blob(
        BlobWrite(raw, handle, full_hash, size, session_key, str(entry.get(TOOL_KEY, "")))
    )


def _insert_tombstone(
    store: MigrationContext, session_key: str, handle: str, entry: BlobEntry
) -> None:
    now = float(entry.get(SWEPT_KEY, time.time()))

    def operation(conn: sqlite3.Connection) -> None:
        _ = conn.execute(
            """
            INSERT OR IGNORE INTO blobs(handle, full_hash, size, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (handle, handle + "0" * 52, int(entry.get(SIZE_KEY, 0)), now),
        )
        _ = conn.execute(
            "INSERT OR IGNORE INTO sessions(session_key, created_at) VALUES (?, ?)",
            (session_key, now),
        )
        _ = conn.execute(
            """
            INSERT OR REPLACE INTO ownership(
              session_key, handle, state, tool, created_at, swept_at, reason
            )
            VALUES (?, ?, 'tombstone', ?, ?, ?, 'legacy_tombstone')
            """,
            (session_key, handle, str(entry.get(TOOL_KEY, "")), now, now),
        )

    store.write_tx(operation)
