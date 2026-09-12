"""Shared test fixtures: isolate every test from the developer's real config/state dirs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import os

    for var in ("MCP_ATLASSIAN_BETTER_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    for key in list(os.environ):
        if key.startswith("MCP_ATLASSIAN_BETTER_SITE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    yield
