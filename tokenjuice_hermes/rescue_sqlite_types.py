from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

State: TypeAlias = Literal["live", "tombstone"]
CountRow: TypeAlias = tuple[int]
ExistsRow: TypeAlias = tuple[int]
BlobRow: TypeAlias = tuple[str, str, int]
BlobSweepRow: TypeAlias = tuple[str, int, float]
HandleRow: TypeAlias = tuple[str]

DB_NAME = "ownership.sqlite3"
MIGRATION_MARKER = "ownership.sqlite3.migrated"
BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True, slots=True)
class BlobMeta:
    tool: str = "?"
    size: int = 0


@dataclass(frozen=True, slots=True)
class StoreStats:
    live_count: int
    tombstone_count: int


@dataclass(frozen=True, slots=True)
class ReconcileStats:
    missing_live_tombstoned: int
    integrity_tombstoned: int
    orphan_blobs_quarantined: int


@dataclass(frozen=True, slots=True)
class BlobWrite:
    raw: bytes
    handle: str
    full_hash: str
    size: int
    session_key: str
    tool_name: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
  handle TEXT PRIMARY KEY CHECK(length(handle) = 12),
  full_hash TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ownership (
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
CREATE INDEX IF NOT EXISTS ownership_state_idx ON ownership(state, swept_at);
"""
