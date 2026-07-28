from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, cast

if TYPE_CHECKING:
    from pathlib import Path

from .rescue_sqlite_types import (
    BlobRow,
    BlobSweepRow,
    CountRow,
    ExistsRow,
    HandleRow,
    ReconcileStats,
    StoreStats,
)

WriteOperation: TypeAlias = Callable[[sqlite3.Connection], None]
Connect: TypeAlias = Callable[[], sqlite3.Connection]
BlobMatches: TypeAlias = Callable[["Path", str, int], bool]


@dataclass(frozen=True, slots=True)
class MaintenanceContext:
    root: Path
    blob_dir: Path
    connect: Connect
    write_tx: Callable[[WriteOperation], None]
    blob_matches: BlobMatches


def sweep(store: MaintenanceContext, now: float, ttl: float, tomb_ttl: float, max_mb: int) -> None:
    max_bytes = max_mb * 1024 * 1024

    def operation(conn: sqlite3.Connection) -> None:
        _ = conn.execute(
            """
            UPDATE ownership SET state='tombstone', swept_at=?, reason='ttl'
            WHERE state='live' AND ? - created_at > ?
            """,
            (now, now, ttl),
        )
        _ = conn.execute(
            """
            DELETE FROM ownership
            WHERE state='tombstone' AND swept_at IS NOT NULL AND ? - swept_at > ?
            """,
            (now, tomb_ttl),
        )
        rows = cast(
            "list[BlobSweepRow]",
            list(conn.execute("SELECT handle, size, created_at FROM blobs ORDER BY created_at")),
        )
        total = sum(int(row[1] or 0) for row in rows if (store.blob_dir / str(row[0])).exists())
        for handle, size, _created_at in rows:
            if total <= max_bytes:
                break
            _ = conn.execute(
                """
                UPDATE ownership SET state='tombstone', swept_at=?, reason='size'
                WHERE handle=? AND state='live'
                """,
                (now, handle),
            )
            total -= int(size or 0)
        for (handle,) in cast("list[HandleRow]", list(conn.execute("SELECT handle FROM blobs"))):
            live = cast(
                "ExistsRow | None",
                conn.execute(
                    "SELECT 1 FROM ownership WHERE handle=? AND state='live' LIMIT 1",
                    (handle,),
                ).fetchone(),
            )
            if live is None:
                with contextlib.suppress(FileNotFoundError):
                    (store.blob_dir / str(handle)).unlink()

    store.write_tx(operation)


def stats(store: MaintenanceContext) -> StoreStats:
    with store.connect() as conn:
        live = cast(
            "CountRow | None",
            conn.execute("SELECT count(*) FROM ownership WHERE state='live'").fetchone(),
        )
        tomb = cast(
            "CountRow | None",
            conn.execute("SELECT count(*) FROM ownership WHERE state='tombstone'").fetchone(),
        )
    return StoreStats(
        live_count=int(live[0] if live else 0), tombstone_count=int(tomb[0] if tomb else 0)
    )


def reconcile(store: MaintenanceContext) -> ReconcileStats:
    missing = 0
    mismatch = 0
    orphan = 0

    def operation(conn: sqlite3.Connection) -> None:
        nonlocal missing, mismatch, orphan
        rows = cast(
            "list[BlobRow]", list(conn.execute("SELECT handle, full_hash, size FROM blobs"))
        )
        known = {str(row[0]) for row in rows}
        for handle, full_hash, size in rows:
            path = store.blob_dir / str(handle)
            live = cast(
                "ExistsRow | None",
                conn.execute(
                    "SELECT 1 FROM ownership WHERE handle=? AND state='live' LIMIT 1", (handle,)
                ).fetchone(),
            )
            if live is None:
                continue
            if not path.exists():
                missing += 1
                _ = conn.execute(
                    """
                    UPDATE ownership SET state='tombstone', swept_at=?, reason='missing_blob'
                    WHERE handle=? AND state='live'
                    """,
                    (time.time(), handle),
                )
            elif not store.blob_matches(path, str(full_hash), int(size)):
                mismatch += 1
                _ = conn.execute(
                    """
                    UPDATE ownership
                    SET state='tombstone', swept_at=?, reason='integrity_mismatch'
                    WHERE handle=? AND state='live'
                    """,
                    (time.time(), handle),
                )
        for blob in store.blob_dir.iterdir():
            if blob.is_file() and blob.name not in known:
                orphan += 1
                _ = blob.rename(store.root / f"orphan-{blob.name}")

    store.write_tx(operation)
    return ReconcileStats(missing, mismatch, orphan)
