from __future__ import annotations

import hashlib
import sqlite3
from multiprocessing import get_context
from queue import Empty, Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING, Protocol, cast

from tokenjuice_hermes.rescue_index import safe_sid
from tokenjuice_hermes.rescue_sqlite import OwnershipStore
from tokenjuice_hermes.rescue_sqlite_types import BlobWrite
from tokenjuice_hermes.rescue_store import BlobStore

if TYPE_CHECKING:
    from pathlib import Path


class ProcessBarrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


class ProcessOutput(Protocol):
    def put(self, item: tuple[bool, str, str]) -> None: ...

    def get(self, timeout: float | None = None) -> tuple[bool, str, str]: ...


def _put_worker(
    store_path: Path, session_id: str, content: str, results: Queue[tuple[str, str]]
) -> None:
    handle = BlobStore({"store_path": str(store_path)}).put(
        content,
        tool_name="web_search",
        session_id=session_id,
    )
    results.put((handle, content))


def _process_put_worker(
    store_path: str,
    index: int,
    barrier: ProcessBarrier,
    output: ProcessOutput,
) -> None:
    try:
        _ = barrier.wait(timeout=10)
        handle = BlobStore({"store_path": store_path}).put(
            f"process content {index}",
            tool_name="web_search",
            session_id="session-a",
        )
        output.put((True, handle, ""))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        output.put((False, "", repr(exc)))


def test_concurrent_puts_from_multiple_store_instances_preserve_same_session_ownership(
    tmp_path: Path,
) -> None:
    writer_count = 24
    results: Queue[tuple[str, str]] = Queue()
    threads = [
        Thread(
            target=_put_worker,
            args=(tmp_path, "session-a", f"content {index}", results),
        )
        for index in range(writer_count)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    written = [results.get_nowait() for _ in range(writer_count)]
    restarted = BlobStore({"store_path": str(tmp_path)})
    for handle, content in written:
        assert restarted.fetch(handle, mode="full", session_id="session-a") == content


def test_cold_start_processes_preserve_same_session_ownership(tmp_path: Path) -> None:
    process_count = 12
    context = get_context("fork")
    barrier = context.Barrier(process_count)
    output = context.Queue()
    processes = [
        context.Process(target=_process_put_worker, args=(str(tmp_path), index, barrier, output))
        for index in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    rows: list[tuple[bool, str, str]] = []
    for _index in range(process_count):
        try:
            rows.append(cast("tuple[bool, str, str]", output.get(timeout=5)))
        except Empty:
            rows.append((False, "", "queue-empty"))
    assert [process.exitcode for process in processes] == [0] * process_count
    assert [row for row in rows if not row[0]] == []
    restarted = BlobStore({"store_path": str(tmp_path)})
    for _ok, handle, _error in rows:
        assert restarted.fetch(handle, mode="full", session_id="session-a").startswith(
            "process content"
        )


def test_restart_fetchability_and_cross_session_isolation(tmp_path: Path) -> None:
    store = BlobStore({"store_path": str(tmp_path)})
    handle_a = store.put("alpha", tool_name="web_search", session_id="session-a")
    handle_b = store.put("bravo", tool_name="web_search", session_id="session-b")

    restarted = BlobStore({"store_path": str(tmp_path)})

    assert restarted.fetch(handle_a, mode="full", session_id="session-a") == "alpha"
    assert restarted.fetch(handle_b, mode="full", session_id="session-b") == "bravo"
    assert "not available in this session" in restarted.fetch(
        handle_a,
        mode="full",
        session_id="session-b",
    )


def test_sweep_and_same_content_put_never_leave_live_missing_blob(tmp_path: Path) -> None:
    store = BlobStore({"store_path": str(tmp_path), "ttl_hours": 0, "tombstone_ttl_hours": 24})
    handle = store.put("same content", tool_name="web_search", session_id="session-a")
    barrier = Barrier(2)
    results: Queue[str] = Queue()

    def sweep() -> None:
        _ = barrier.wait(timeout=10)
        store.lazy_sweep()
        results.put("swept")

    def put_again() -> None:
        _ = barrier.wait(timeout=10)
        results.put(
            BlobStore({"store_path": str(tmp_path)}).put(
                "same content", tool_name="web_search", session_id="session-a"
            )
        )

    threads = [Thread(target=sweep), Thread(target=put_again)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert results.qsize() == 2
    fetched = BlobStore({"store_path": str(tmp_path)}).fetch(
        handle, mode="full", session_id="session-a"
    )
    assert fetched == "same content" or fetched.startswith("[Swept]")


def test_prefix_collision_fails_closed_without_cross_authorizing(tmp_path: Path) -> None:
    store = BlobStore({"store_path": str(tmp_path)})
    handle = "aaaaaaaaaaaa"
    alpha_hash = hashlib.sha256(b"alpha").hexdigest()
    bravo_hash = hashlib.sha256(b"bravo").hexdigest()

    ownership = OwnershipStore(tmp_path)
    accepted_a = ownership.put_blob(
        BlobWrite(b"alpha", handle, alpha_hash, 5, safe_sid("session-a"), "web_search")
    )
    accepted_b = ownership.put_blob(
        BlobWrite(b"bravo", handle, bravo_hash, 5, safe_sid("session-b"), "web_search")
    )

    assert accepted_a is True
    assert accepted_b is False
    assert store.fetch(handle, mode="full", session_id="session-a") == "alpha"
    assert "not available in this session" in store.fetch(
        handle, mode="full", session_id="session-b"
    )


def test_corrupt_existing_blob_is_not_served_or_cross_authorized(tmp_path: Path) -> None:
    store = BlobStore({"store_path": str(tmp_path)})
    handle = store.put("same content", tool_name="web_search", session_id="session-a")
    _ = (tmp_path / "blobs" / handle).write_text("BAD", encoding="utf-8")

    corrupted_fetch = store.fetch(handle, mode="full", session_id="session-a")
    repaired_handle = BlobStore({"store_path": str(tmp_path)}).put(
        "same content", tool_name="web_search", session_id="session-b"
    )

    assert corrupted_fetch != "BAD"
    assert repaired_handle == handle
    assert (
        BlobStore({"store_path": str(tmp_path)}).fetch(handle, mode="full", session_id="session-b")
        == "same content"
    )


def test_reconcile_tombstones_missing_and_mismatched_live_blobs(tmp_path: Path) -> None:
    store = BlobStore({"store_path": str(tmp_path)})
    missing = store.put("missing data", tool_name="web_search", session_id="session-a")
    mismatch = store.put("mismatch data", tool_name="web_search", session_id="session-a")
    orphan = hashlib.sha256(b"orphan").hexdigest()[:12]
    (tmp_path / "blobs" / missing).unlink()
    _ = (tmp_path / "blobs" / mismatch).write_text("MISMATCH", encoding="utf-8")
    _ = (tmp_path / "blobs" / orphan).write_text("orphan", encoding="utf-8")

    stats = OwnershipStore(tmp_path).reconcile()

    assert stats.missing_live_tombstoned == 1
    assert stats.integrity_tombstoned == 1
    assert stats.orphan_blobs_quarantined == 1
    assert "[Swept]" in store.fetch(missing, mode="full", session_id="session-a")
    assert "[Swept]" in store.fetch(mismatch, mode="full", session_id="session-a")
    assert not (tmp_path / "blobs" / orphan).exists()
