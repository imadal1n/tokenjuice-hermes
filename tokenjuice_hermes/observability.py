"""Safe, aggregate-only observability for tokenjuice-hermes.

All counters are kept in memory for the process lifetime only. The status surface
is read-only and never returns raw blob content, raw session IDs, per-session
rows, secrets, transcript snippets, or private paths. Store statistics are
computed on demand by scanning the rescue store directories without creating
them.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from .rescue_handles import is_valid_handle
from .rescue_index import read_idx_file
from .rescue_sqlite import store_stats
from .rescue_sqlite_types import DB_NAME

VERSION: Final[str] = "0.1.0"


class _Counters:
    """In-memory, process-lifetime counters."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "compaction_chars_saved",
        "compaction_count",
        "fetch_count",
        "fetch_modes",
        "passref_budget_exceeded_count",
        "passref_chars_expanded",
        "passref_denied_count",
        "passref_enabled",
        "passref_expansion_count",
        "passref_truncated_count",
        "rescue_chars_saved",
        "rescue_count",
        "structured_pruning_attempted_count",
        "structured_pruning_config_rejected_count",
        "structured_pruning_count",
        "structured_pruning_insufficient_eligible_savings",
        "structured_pruning_rescued_count",
        "structured_pruning_saved_tokens",
    )

    def __init__(self) -> None:
        self.compaction_count: int = 0
        self.compaction_chars_saved: int = 0
        self.rescue_count: int = 0
        self.rescue_chars_saved: int = 0
        self.fetch_count: int = 0
        self.fetch_modes: dict[str, int] = {}
        self.passref_enabled: bool = False
        self.passref_expansion_count: int = 0
        self.passref_denied_count: int = 0
        self.passref_truncated_count: int = 0
        self.passref_budget_exceeded_count: int = 0
        self.passref_chars_expanded: int = 0
        self.structured_pruning_attempted_count: int = 0
        self.structured_pruning_config_rejected_count: int = 0
        self.structured_pruning_count: int = 0
        self.structured_pruning_rescued_count: int = 0
        self.structured_pruning_insufficient_eligible_savings: int = 0
        self.structured_pruning_saved_tokens: int = 0


_COUNTERS: Final[_Counters] = _Counters()


@dataclass(frozen=True, slots=True)
class StructuredPruningStats:
    attempted_count: int = 0
    rescued_count: int = 0
    insufficient_eligible_savings: int = 0


def reset_stats() -> None:
    """Reset all counters. Intended for tests only."""
    _COUNTERS.compaction_count = 0
    _COUNTERS.compaction_chars_saved = 0
    _COUNTERS.rescue_count = 0
    _COUNTERS.rescue_chars_saved = 0
    _COUNTERS.fetch_count = 0
    _COUNTERS.fetch_modes.clear()
    _COUNTERS.passref_enabled = False
    _COUNTERS.passref_expansion_count = 0
    _COUNTERS.passref_denied_count = 0
    _COUNTERS.passref_truncated_count = 0
    _COUNTERS.passref_budget_exceeded_count = 0
    _COUNTERS.passref_chars_expanded = 0
    _COUNTERS.structured_pruning_attempted_count = 0
    _COUNTERS.structured_pruning_config_rejected_count = 0
    _COUNTERS.structured_pruning_count = 0
    _COUNTERS.structured_pruning_rescued_count = 0
    _COUNTERS.structured_pruning_insufficient_eligible_savings = 0
    _COUNTERS.structured_pruning_saved_tokens = 0


def record_compaction(chars_saved: int) -> None:
    """Record a successful terminal compaction."""
    with contextlib.suppress(Exception):
        _COUNTERS.compaction_count += 1
        if chars_saved > 0:
            _COUNTERS.compaction_chars_saved += chars_saved


def record_rescue(chars_saved: int) -> None:
    """Record a successful rescue transformation."""
    with contextlib.suppress(Exception):
        _COUNTERS.rescue_count += 1
        if chars_saved > 0:
            _COUNTERS.rescue_chars_saved += chars_saved


def record_fetch(mode: str) -> None:
    """Record a rescuer_fetch invocation."""
    with contextlib.suppress(Exception):
        _COUNTERS.fetch_count += 1
        _COUNTERS.fetch_modes[mode] = _COUNTERS.fetch_modes.get(mode, 0) + 1


def record_passref_enabled(*, enabled: bool) -> None:
    """Record whether passref is enabled in the current process."""
    with contextlib.suppress(Exception):
        _COUNTERS.passref_enabled = enabled


def record_passref_expansion(chars_expanded: int) -> None:
    """Record a successful passref expansion."""
    with contextlib.suppress(Exception):
        _COUNTERS.passref_expansion_count += 1
        if chars_expanded > 0:
            _COUNTERS.passref_chars_expanded += chars_expanded


def record_passref_denial() -> None:
    """Record a passref request that was denied."""
    with contextlib.suppress(Exception):
        _COUNTERS.passref_denied_count += 1


def record_passref_truncation() -> None:
    """Record a passref expansion that was truncated."""
    with contextlib.suppress(Exception):
        _COUNTERS.passref_truncated_count += 1


def record_passref_budget_exceeded() -> None:
    """Record a passref expansion that hit the total budget marker."""
    with contextlib.suppress(Exception):
        _COUNTERS.passref_budget_exceeded_count += 1


def record_structured_pruning(
    *,
    pruned_count: int,
    saved_tokens: int,
    phase: str = "",
    redacted: list[dict[str, object]] | None = None,
    stats: StructuredPruningStats | None = None,
) -> None:
    """Record aggregate structured pruning counters.

    The ``redacted`` argument is accepted for caller convenience but is never
    stored or returned in status; only aggregate counts cross the boundary.
    """
    _ = phase
    _ = redacted
    current_stats = stats or StructuredPruningStats()
    with contextlib.suppress(Exception):
        if current_stats.attempted_count > 0:
            _COUNTERS.structured_pruning_attempted_count += current_stats.attempted_count
        if pruned_count > 0:
            _COUNTERS.structured_pruning_count += pruned_count
        if current_stats.rescued_count > 0:
            _COUNTERS.structured_pruning_rescued_count += current_stats.rescued_count
        if current_stats.insufficient_eligible_savings > 0:
            _COUNTERS.structured_pruning_insufficient_eligible_savings += (
                current_stats.insufficient_eligible_savings
            )
        if saved_tokens > 0:
            _COUNTERS.structured_pruning_saved_tokens += saved_tokens


def record_structured_pruning_config_rejected() -> None:
    with contextlib.suppress(Exception):
        _COUNTERS.structured_pruning_config_rejected_count += 1


def _count_index_entries(meta_dir: Path) -> tuple[int, int]:
    """Return (live_count, tombstone_count) across all session indices."""
    store_root = meta_dir.parent
    if (store_root / DB_NAME).exists():
        stats = store_stats(store_root)
        return stats.live_count, stats.tombstone_count
    live_count = 0
    tombstone_count = 0
    try:
        if not meta_dir.exists():
            return live_count, tombstone_count
        for ip in sorted(meta_dir.glob("*.json")):
            idx = read_idx_file(ip)
            for entry in idx.get("blobs", {}).values():
                if "swept_at" in entry:
                    tombstone_count += 1
                else:
                    live_count += 1
    except OSError:
        pass
    return live_count, tombstone_count


def _sum_blob_sizes(blob_dir: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for blobs on disk."""
    total_bytes = 0
    file_count = 0
    try:
        if not blob_dir.exists():
            return file_count, total_bytes
        for bf in blob_dir.iterdir():
            if not bf.is_file() or not is_valid_handle(bf.name):
                continue
            try:
                st = bf.stat()
                total_bytes += st.st_size
                file_count += 1
            except OSError:
                continue
    except OSError:
        pass
    return file_count, total_bytes


def _compute_store_stats(store_path: str) -> dict[str, int]:
    """Read aggregate store statistics without creating directories."""
    try:
        base = Path(store_path).expanduser().resolve()
    except (OSError, ValueError):
        return _empty_store_stats()

    try:
        live_count, tombstone_count = _count_index_entries(base / "sessions")
        file_count, total_bytes = _sum_blob_sizes(base / "blobs")
    except (OSError, sqlite3.Error):
        return _empty_store_stats()

    return {
        "live_blob_count": live_count,
        "tombstone_count": tombstone_count,
        "total_blob_bytes": total_bytes,
        "blob_file_count": file_count,
    }


def _empty_store_stats() -> dict[str, int]:
    return {"live_blob_count": 0, "tombstone_count": 0, "total_blob_bytes": 0, "blob_file_count": 0}


def status_snapshot(store_path: str = "") -> dict[str, object]:
    """Return an aggregate, redacted status snapshot."""
    store_stats = _compute_store_stats(store_path) if store_path else _empty_store_stats()
    return {
        "version": VERSION,
        "passref_enabled": _COUNTERS.passref_enabled,
        "compaction_count": _COUNTERS.compaction_count,
        "compaction_chars_saved": _COUNTERS.compaction_chars_saved,
        "rescue_count": _COUNTERS.rescue_count,
        "rescue_chars_saved": _COUNTERS.rescue_chars_saved,
        "fetch_count": _COUNTERS.fetch_count,
        "fetch_modes": dict(_COUNTERS.fetch_modes),
        "passref_expansion_count": _COUNTERS.passref_expansion_count,
        "passref_denied_count": _COUNTERS.passref_denied_count,
        "passref_truncated_count": _COUNTERS.passref_truncated_count,
        "passref_budget_exceeded_count": _COUNTERS.passref_budget_exceeded_count,
        "passref_chars_expanded": _COUNTERS.passref_chars_expanded,
        "structured_pruning_attempted_count": _COUNTERS.structured_pruning_attempted_count,
        "structured_pruning_config_rejected_count": (
            _COUNTERS.structured_pruning_config_rejected_count
        ),
        "structured_pruning_count": _COUNTERS.structured_pruning_count,
        "structured_pruning_rescued_count": _COUNTERS.structured_pruning_rescued_count,
        "structured_pruning_insufficient_eligible_savings": (
            _COUNTERS.structured_pruning_insufficient_eligible_savings
        ),
        "structured_pruning_saved_tokens": _COUNTERS.structured_pruning_saved_tokens,
        "store": store_stats,
    }


def tokenjuice_status(_args: dict[str, object], *, store_path: str = "") -> str:
    """Read-only aggregate status tool callable by a Hermes host."""
    with contextlib.suppress(Exception):
        snapshot = status_snapshot(store_path)
        return json.dumps(snapshot, separators=(",", ":"))
    return json.dumps({"error": "tokenjuice_status unavailable"})
