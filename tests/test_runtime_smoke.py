from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_NAME: str = "runtime_smoke.py"
_LIVE_RESCUE_STORE: str = "/opt/data/tokenjuice-hermes/rescue-blobs"
_SMOKE_SESSION: str = "tokenjuice-smoke-session"


@pytest.fixture
def smoke_script() -> Path:
    """Path to the source-controlled runtime smoke script."""
    return Path(__file__).resolve().parents[1] / "scripts" / _SCRIPT_NAME


@pytest.fixture
def plugin_mount(tmp_path: Path) -> Path:
    """Create a temporary plugin mount mirroring the current package source."""
    source_package = Path(__file__).resolve().parents[1] / "tokenjuice_hermes"
    mount = tmp_path / "plugins" / "tokenjuice-hermes"
    mount.parent.mkdir(parents=True, exist_ok=True)
    # The plugin directory itself is the Python package, so symlink it directly.
    mount.symlink_to(source_package, target_is_directory=True)
    return mount


def test_smoke_script_exists(smoke_script: Path) -> None:
    assert smoke_script.exists(), "runtime smoke script must be source-controlled"


def test_smoke_script_never_writes_to_live_rescue_store(smoke_script: Path) -> None:
    text = smoke_script.read_text(encoding="utf-8")
    assert f'"tokenjuice_rescue_store_path": "{_LIVE_RESCUE_STORE}"' not in text
    assert f'TemporaryDirectory(dir="{_LIVE_RESCUE_STORE}"' not in text
    assert 'TemporaryDirectory(dir="' not in text or _LIVE_RESCUE_STORE not in text
    assert "mkdir" not in text or _LIVE_RESCUE_STORE not in text


def test_smoke_script_contains_no_chat_api_or_secrets_access(smoke_script: Path) -> None:
    text = smoke_script.read_text(encoding="utf-8").lower()
    unsafe_access_patterns = [
        "chat",
        "api_key",
        "apikey",
        "password",
        "credential",
        "env_file",
        "config.yaml",
        "os.environ[",
    ]
    for word in unsafe_access_patterns:
        assert word not in text, f"smoke script must not reference {word}"

    assert 'importlib.import_module("tokenjuice_hermes.plugin")' in text
    assert "requests" not in text
    assert "urllib" not in text
    assert "chat_client" not in text


def test_smoke_script_uses_temp_store_and_cleans_up(smoke_script: Path) -> None:
    text = smoke_script.read_text(encoding="utf-8")
    assert "tempfile.TemporaryDirectory" in text
    assert "temp_store_removed" in text


def test_smoke_script_runs_hermetically(plugin_mount: Path, smoke_script: Path) -> None:
    """Run the smoke script against a temp plugin mount and verify safe output."""
    env = os.environ.copy()
    env["TOKENJUICE_SMOKE_PLUGIN_PATH"] = str(plugin_mount)
    result = subprocess.run(
        [sys.executable, str(smoke_script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, (
        f"smoke script failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    stdout = result.stdout
    assert "import_ok=true" in stdout
    assert "register_callable=true" in stdout
    assert "transform_registered=true" in stdout
    assert "rescue_ok=true" in stdout
    assert "fetch_ok=true" in stdout
    assert "status_ok=true" in stdout
    assert "temp_store_removed=true" in stdout

    # Output must not contain raw blob content, the session id, or private paths.
    assert _SMOKE_SESSION not in stdout
    assert "/opt/data" not in stdout
    assert "tokenjuice smoke line" not in stdout
    assert result.stderr == ""


def test_smoke_script_fails_on_missing_plugin_mount(smoke_script: Path) -> None:
    """A missing plugin mount must fail without touching config or live store."""
    env = os.environ.copy()
    env["TOKENJUICE_SMOKE_PLUGIN_PATH"] = "/nonexistent/tokenjuice-hermes-mount"
    result = subprocess.run(
        [sys.executable, str(smoke_script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "import_ok=false" in result.stdout
    assert "error=plugin_path_missing" in result.stdout
    assert result.stderr == ""
