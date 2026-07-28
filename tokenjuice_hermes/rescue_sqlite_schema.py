from __future__ import annotations

import sqlite3


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=OFF").close()
    conn.execute("BEGIN IMMEDIATE").close()
    try:
        _create_base_tables(conn)
        if not _table_exists(conn, "ownership"):
            _create_ownership(conn)
        elif _ownership_needs_rebuild(conn):
            _rebuild_ownership(conn)
        _create_indexes(conn)
        conn.execute("COMMIT").close()
    except BaseException:
        conn.execute("ROLLBACK").close()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON").close()


def _create_base_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
          session_key TEXT PRIMARY KEY,
          created_at REAL NOT NULL
        )
        """
    ).close()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blobs (
          handle TEXT PRIMARY KEY CHECK(length(handle) = 12),
          full_hash TEXT NOT NULL,
          size INTEGER NOT NULL,
          created_at REAL NOT NULL
        )
        """
    ).close()


def _create_ownership(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ownership (
          session_key TEXT NOT NULL,
          handle TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('live', 'tombstone')),
          tool TEXT NOT NULL,
          size INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,
          accessed_at REAL,
          swept_at REAL,
          reason TEXT,
          PRIMARY KEY (session_key, handle),
          FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
        )
        """
    ).close()


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ownership_state_idx ON ownership(state, swept_at)"
    ).close()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    try:
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def _ownership_needs_rebuild(conn: sqlite3.Connection) -> bool:
    return not _ownership_has_size(conn) or _ownership_rejects_tombstone_without_blob(conn)


def _ownership_has_size(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT size FROM ownership LIMIT 0").close()
    except sqlite3.OperationalError:
        return False
    return True


def _ownership_rejects_tombstone_without_blob(conn: sqlite3.Connection) -> bool:
    rejected = False
    conn.execute("SAVEPOINT ownership_fk_probe").close()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_key, created_at) VALUES (?, ?)",
            ("__schema_probe__", 0.0),
        ).close()
        conn.execute(
            """
            INSERT INTO ownership(
              session_key, handle, state, tool, size, created_at, accessed_at, swept_at, reason
            )
            VALUES (?, '000000000000', 'tombstone', '', 0, 0.0, NULL, 0.0, 'schema_probe')
            """,
            ("__schema_probe__",),
        ).close()
    except sqlite3.IntegrityError:
        rejected = True
    finally:
        conn.execute("ROLLBACK TO ownership_fk_probe").close()
        conn.execute("RELEASE ownership_fk_probe").close()
    return rejected


def _rebuild_ownership(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE ownership RENAME TO ownership_old").close()
    _create_ownership(conn)
    conn.execute(
        """
        INSERT INTO ownership(
          session_key, handle, state, tool, size, created_at, accessed_at, swept_at, reason
        )
        SELECT
          ownership_old.session_key,
          ownership_old.handle,
          ownership_old.state,
          ownership_old.tool,
          COALESCE(blobs.size, 0),
          ownership_old.created_at,
          ownership_old.accessed_at,
          ownership_old.swept_at,
          ownership_old.reason
        FROM ownership_old LEFT JOIN blobs USING(handle)
        """
    ).close()
    conn.execute("DROP TABLE ownership_old").close()
