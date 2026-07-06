"""Bounded grep implementation for rescued blob fetch (stdlib-only literal fallback).

Regex patterns are refused; install the optional `regex` package and swap this
module for a regex-capable implementation if regex grep is required.
"""

from __future__ import annotations

import re
import time
from typing import Final

from .rescue_types import (
    DEFAULT_FETCH_MAX_CHARS,
    DEFAULT_GREP_MAX_LINE_LEN,
    DEFAULT_GREP_MAX_PATTERN_LEN,
    DEFAULT_GREP_TIMEOUT_MS,
)

_META_CHARS: Final[frozenset[str]] = frozenset(r".^$*+?{}[]\|()")
_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f]")
_MATCH_CAP: Final[int] = 50


def grep_text(
    text: str,
    pattern: str,
    cfg: dict[str, object] | None = None,
) -> str:
    """Search *text* for *pattern* with ReDoS bounds and caps."""
    config = cfg if cfg is not None else {}
    cap = _int_or(config.get("fetch_max_chars"), DEFAULT_FETCH_MAX_CHARS)

    if not pattern:
        return "Error: grep requires pattern=..."
    if len(pattern) > _int_or(config.get("grep_max_pattern_len"), DEFAULT_GREP_MAX_PATTERN_LEN):
        return f"Error: pattern too long ({len(pattern)} chars)"
    if _CONTROL_RE.search(pattern):
        return "Error: pattern contains control characters"
    if set(pattern) & _META_CHARS:
        return (
            "Error: regex patterns need the optional 'regex' "
            "package; install it, or use a literal substring"
        )

    per_line_timeout = max(
        _int_or(config.get("grep_timeout_ms"), DEFAULT_GREP_TIMEOUT_MS) / 1000.0, 0.1
    )
    wall_timeout = max(per_line_timeout, 2.0)
    line_cap = _int_or(config.get("grep_max_line_len"), DEFAULT_GREP_MAX_LINE_LEN)

    needle = pattern.lower()
    lines = text.splitlines()
    results: list[str] = []
    t0 = time.time()
    total = len(lines)
    for n, line in enumerate(lines):
        if time.time() - t0 > wall_timeout:
            results.append(f"[grep timed out after {wall_timeout}s; {len(results)} matches]")
            break
        if needle in line[:line_cap].lower():
            results.append(f"{n}: {line[:500]}")
            if len(results) >= _MATCH_CAP:
                results.append(f"[{_MATCH_CAP} matches; capped]")
                break
    if not results:
        return f"[no matches for pattern '{pattern}' in {total} lines]"
    return "\n".join(results)[:cap]


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value
