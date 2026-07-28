"""Persistent blob store for rescued oversized tool results.

The store keeps full content addressed by an internal content hash and tracks
per-session ownership in a separate index. Model-visible handles are opaque
12-hex strings; authorization is always checked against the session index, so
a handle is never redeemable cross-session simply because the blob exists on
disk.

All metadata writes are atomic (temp file + atomic replace). Blob eviction
happens through ``lazy_sweep``, which turns expired entries into tombstones
before deleting content no session references.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import cast

from .rescue_handles import is_valid_handle, normalize_handle
from .rescue_sqlite import OwnershipStore
from .rescue_types import (
    DEFAULT_FETCH_MAX_CHARS,
    DEFAULT_FULL_FETCH_MAX_CHARS,
    DEFAULT_MAX_STORE_MB,
    DEFAULT_REFUSE_FULL_FETCH,
    DEFAULT_TOMBSTONE_TTL_HOURS,
    DEFAULT_TTL_HOURS,
    RESCUE_STORE_PATH_DEFAULT,
    BlobStoreConfig,
)


class BlobStore:
    """Stdlib-only persistent rescue blob store."""

    cfg: BlobStoreConfig
    _ownership: OwnershipStore
    blob_dir: Path
    meta_dir: Path

    def __init__(self, cfg: BlobStoreConfig | dict[str, object]) -> None:
        """Open or create the store rooted at the configured path."""
        self.cfg = cast("BlobStoreConfig", cfg)
        store_path = cfg.get("store_path", RESCUE_STORE_PATH_DEFAULT)
        if not isinstance(store_path, str):
            store_path = RESCUE_STORE_PATH_DEFAULT
        base = Path(store_path).expanduser().resolve()
        self.blob_dir = base / "blobs"
        self.meta_dir = base / "sessions"
        try:
            self.blob_dir.mkdir(parents=True, exist_ok=True)
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            self._ownership = OwnershipStore(base)
        except PermissionError:
            pass

    # ─── public put / ownership ─────────────────────────────────────────

    def put(self, content: str, tool_name: str = "", session_id: str = "") -> str:
        """Store *content* and return an opaque handle scoped to *session_id*.

        If *session_id* is empty, the content is not indexed and no
        model-visible handle is returned. The caller keeps the inline content
        recoverable.
        """
        if not session_id:
            return ""
        ownership = self._ownership_or_none()
        if ownership is None:
            return ""
        return ownership.put(content, tool_name, session_id)

    def session_references(self, handle: str, session_id: str) -> bool:
        """Return whether *session_id* owns a live (non-tombstoned) *handle*."""
        if not session_id or not is_valid_handle(handle):
            return False
        ownership = self._ownership_or_none()
        if ownership is None:
            return False
        return ownership.session_references(handle, session_id)

    def has_blob(self, handle: str) -> bool:
        """Return whether decoded blob content exists on disk."""
        bpath = self._blob_path(handle)
        return bpath is not None and bpath.exists()

    def blob_text(self, handle: str) -> str | None:
        """Return decoded blob content, or None if missing or not UTF-8."""
        bpath = self._blob_path(handle)
        if bpath is None:
            return None
        try:
            return bpath.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            return None

    # ─── public fetch ───────────────────────────────────────────────────

    def fetch(
        self,
        handle: str,
        mode: str,
        *,
        session_id: str = "",
        start: int = 0,
        count: int = 20,
    ) -> str:
        """Retrieve a slice of a blob.

        Modes:
            stat  - metadata about the blob
            full  - full decoded text (honours the full-fetch cap)
            range - lines ``start`` to ``start+count``
        """
        error = self._authorize(handle, session_id)
        if error is not None:
            return error
        if mode == "stat":
            return self._fetch_stat(handle, session_id)
        text = self._read_blob_text(handle, session_id)
        if mode == "full":
            return self._fetch_full(text)
        if mode == "range":
            return self._fetch_range(text, start, count)
        return f"Error: unknown mode '{mode}'"

    # ─── sweep / GC ─────────────────────────────────────────────────────

    def lazy_sweep(self) -> None:
        """Expire blobs past TTL or over the size limit, oldest first."""
        ttl = self.cfg.get("ttl_hours", DEFAULT_TTL_HOURS) * 3600
        tomb_ttl = self.cfg.get("tombstone_ttl_hours", DEFAULT_TOMBSTONE_TTL_HOURS) * 3600
        max_mb = self.cfg.get("max_store_mb", DEFAULT_MAX_STORE_MB)
        now = time.time()
        ownership = self._ownership_or_none()
        if ownership is not None:
            ownership.sweep(now, ttl, tomb_ttl, max_mb)

    # ─── internal helpers ───────────────────────────────────────────────

    def _authorize(self, handle: str, session_id: str) -> str | None:
        if normalize_handle(handle) is None:
            return "Error: invalid handle"
        if not session_id:
            return "Error: session ID required to fetch a handle"
        if not self.session_references(handle, session_id):
            ownership = self._ownership_or_none()
            tomb = (
                ownership.tombstone_message(handle, session_id) if ownership is not None else None
            )
            if tomb:
                return tomb
            return f"Error: handle {handle} not available in this session"
        return None

    def _ownership_or_none(self) -> OwnershipStore | None:
        try:
            return self._ownership
        except AttributeError:
            return None

    def _blob_path(self, handle: str) -> Path | None:
        if not is_valid_handle(handle):
            return None
        return self.blob_dir / handle

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp", suffix=".blob")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                _ = f.write(data)
            _ = tmp.replace(path)
        except Exception:
            with contextlib.suppress(OSError):
                _ = tmp.unlink()
            raise

    def _fetch_stat(self, handle: str, session_id: str) -> str:
        bpath = self._blob_path(handle)
        if bpath is None or not bpath.exists():
            return self._ownership.tombstone_message(handle, session_id) or (
                f"Error: handle {handle} not found (may have been swept)"
            )
        try:
            st = bpath.stat()
        except OSError:
            return self._ownership.tombstone_message(handle, session_id) or (
                f"Error: handle {handle} not found (may have been swept)"
            )
        meta = self._ownership.find_meta(handle, session_id)
        return (
            f"blob: {handle}\n"
            f"size: {st.st_size:,} bytes\n"
            f"stored: {time.ctime(st.st_ctime)}\n"
            f"tool: {meta.tool}"
        )

    def _read_blob_text(self, handle: str, session_id: str) -> str:
        bpath = self._blob_path(handle)
        if bpath is None or not bpath.exists():
            return self._ownership.tombstone_message(handle, session_id) or (
                f"Error: handle {handle} not found (may have been swept)"
            )
        try:
            raw = bpath.read_bytes()
        except FileNotFoundError:
            return self._ownership.tombstone_message(handle, session_id) or (
                f"Error: handle {handle} not found (may have been swept)"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: handle {handle} is binary ({len(raw)} bytes)"

    def _fetch_full(self, text: str) -> str:
        cap = self.cfg.get("full_fetch_max_chars", DEFAULT_FULL_FETCH_MAX_CHARS)
        if self.cfg.get("refuse_full_fetch", DEFAULT_REFUSE_FULL_FETCH) and len(text) > cap:
            return (
                f"Refused: blob is {len(text):,} chars, over the "
                f"tokenjuice_rescue_full_fetch_max_chars={cap:,} limit "
                "(tokenjuice_rescue_refuse_full_fetch=True). "
                "Use mode='range' with start/count, e.g. start=0,count=20, "
                "or mode='grep' with a literal pattern."
            )
        return text

    def _fetch_range(self, text: str, start: int, count: int) -> str:
        cap = self.cfg.get("fetch_max_chars", DEFAULT_FETCH_MAX_CHARS)
        lines = text.splitlines()
        total = len(lines)
        start = max(0, start)
        note = ""
        if 0 < total <= start:
            note = f"[start {start} past end; clamped]\n"
            start = max(0, total - max(1, count))
        end = min(total, start + max(1, count))
        body = "\n".join(lines[start:end])[:cap]
        return f"{note}[lines {start}..{end - 1} of {total}]\n{body}"
