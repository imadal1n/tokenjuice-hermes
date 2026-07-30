from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest

from tokenjuice_hermes.observability import tokenjuice_status
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


class TrackedConnectionProtocol(Protocol):
    def execute(self, sql: str, /) -> sqlite3.Cursor: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ConnectionTracker:
    connections: list[TrackedConnectionProtocol]

    @property
    def closed_count(self) -> int:
        return sum(_is_closed(connection) for connection in self.connections)


def _track_connections(monkeypatch: MonkeyPatch) -> ConnectionTracker:
    original_connect = sqlite3.connect
    tracker = ConnectionTracker(connections=[])

    def tracked_connect(
        database: str | bytes | Path,
        *,
        timeout: float,
        isolation_level: None,
    ) -> sqlite3.Connection:
        connection = original_connect(
            database,
            timeout=timeout,
            isolation_level=isolation_level,
        )
        tracker.connections.append(connection)
        return connection

    monkeypatch.setattr("tokenjuice_hermes.rescue_sqlite.sqlite3.connect", tracked_connect)
    return tracker


def _closed_by_phase(tracker: ConnectionTracker) -> tuple[int, int]:
    return len(tracker.connections), tracker.closed_count


class FailingSetupConnection:
    def __init__(self) -> None:
        self.closed: bool = False

    def execute(self, sql: str, /) -> sqlite3.Cursor:
        if sql == "PRAGMA foreign_keys=ON":
            message = "simulated pragma setup failure"
            raise sqlite3.OperationalError(message)
        message = "unexpected SQL after setup failure"
        raise AssertionError(message)

    def close(self) -> None:
        self.closed = True


def _is_closed(connection: TrackedConnectionProtocol) -> bool:
    if isinstance(connection, FailingSetupConnection):
        return connection.closed
    try:
        connection.execute("SELECT 1").close()
    except sqlite3.ProgrammingError:
        return True
    return False


def test_rescue_sqlite_closes_connections_after_store_reads_and_writes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Given: the rescue SQLite store is instrumented with real close tracking.
    tracker = _track_connections(monkeypatch)

    # When: initialization, write, read, stats, and sweep paths run.
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("payload", tool_name="web_search", session_id="session-a")
    assert store.fetch(handle, mode="full", session_id="session-a") == "payload"
    assert '"live_blob_count":1' in tokenjuice_status({}, store_path=str(tmp_path))
    store.lazy_sweep()

    # Then: every opened SQLite connection has been deterministically closed.
    opened, closed = _closed_by_phase(tracker)
    assert opened > 0
    assert closed == opened


def test_rescue_sqlite_closes_connections_when_write_operation_raises(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Given: a rescue SQLite store and a write operation that fails mid-transaction.
    tracker = _track_connections(monkeypatch)
    store = BlobStore({"store_path": str(tmp_path)})
    opened_before_failure, _closed_before_failure = _closed_by_phase(tracker)

    def fail_write(_self: object, _path: Path, _data: bytes) -> None:
        message = "simulated transaction failure"
        raise RuntimeError(message)

    monkeypatch.setattr("tokenjuice_hermes.rescue_sqlite.OwnershipStore._atomic_write", fail_write)

    # When: the transaction rolls back after the operation raises.
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        _ = store.put("failing payload", tool_name="web_search", session_id="session-a")

    # Then: the failing transaction connection is closed as well.
    opened, closed = _closed_by_phase(tracker)
    assert opened == opened_before_failure + 1
    assert closed == opened


def test_rescue_sqlite_closes_connections_across_locked_retry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Given: a transaction operation reports one lock before succeeding on retry.
    tracker = _track_connections(monkeypatch)
    store = BlobStore({"store_path": str(tmp_path)})
    attempts = 0

    def locked_once(_self: object, path: Path, data: bytes) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "database is locked"
            raise sqlite3.OperationalError(message)
        _ = path.write_bytes(data)

    monkeypatch.setattr("tokenjuice_hermes.rescue_sqlite.OwnershipStore._atomic_write", locked_once)

    # When: the write transaction retries and succeeds.
    handle = store.put("retry payload", tool_name="web_search", session_id="session-a")

    # Then: both the failed-attempt connection and retry connection are closed.
    assert attempts == 2
    assert store.fetch(handle, mode="full", session_id="session-a") == "retry payload"
    opened, closed = _closed_by_phase(tracker)
    assert closed == opened


def test_rescue_sqlite_closes_connection_when_connect_setup_raises(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Given: sqlite connection acquisition succeeds but setup PRAGMA fails.
    tracker = ConnectionTracker(connections=[])

    def connect_with_setup_failure(
        database: str | bytes | Path,
        *,
        timeout: float,
        isolation_level: None,
    ) -> FailingSetupConnection:
        _ = database
        _ = timeout
        _ = isolation_level
        connection = FailingSetupConnection()
        tracker.connections.append(connection)
        return connection

    monkeypatch.setattr(
        "tokenjuice_hermes.rescue_sqlite.sqlite3.connect",
        connect_with_setup_failure,
    )

    # When: store initialization hits the setup failure before returning the connection.
    with pytest.raises(sqlite3.OperationalError, match="simulated pragma setup failure"):
        _ = BlobStore({"store_path": str(tmp_path)})

    # Then: the acquired connection is closed before the setup exception propagates.
    opened, closed = _closed_by_phase(tracker)
    assert opened == 1
    assert closed == opened
