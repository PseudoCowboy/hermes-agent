"""Tests for sandbox interception in ToolRegistry.dispatch (P7a-2).

When the dispatch context carries a SandboxedToolset and the called tool
is in its allowlist, dispatch routes through the sandbox first (which
validates path args + forces workdir) and then back into the registry to
execute the handler. The recursion guard prevents the second pass from
re-entering the sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from tools.registry import (
    ToolDispatchContext,
    ToolRegistry,
    use_dispatch_context,
)
from tools.sandboxed_toolset import SandboxedToolset, SandboxViolation


# ---------------------------------------------------------------------------
# Fixtures: a private registry per test so we don't pollute the global one.
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry():
    return ToolRegistry()


@pytest.fixture()
def sandbox_root(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    return root


def _register_path_tool(reg: ToolRegistry, name: str = "fake_read"):
    """Register a tool that accepts a ``path`` argument and echoes it back.

    The schema declares no auto-bind keys — sandbox interception happens
    independent of auto-bind, so we keep the tool minimal.
    """
    calls: list = []

    def handler(args: Dict[str, Any]) -> str:
        calls.append(args)
        return json.dumps({"ok": True, "path": args.get("path")})

    reg.register(
        name=name,
        toolset="sandbox-test",
        schema={
            "name": name,
            "description": "fake path-bearing tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        handler=handler,
    )
    return calls


# ---------------------------------------------------------------------------
# Sandbox routes the call through and the handler runs once.
# ---------------------------------------------------------------------------


def test_sandbox_in_context_routes_through_then_executes_handler(
    registry, sandbox_root,
):
    calls = _register_path_tool(registry, name="fake_read")

    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    result = registry.dispatch("fake_read", {"path": "foo.txt"}, context=ctx)
    payload = json.loads(result)
    # Sandbox rewrote relative path → absolute under the worktree.
    assert payload["ok"] is True
    expected = str((sandbox_root / "foo.txt").resolve())
    assert payload["path"] == expected
    # Handler ran exactly once (no recursion loop).
    assert len(calls) == 1
    assert calls[0]["path"] == expected


# ---------------------------------------------------------------------------
# Recursion guard: SandboxedToolset.dispatch calling back into registry must
# not re-enter the sandbox check.
# ---------------------------------------------------------------------------


def test_sandbox_recursion_guard_runs_handler_exactly_once(
    registry, sandbox_root,
):
    """The recursion guard ContextVar should keep the handler running once.

    We assert by counting handler invocations — a missing guard would
    loop infinitely (StackOverflow / RecursionError); a buggy guard
    might run the handler twice.
    """
    calls = _register_path_tool(registry, name="fake_read")

    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    registry.dispatch("fake_read", {"path": "a.txt"}, context=ctx)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Tool NOT in allowlist falls through to direct dispatch (no sandbox interception).
# ---------------------------------------------------------------------------


def test_tool_not_in_allowlist_falls_through_to_handler(registry, sandbox_root):
    calls = _register_path_tool(registry, name="other_tool")

    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},  # other_tool deliberately not listed
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    # Unknown-to-sandbox tool: dispatch goes straight to the handler,
    # the path arg is NOT validated against the worktree (a direct
    # absolute path passes through unchanged).
    result = registry.dispatch("other_tool", {"path": "/etc/anywhere"}, context=ctx)
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["path"] == "/etc/anywhere"  # not rewritten
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# sandbox=None path is unchanged (regression guard for non-implementer dispatch).
# ---------------------------------------------------------------------------


def test_sandbox_none_dispatch_is_unchanged(registry, sandbox_root):
    calls = _register_path_tool(registry, name="fake_read")

    ctx = ToolDispatchContext(sandbox=None)
    registry.dispatch("fake_read", {"path": "/abs/raw"}, context=ctx)
    assert len(calls) == 1
    assert calls[0]["path"] == "/abs/raw"


def test_no_context_dispatch_is_unchanged(registry):
    calls = _register_path_tool(registry, name="fake_read")
    registry.dispatch("fake_read", {"path": "/no/context"})
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Sandbox refuses path-traversal: the handler is never called and the
# error is returned as a JSON string (not raised).
# ---------------------------------------------------------------------------


def test_sandbox_rejects_absolute_path_outside_root(registry, sandbox_root):
    calls = _register_path_tool(registry, name="fake_read")
    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    result = registry.dispatch("fake_read", {"path": "/etc/passwd"}, context=ctx)
    payload = json.loads(result)
    assert "error" in payload
    assert "sandbox" in payload["error"].lower() or "absolute" in payload["error"].lower()
    assert calls == []


def test_sandbox_rejects_dotdot_traversal(registry, sandbox_root):
    calls = _register_path_tool(registry, name="fake_read")
    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    result = registry.dispatch(
        "fake_read", {"path": "../../escape.txt"}, context=ctx,
    )
    payload = json.loads(result)
    assert "error" in payload
    assert calls == []


# ---------------------------------------------------------------------------
# Ambient context (use_dispatch_context) also picks up the sandbox.
# ---------------------------------------------------------------------------


def test_ambient_context_carries_sandbox(registry, sandbox_root):
    calls = _register_path_tool(registry, name="fake_read")
    sandbox = SandboxedToolset(
        root=sandbox_root,
        registry=registry,
        allowed_tools={"fake_read"},
    )
    ctx = ToolDispatchContext(sandbox=sandbox, worktree_root=sandbox_root)

    with use_dispatch_context(ctx):
        registry.dispatch("fake_read", {"path": "x.txt"})

    assert len(calls) == 1
    assert calls[0]["path"] == str((sandbox_root / "x.txt").resolve())
