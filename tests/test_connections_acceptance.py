"""Run the existing disposable-identity browser scenario as a named pytest case."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.mark.browser
def test_disposable_connections():
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/connections_e2e.py"), run_name="__main__")
