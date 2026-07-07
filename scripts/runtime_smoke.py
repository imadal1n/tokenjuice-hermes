"""Runtime smoke proof for tokenjuice-hermes.

This script is intended to be run inside the Hermes container after a host
rebuild, for example:

    docker exec -i -u 1000:100 <hermes-container> python < runtime_smoke.py

It loads the mounted plugin from /opt/data/plugins/tokenjuice-hermes, verifies
that register() is callable, creates a temporary throwaway BlobStore, exercises
rescue/fetch/status through the plugin, and removes the temporary store.

Output is limited to safe booleans and aggregate counts. Raw blob content,
session identifiers, authentication material, and private paths are never
emitted.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_PLUGIN_PATH_DEFAULT: str = "/opt/data/plugins/tokenjuice-hermes"
_PLUGIN_PATH_ENV: str = "TOKENJUICE_SMOKE_PLUGIN_PATH"
_LIVE_RESCUE_STORE: str = "/opt/data/tokenjuice-hermes/rescue-blobs"
_SMOKE_SESSION: str = "tokenjuice-smoke-session"


class _SmokeHost:
    """Minimal Hermes-like host that exposes the registration surfaces."""

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.hooks: dict[str, Callable[..., str | None]] = {}
        self.middlewares: dict[str, Callable[..., object]] = {}
        self.tools: dict[str, Callable[..., object]] = {}

    def register_hook(self, name: str, callback: Callable[..., str | None]) -> None:
        self.hooks[name] = callback

    def register_middleware(self, name: str, callback: Callable[..., object]) -> None:
        self.middlewares[name] = callback

    def register_tool(self, name: str, callback: Callable[..., object]) -> None:
        self.tools[name] = callback


def _plugin_path() -> Path:
    raw = os.environ.get(_PLUGIN_PATH_ENV, _PLUGIN_PATH_DEFAULT)
    return Path(raw).expanduser().resolve()


def _big_content(lines: int = 150) -> str:
    return "\n".join(f"tokenjuice smoke line {number:04d}" for number in range(1, lines + 1))


def _extract_handle(text: str) -> str | None:
    match = re.search(r"\b[0-9a-f]{12}\b", text)
    return match.group(0) if match else None


def _extract_handle(text: str) -> str | None:
    match = re.search(r"\b[0-9a-f]{12}\b", text)
    return match.group(0) if match else None


def _run_rescue(
    host: _SmokeHost,
    store_path: str,
) -> tuple[bool, str, str]:
    """Rescue oversized content and return (ok, handle, detail)."""
    transform = host.hooks.get("transform_tool_result")
    if transform is None:
        return False, "", "transform_tool_result not registered"

    original = json.dumps({"content": _big_content()})
    result = transform(
        original,
        tool_name="web_search",
        session_id=_SMOKE_SESSION,
        tokenjuice_rescue_store_path=store_path,
    )
    if not isinstance(result, str):
        return False, "", "transform returned non-string"

    handle = _extract_handle(result)
    if not handle:
        return False, "", "no rescue handle in transform result"

    return True, handle, ""


def _run_fetch(host: _SmokeHost, handle: str) -> tuple[bool, str]:
    """Fetch a range from the rescued blob and return (ok, detail)."""
    fetch = host.tools.get("rescuer_fetch")
    if fetch is None:
        return False, "rescuer_fetch not registered"

    fetched = fetch(
        args={"id": handle, "mode": "range", "start": 0, "count": 5},
        session_id=_SMOKE_SESSION,
    )
    if not isinstance(fetched, str):
        return False, "rescuer_fetch returned non-string"
    if "tokenjuice smoke line 0001" not in fetched:
        return False, "expected content missing from range fetch"

    return True, ""


def _run_status(host: _SmokeHost) -> tuple[bool, dict[str, object], str]:
    """Invoke tokenjuice_status and return (ok, snapshot, detail)."""
    status_fn = host.tools.get("tokenjuice_status")
    if status_fn is None:
        return False, {}, "tokenjuice_status not registered"

    status_text = status_fn(args={})
    if not isinstance(status_text, str):
        return False, {}, "tokenjuice_status returned non-string"

    try:
        snapshot = json.loads(status_text)
    except json.JSONDecodeError as exc:
        return False, {}, f"status JSON decode failed: {exc}"

    return True, snapshot, ""


def _print_safe_summary(
    *,
    import_ok: bool,
    register_callable: bool,
    transform_registered: bool,
    rescue_ok: bool,
    fetch_ok: bool,
    status_ok: bool,
    status: dict[str, object],
    temp_store_removed: bool,
) -> None:
    print(f"import_ok={str(import_ok).lower()}")
    print(f"register_callable={str(register_callable).lower()}")
    print(f"transform_registered={str(transform_registered).lower()}")
    print(f"rescue_ok={str(rescue_ok).lower()}")
    print(f"fetch_ok={str(fetch_ok).lower()}")
    print(f"status_ok={str(status_ok).lower()}")
    print(f"rescue_count={status.get('rescue_count', 0)}")
    print(f"fetch_count={status.get('fetch_count', 0)}")
    store = status.get("store", {}) if isinstance(status.get("store"), dict) else {}
    print(f"live_blob_count={store.get('live_blob_count', 0)}")
    print(f"temp_store_removed={str(temp_store_removed).lower()}")


def _exercise_plugin(
    plugin_module: object,
    store_path: str,
) -> tuple[bool, bool, bool, bool, dict[str, object], str]:
    """Register and exercise rescue/fetch/status, returning results and any error."""
    host = _SmokeHost({"tokenjuice_rescue_store_path": store_path})
    try:
        plugin_module.register(host)
    except Exception as exc:  # noqa: BLE001 - smoke script must report failures safely
        return False, False, False, False, {}, f"register_failed: {type(exc).__name__}"

    transform_registered = "transform_tool_result" in host.hooks
    rescue_ok, handle, error = _run_rescue(host, store_path)
    if not rescue_ok:
        return transform_registered, False, False, False, {}, error

    fetch_ok, error = _run_fetch(host, handle)
    if not fetch_ok:
        return transform_registered, True, False, False, {}, error

    status_ok, status, error = _run_status(host)
    return transform_registered, True, fetch_ok, status_ok, status, error


def main() -> int:
    import_ok = False
    register_callable = False
    transform_registered = False
    rescue_ok = False
    fetch_ok = False
    status_ok = False
    status: dict[str, object] = {}
    temp_store_removed = False
    error = ""

    plugin_path = _plugin_path()
    if not plugin_path.is_dir() or not (plugin_path / "__init__.py").is_file():
        error = "plugin_path_missing"
        _print_safe_summary(
            import_ok=import_ok,
            register_callable=register_callable,
            transform_registered=transform_registered,
            rescue_ok=rescue_ok,
            fetch_ok=fetch_ok,
            status_ok=status_ok,
            status=status,
            temp_store_removed=temp_store_removed,
        )
        print(f"error={error}")
        return 1

    tmp_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="tokenjuice-smoke-") as tmp:
        tmp_path = Path(tmp)

        # The runtime mount directory is named tokenjuice-hermes, but Python
        # needs the directory name to match the package name tokenjuice_hermes.
        # Symlink it into the temp workspace under the correct name and import
        # from there; both the symlink and the store are removed with the temp
        # directory.
        package_link = tmp_path / "tokenjuice_hermes"
        package_link.symlink_to(plugin_path, target_is_directory=True)
        sys.path.insert(0, str(tmp_path))

        try:
            plugin_module = importlib.import_module("tokenjuice_hermes.plugin")
            import_ok = True
            register_callable = callable(getattr(plugin_module, "register", None))
        except Exception as exc:  # noqa: BLE001 - smoke script must report failures safely
            error = f"import_failed: {type(exc).__name__}"

        # Guard: the temp store must never be the live rescue store.
        if str(tmp_path) == _LIVE_RESCUE_STORE or tmp_path.is_relative_to(Path(_LIVE_RESCUE_STORE)):
            error = "temp_store_collides_with_live_store"
            _print_safe_summary(
                import_ok=import_ok,
                register_callable=register_callable,
                transform_registered=transform_registered,
                rescue_ok=rescue_ok,
                fetch_ok=fetch_ok,
                status_ok=status_ok,
                status=status,
                temp_store_removed=temp_store_removed,
            )
            print(f"error={error}")
            return 1

        if register_callable:
            transform_registered, rescue_ok, fetch_ok, status_ok, status, error = _exercise_plugin(
                plugin_module, str(tmp_path)
            )

    temp_store_removed = tmp_path is not None and not tmp_path.exists()

    _print_safe_summary(
        import_ok=import_ok,
        register_callable=register_callable,
        transform_registered=transform_registered,
        rescue_ok=rescue_ok,
        fetch_ok=fetch_ok,
        status_ok=status_ok,
        status=status,
        temp_store_removed=temp_store_removed,
    )

    if error:
        print(f"error={error}")
    checks = [import_ok, register_callable, transform_registered, rescue_ok, fetch_ok, status_ok]
    if not all(checks):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
