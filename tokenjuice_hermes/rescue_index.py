"""Session index persistence for the rescue blob store.

The index maps a stable session ID to the set of blob handles that session is
allowed to redeem. Index files are JSON and all writes are atomic
(temp file + replace).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict, cast

LIVE_KEY: str = "t"
SWEPT_KEY: str = "swept_at"
TOOL_KEY: str = "tool"
SIZE_KEY: str = "size"
HASH_KEY: str = "hash"


class BlobEntry(TypedDict, total=False):
    t: float
    swept_at: float
    tool: str
    size: int
    hash: str


class SessionIndex(TypedDict, total=False):
    blobs: dict[str, BlobEntry]


def is_live(entry: BlobEntry) -> bool:
    """Return True when *entry* has not been tombstoned."""
    return SWEPT_KEY not in entry


def safe_sid(session_id: str) -> str:
    """Return a filesystem-safe slug derived from *session_id*."""
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:32]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def idx_path(meta_dir: Path, session_id: str) -> Path:
    """Path to the JSON index file for *session_id*."""
    return meta_dir / f"{safe_sid(session_id)}.json"


def read_idx_file(path: Path) -> SessionIndex:
    """Read a session index, returning an empty dict on any error."""
    try:
        if not path.exists():
            return {}
        data = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        return cast("SessionIndex", cast("object", data))
    return {}


def write_idx_file(path: Path, idx: SessionIndex) -> None:
    """Atomically write *idx* to *path*."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        _ = tmp.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            _ = tmp.unlink()
        raise


def load_idx(meta_dir: Path, session_id: str) -> SessionIndex:
    """Load the index for *session_id*."""
    return read_idx_file(idx_path(meta_dir, session_id))


def save_idx(meta_dir: Path, idx: SessionIndex, session_id: str) -> None:
    """Persist *idx* for *session_id*."""
    write_idx_file(idx_path(meta_dir, session_id), idx)


def find_meta(meta_dir: Path, handle: str, session_id: str = "") -> BlobEntry:
    """Find live metadata for *handle*, preferring the owning session."""
    paths = [idx_path(meta_dir, session_id)] if session_id else []
    paths.extend(sorted(meta_dir.glob("*.json")))
    for ip in paths:
        meta = read_idx_file(ip).get("blobs", {}).get(handle)
        if meta and is_live(meta):
            return meta
    return {}


def tombstone_message(meta_dir: Path, handle: str, session_id: str = "") -> str | None:
    """Return a swept message for *handle* if it is tombstoned for *session_id*."""
    if not session_id:
        return None
    meta = read_idx_file(idx_path(meta_dir, session_id)).get("blobs", {}).get(handle)
    if not meta or is_live(meta):
        return None
    tool = meta.get(TOOL_KEY, "the source tool")
    size = meta.get(SIZE_KEY, 0)
    return (
        f"[Swept] Handle {handle} (from {tool}, {size:,} chars) expired "
        f"after the retention window. Re-run {tool} to regenerate it."
    )
