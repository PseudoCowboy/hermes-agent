"""Conftest for mythos tests — keeps imports + workspace tmpdir setup tidy.

The repo's top-level ``tests/conftest.py`` already enforces hermetic env
(no credential vars, isolated HERMES_HOME). We add a per-test tmp
``MYTHOS_WORKSPACE_DIR`` override to keep filesystem writes contained.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def mythos_workspace(tmp_path: Path, monkeypatch) -> Path:
    workspace = tmp_path / "mythos"
    workspace.mkdir()
    monkeypatch.setenv("MYTHOS_WORKSPACE_DIR", str(workspace))
    return workspace
