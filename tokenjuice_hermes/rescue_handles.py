"""Opaque handle validation and normalization.

Handles exposed to the model are 12-character lowercase hex strings. They are
opaque from the model's point of view; internally the store may derive them
from content hashes, but authorization is always checked against the session
index, never the handle shape alone.
"""

from __future__ import annotations

import re

_HANDLE_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{12}$")


def is_valid_handle(handle: str) -> bool:
    """Return True if *handle* is a well-formed opaque rescue handle."""
    return bool(_HANDLE_RE.match(handle))


def normalize_handle(handle: str) -> str | None:
    """Return *handle* if valid, otherwise None.

    Invalid handles include path traversal attempts such as ``../../../etc/passwd``
    and any string that does not match the 12-hex opaque format.
    """
    return handle if is_valid_handle(handle) else None
