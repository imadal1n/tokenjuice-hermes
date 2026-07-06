"""Garbage collection for the rescue blob store.

Sweeping expires blobs past a TTL and enforces a maximum store size, oldest
first. Expired entries become tombstones in the session index; tombstones are
removed after a second TTL, and only then is the underlying blob content
eligible for deletion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .rescue_handles import is_valid_handle
from .rescue_index import (
    SIZE_KEY,
    SWEPT_KEY,
    TOOL_KEY,
    BlobEntry,
    is_live,
    read_idx_file,
    write_idx_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def sweep_by_ttl(meta_dir: Path, blob_dir: Path, now: float, ttl: float, tomb_ttl: float) -> None:
    """Tombstone live blobs past *ttl* and drop tombstones past *tomb_ttl*."""
    for ip in sorted(meta_dir.glob("*.json")):
        idx = read_idx_file(ip)
        blobs = idx.get("blobs", {})
        changed = False
        for bid, meta in list(blobs.items()):
            if not is_live(meta):
                if now - meta.get(SWEPT_KEY, 0) > tomb_ttl:
                    del blobs[bid]
                    changed = True
                continue
            if now - meta.get("t", 0) > ttl:
                tomb: BlobEntry = cast(
                    "BlobEntry",
                    cast(
                        "object",
                        {
                            SWEPT_KEY: now,
                            TOOL_KEY: meta.get(TOOL_KEY, ""),
                            SIZE_KEY: meta.get(SIZE_KEY, 0),
                        },
                    ),
                )
                blobs[bid] = tomb
                changed = True
        if changed:
            idx["blobs"] = blobs
            write_idx_file(ip, idx)

    for bf in blob_dir.iterdir():
        if bf.is_file() and is_valid_handle(bf.name) and not any_live_refs(meta_dir, bf.name):
            bf.unlink()


def sweep_by_size(meta_dir: Path, blob_dir: Path, now: float, max_mb: int) -> None:
    """Delete oldest blobs until the store is under *max_mb* megabytes."""
    max_bytes = max_mb * 1024 * 1024
    all_blobs: list[tuple[float, int, Path]] = []
    for bf in blob_dir.iterdir():
        if bf.is_file() and is_valid_handle(bf.name):
            try:
                st = bf.stat()
                all_blobs.append((st.st_ctime, st.st_size, bf))
            except OSError:
                continue
    all_blobs.sort()
    total = sum(sz for _, sz, _ in all_blobs)
    for _, _, bf in all_blobs:
        if total <= max_bytes:
            break
        tombstone_everywhere(meta_dir, bf.name, now)
        if bf.exists():
            bf.unlink()
        total -= bf.stat().st_size if bf.exists() else 0


def tombstone_everywhere(meta_dir: Path, handle: str, now: float) -> None:
    """Convert *handle* to a tombstone in every session index where it is live."""
    for ip in sorted(meta_dir.glob("*.json")):
        idx = read_idx_file(ip)
        blobs = idx.get("blobs", {})
        entry = blobs.get(handle)
        if entry and is_live(entry):
            tomb: BlobEntry = cast(
                "BlobEntry",
                cast(
                    "object",
                    {
                        SWEPT_KEY: now,
                        TOOL_KEY: entry.get(TOOL_KEY, ""),
                        SIZE_KEY: entry.get(SIZE_KEY, 0),
                    },
                ),
            )
            blobs[handle] = tomb
            idx["blobs"] = blobs
            write_idx_file(ip, idx)


def any_live_refs(meta_dir: Path, handle: str) -> bool:
    """Return True if any session index still holds *handle* as live."""
    for ip in meta_dir.glob("*.json"):
        entry = read_idx_file(ip).get("blobs", {}).get(handle)
        if entry and is_live(entry):
            return True
    return False
