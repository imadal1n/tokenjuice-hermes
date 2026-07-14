"""Rescue store configuration constants and typed defaults.

The canonical container path is declared here so callers can inject it through
flat kwargs/config. Owner and mode defaults are recorded for downstream Nix
wiring; this module does not attempt to create or chown the runtime path.
"""

from __future__ import annotations

from typing import TypedDict

# Canonical runtime path inside the Hermes container.
RESCUE_STORE_PATH_DEFAULT: str = "/opt/data/tokenjuice-hermes/rescue-blobs"

# Compatibility alias for callers that still import the old constant name.
RESCUE_HOST_STATE_VOLUME: str = RESCUE_STORE_PATH_DEFAULT

# Owner and permissions for the persistent state directory.
RESCUE_STORE_UID: int = 1000
RESCUE_STORE_GID: int = 100
RESCUE_STORE_MODE: int = 0o700

# Default GC and fetch guardrails.
DEFAULT_TTL_HOURS: int = 72
DEFAULT_TOMBSTONE_TTL_HOURS: int = 720
DEFAULT_MAX_STORE_MB: int = 500
DEFAULT_FETCH_MAX_CHARS: int = 4000
DEFAULT_FULL_FETCH_MAX_CHARS: int = 50000
DEFAULT_REFUSE_FULL_FETCH: bool = True
DEFAULT_GREP_MAX_PATTERN_LEN: int = 80
DEFAULT_GREP_MAX_LINE_LEN: int = 2000
DEFAULT_GREP_TIMEOUT_MS: int = 500


class BlobStoreConfig(TypedDict, total=False):
    """Typed configuration accepted by ``BlobStore``.

    Dict literals passed to ``BlobStore`` are checked against this shape by
    the type checker.
    """

    store_path: str
    ttl_hours: int
    tombstone_ttl_hours: int
    max_store_mb: int
    fetch_max_chars: int
    full_fetch_max_chars: int
    refuse_full_fetch: bool
    grep_max_pattern_len: int
    grep_max_line_len: int
    grep_timeout_ms: int
