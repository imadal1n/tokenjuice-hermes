from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture(autouse=True)
def isolate_ambient_hermes_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(PACKAGE_ROOT / ".pytest-no-hermes-config.yaml"))
