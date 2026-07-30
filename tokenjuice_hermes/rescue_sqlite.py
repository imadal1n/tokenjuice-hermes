from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

from .rescue_index import safe_sid
from .rescue_sqlite_maintenance import MaintenanceContext
from .rescue_sqlite_maintenance import reconcile as reconcile_store
from .rescue_sqlite_maintenance import stats as store_stats_for
from .rescue_sqlite_maintenance import sweep as sweep_store
from .rescue_sqlite_migration import MigrationContext, migrate_legacy_indexes
from .rescue_sqlite_schema import initialize_database
from .rescue_sqlite_types import (
    BUSY_TIMEOUT_MS,
    DB_NAME,
    BlobMeta,
    BlobWrite,
    ReconcileStats,
    StoreStats,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@final
class OwnershipStore:
    def __init__(self, store_root: Path) -> None:
        """Open the SQLite ownership index under *store_root*."""
        self.root = store_root
        self.blob_dir = store_root / "blobs"
        self.meta_dir = store_root / "sessions"
        self.db_path = store_root / DB_NAME
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.migrate_legacy_indexes()

    def put(self, content: str, tool_name: str, session_id: str) -> str:
        raw = content.encode("utf-8")
        full_hash = hashlib.sha256(raw).hexdigest()
        handle = full_hash[:12]
        accepted = self.put_blob(
            BlobWrite(raw, handle, full_hash, len(raw), safe_sid(session_id), tool_name)
        )
        if not accepted:
            message = "rescue handle collision"
            raise ValueError(message)
        return handle

    def put_blob(self, blob: BlobWrite) -> bool:
        path = self.blob_dir / blob.handle
        now = time.time()
        accepted = False

        def operation(conn: sqlite3.Connection) -> None:
            nonlocal accepted
            existing = cast(
                "tuple[str, int] | None",
                conn.execute(
                    "SELECT full_hash, size FROM blobs WHERE handle=?",
                    (blob.handle,),
                ).fetchone(),
            )
            if existing is not None and (existing[0] != blob.full_hash or existing[1] != blob.size):
                accepted = False
                return
            if existing is None or not self._blob_matches(path, blob.full_hash, blob.size):
                self._atomic_write(path, blob.raw)
                if not self._blob_matches(path, blob.full_hash, blob.size):
                    accepted = False
                    return
            if existing is None:
                _ = conn.execute(
                    """
                    INSERT INTO blobs(handle, full_hash, size, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (blob.handle, blob.full_hash, blob.size, now),
                )
            _ = conn.execute(
                "INSERT OR IGNORE INTO sessions(session_key, created_at) VALUES (?, ?)",
                (blob.session_key, now),
            )
            _ = conn.execute(
                """
                INSERT INTO ownership(
                  session_key, handle, state, tool, size, created_at, accessed_at, swept_at, reason
                )
                VALUES (?, ?, 'live', ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(session_key, handle) DO UPDATE SET
                  state='live', tool=excluded.tool, size=excluded.size,
                  accessed_at=excluded.accessed_at, swept_at=NULL, reason=NULL
                """,
                (blob.session_key, blob.handle, blob.tool_name, blob.size, now, now),
            )
            accepted = True

        self._write_tx(operation)
        return accepted

    def session_references(self, handle: str, session_id: str) -> bool:
        with contextlib.closing(self._connect()) as conn:
            row = cast(
                "tuple[str, int] | None",
                conn.execute(
                    """
                    SELECT blobs.full_hash, blobs.size
                    FROM ownership JOIN blobs USING(handle)
                    WHERE session_key=? AND handle=? AND state='live'
                    """,
                    (safe_sid(session_id), handle),
                ).fetchone(),
            )
        return row is not None and self._blob_matches(self.blob_dir / handle, row[0], row[1])

    def tombstone_message(self, handle: str, session_id: str) -> str | None:
        with contextlib.closing(self._connect()) as conn:
            row = cast(
                "tuple[str, int] | None",
                conn.execute(
                    """
                    SELECT ownership.tool, COALESCE(blobs.size, ownership.size, 0)
                    FROM ownership LEFT JOIN blobs USING(handle)
                    WHERE session_key=? AND handle=? AND state='tombstone'
                    """,
                    (safe_sid(session_id), handle),
                ).fetchone(),
            )
        if row is None:
            return None
        tool = row[0] or "the source tool"
        return (
            f"[Swept] Handle {handle} (from {tool}, {int(row[1] or 0):,} chars) expired "
            f"after the retention window. Re-run {tool} to regenerate it."
        )

    def find_meta(self, handle: str, session_id: str = "") -> BlobMeta:
        with contextlib.closing(self._connect()) as conn:
            if session_id:
                row = cast(
                    "tuple[str, int] | None",
                    conn.execute(
                        """
                        SELECT ownership.tool, blobs.size FROM ownership JOIN blobs USING(handle)
                        WHERE handle=? AND state='live' AND ownership.session_key=? LIMIT 1
                        """,
                        (handle, safe_sid(session_id)),
                    ).fetchone(),
                )
            else:
                row = cast(
                    "tuple[str, int] | None",
                    conn.execute(
                        """
                        SELECT ownership.tool, blobs.size FROM ownership JOIN blobs USING(handle)
                        WHERE handle=? AND state='live' LIMIT 1
                        """,
                        (handle,),
                    ).fetchone(),
                )
        if row is None:
            return BlobMeta()
        return BlobMeta(tool=str(row[0] or "?"), size=int(row[1] or 0))

    def sweep(self, now: float, ttl: float, tomb_ttl: float, max_mb: int) -> None:
        sweep_store(self._maintenance_context(), now, ttl, tomb_ttl, max_mb)

    def stats(self) -> StoreStats:
        return store_stats_for(self._maintenance_context())

    def reconcile(self) -> ReconcileStats:
        return reconcile_store(self._maintenance_context())

    def migrate_legacy_indexes(self) -> None:
        migrate_legacy_indexes(self._migration_context())

    def _maintenance_context(self) -> MaintenanceContext:
        return MaintenanceContext(
            root=self.root,
            blob_dir=self.blob_dir,
            connect=self._connect,
            write_tx=self._write_tx,
            blob_matches=self._blob_matches,
        )

    def _migration_context(self) -> MigrationContext:
        return MigrationContext(
            root=self.root,
            blob_dir=self.blob_dir,
            meta_dir=self.meta_dir,
            put_blob=self.put_blob,
            write_tx=self._write_tx,
            blob_matches=self._blob_matches,
        )

    def _init_db(self) -> None:
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                with contextlib.closing(self._connect()) as conn:
                    conn.execute("PRAGMA journal_mode=DELETE").close()
                    initialize_database(conn)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            else:
                return

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON").close()
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}").close()
        except BaseException:
            conn.close()
            raise
        else:
            return conn

    def _write_tx(self, operation: Callable[[sqlite3.Connection], None]) -> None:
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        delay = 0.01
        while True:
            with contextlib.closing(self._connect()) as conn:
                try:
                    _ = conn.execute("BEGIN IMMEDIATE")
                    operation(conn)
                    _ = conn.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    with contextlib.suppress(sqlite3.Error):
                        _ = conn.execute("ROLLBACK")
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 0.1)
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        _ = conn.execute("ROLLBACK")
                    raise
                else:
                    return

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp", suffix=".blob")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                _ = file.write(data)
            _ = tmp.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    @staticmethod
    def _blob_matches(path: Path, full_hash: str, size: int) -> bool:
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        return len(raw) == size and hashlib.sha256(raw).hexdigest() == full_hash


def store_stats(store_root: Path) -> StoreStats:
    db_path = store_root / DB_NAME
    if not db_path.exists():
        return StoreStats(0, 0)
    return OwnershipStore(store_root).stats()
