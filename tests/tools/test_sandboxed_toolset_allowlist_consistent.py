"""Consistency check: build_stream_sandbox defaults must reference real tools.

P7a-1 shipped a default allowlist that referenced ``edit_file`` — a tool
name that has never been registered in this repo (the actual tool is
``patch``). The drift was invisible until an implementer agent tried to
make an edit and the sandbox refused with "tool not in allowlist".

This test asserts that every name in
:func:`tools.sandboxed_toolset.build_stream_sandbox`'s default set
resolves to a tool registered with the global ``ToolRegistry``. If a
tool is renamed or removed, this test fires immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Importing model_tools forces every tool module to register itself with
# the global registry, mirroring what happens at agent startup.
import model_tools  # noqa: F401
from tools.registry import registry
from tools.sandboxed_toolset import build_stream_sandbox


def test_build_stream_sandbox_defaults_are_registered_tools(tmp_path: Path):
    sandbox = build_stream_sandbox(tmp_path, registry=registry)
    registered = set(registry._tools.keys())
    missing = sandbox.allowed_tools - registered
    assert not missing, (
        f"build_stream_sandbox default allowlist references unregistered "
        f"tools: {sorted(missing)}. Update the default set or rename the "
        f"tool — silent drift between the sandbox allowlist and the real "
        f"tool names is exactly what this test exists to catch."
    )


def test_build_stream_sandbox_extra_tools_extends_allowlist(tmp_path: Path):
    sandbox = build_stream_sandbox(
        tmp_path, registry=registry, extra_tools={"workflow_status"},
    )
    assert "workflow_status" in sandbox.allowed_tools
    # Defaults still present.
    assert "read_file" in sandbox.allowed_tools


def test_build_stream_sandbox_default_includes_patch(tmp_path: Path):
    """Regression: ``patch`` (not ``edit_file``) is the editing tool."""
    sandbox = build_stream_sandbox(tmp_path, registry=registry)
    assert "patch" in sandbox.allowed_tools
    assert "edit_file" not in sandbox.allowed_tools
