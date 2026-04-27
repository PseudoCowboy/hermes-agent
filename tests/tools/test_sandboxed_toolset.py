"""Tests for tools.sandboxed_toolset — path-allowlist wrapper for tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sandboxed_toolset import (
    SandboxedToolset,
    SandboxViolation,
    build_stream_sandbox,
)


class _FakeRegistry:
    """Records every dispatch call without running real tools."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []
        self.return_value = '{"ok": true}'

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        self.calls.append((name, args, kwargs))
        return self.return_value


@pytest.fixture()
def sandbox(tmp_path):
    root = tmp_path / "stream"
    root.mkdir()
    (root / "hello.txt").write_text("hi")
    return SandboxedToolset(
        root=root,
        registry=_FakeRegistry(),
        allowed_tools={"read_file", "write_file", "terminal"},
    )


# =============================================================================
# validate_path
# =============================================================================


class TestValidatePath:
    def test_accepts_relative_path_inside_root(self, sandbox):
        p = sandbox.validate_path("hello.txt")
        assert p == (sandbox.root / "hello.txt").resolve()

    def test_accepts_nested_relative_path(self, sandbox):
        (sandbox.root / "a").mkdir()
        (sandbox.root / "a" / "b.txt").write_text("x")
        p = sandbox.validate_path("a/b.txt")
        assert p == (sandbox.root / "a" / "b.txt").resolve()

    def test_rejects_absolute_path_outside_root(self, sandbox):
        with pytest.raises(SandboxViolation, match="absolute"):
            sandbox.validate_path("/etc/passwd")

    def test_rejects_absolute_path_even_if_under_root(self, sandbox):
        # Design §10 says absolute-path escapes are rejected outright;
        # we do not silently translate an absolute under-root form.
        inside = str(sandbox.root / "hello.txt")
        with pytest.raises(SandboxViolation, match="absolute"):
            sandbox.validate_path(inside)

    def test_rejects_dotdot_escape(self, sandbox):
        with pytest.raises(SandboxViolation, match="escapes"):
            sandbox.validate_path("../outside.txt")

    def test_rejects_empty_string(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.validate_path("")

    def test_rejects_non_string(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.validate_path(None)  # type: ignore[arg-type]


# =============================================================================
# validate_args
# =============================================================================


class TestValidateArgs:
    def test_rejects_unallowed_tool(self, sandbox):
        with pytest.raises(SandboxViolation, match="not in this sandbox"):
            sandbox.validate_args("rm_rf_root", {})

    def test_validates_path_argument(self, sandbox):
        out = sandbox.validate_args("read_file", {"path": "hello.txt"})
        # Path is rewritten to canonical absolute form so downstream
        # tools cannot reinterpret the relative against a different cwd.
        assert out["path"] == str((sandbox.root / "hello.txt").resolve())

    def test_rejects_path_escape(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.validate_args("read_file", {"path": "../etc/passwd"})

    def test_rejects_file_path_escape(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.validate_args(
                "write_file", {"file_path": "/etc/passwd", "content": "x"}
            )

    def test_injects_workdir_when_absent(self, sandbox):
        out = sandbox.validate_args("terminal", {"command": "ls"})
        assert out["workdir"] == str(sandbox.root.resolve())

    def test_accepts_explicit_workdir_inside_root(self, sandbox):
        (sandbox.root / "sub").mkdir()
        out = sandbox.validate_args(
            "terminal", {"command": "ls", "workdir": "sub"}
        )
        # workdir is rewritten to the canonical absolute form.
        assert out["workdir"] == str((sandbox.root / "sub").resolve())

    def test_rejects_explicit_absolute_workdir(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.validate_args(
                "terminal", {"command": "ls", "workdir": "/tmp"}
            )

    def test_does_not_mutate_input_dict(self, sandbox):
        args = {"command": "ls"}
        sandbox.validate_args("terminal", args)
        assert "workdir" not in args, "caller's dict must be untouched"


# =============================================================================
# dispatch
# =============================================================================


class TestDispatch:
    def test_forwards_valid_call(self, sandbox):
        out = sandbox.dispatch("read_file", {"path": "hello.txt"})
        assert out == '{"ok": true}'
        calls = sandbox.registry.calls
        assert len(calls) == 1
        name, args, _ = calls[0]
        assert name == "read_file"
        # Forwarded path is canonical absolute so cwd cannot reroute it.
        assert args["path"] == str((sandbox.root / "hello.txt").resolve())

    def test_dispatch_returns_json_error_on_violation(self, sandbox):
        out = sandbox.dispatch("read_file", {"path": "/etc/passwd"})
        err = json.loads(out)
        assert "error" in err
        assert "sandbox violation" in err["error"]
        assert sandbox.registry.calls == []

    def test_dispatch_returns_json_error_on_unknown_tool(self, sandbox):
        out = sandbox.dispatch("delete_everything", {})
        err = json.loads(out)
        assert "error" in err
        assert sandbox.registry.calls == []

    def test_dispatch_forces_workdir_into_forwarded_args(self, sandbox):
        sandbox.dispatch("terminal", {"command": "ls"})
        name, args, _ = sandbox.registry.calls[-1]
        assert name == "terminal"
        assert args["workdir"] == str(sandbox.root.resolve())

    def test_canonical_rewrite_blocks_cwd_reinterpretation(self, sandbox, tmp_path, monkeypatch):
        """Regression: a relative path forwarded as-is would resolve
        against process cwd downstream. The sandbox must rewrite to an
        absolute path so cwd cannot reroute the operation.
        """
        # Create a same-named decoy file outside the sandbox root.
        decoy_dir = tmp_path / "decoy"
        decoy_dir.mkdir()
        (decoy_dir / "hello.txt").write_text("decoy content")
        # Move process cwd to the decoy dir to simulate a downstream
        # tool that resolves relatives against cwd, not against sandbox.
        monkeypatch.chdir(decoy_dir)

        sandbox.dispatch("read_file", {"path": "hello.txt"})
        name, args, _ = sandbox.registry.calls[-1]
        forwarded = Path(args["path"])
        # The forwarded path is absolute and points into the sandbox
        # root (NOT into the decoy dir under cwd).
        assert forwarded.is_absolute()
        assert forwarded == (sandbox.root / "hello.txt").resolve()
        assert decoy_dir.resolve() not in forwarded.parents


# =============================================================================
# build_stream_sandbox
# =============================================================================


class TestBuildStreamSandbox:
    def test_default_allowlist(self, tmp_path):
        reg = _FakeRegistry()
        root = tmp_path / "s"
        root.mkdir()
        s = build_stream_sandbox(root, registry=reg)
        for name in ("read_file", "write_file", "edit_file", "search_files", "terminal"):
            assert s.is_allowed(name)
        assert not s.is_allowed("browser")

    def test_extra_tools_append(self, tmp_path):
        reg = _FakeRegistry()
        root = tmp_path / "s"
        root.mkdir()
        s = build_stream_sandbox(root, registry=reg, extra_tools=["custom_tool"])
        assert s.is_allowed("custom_tool")
        assert s.is_allowed("read_file")


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_rejects_nonexistent_root(self, tmp_path):
        reg = _FakeRegistry()
        with pytest.raises(SandboxViolation):
            SandboxedToolset(
                root=tmp_path / "does-not-exist",
                registry=reg,
                allowed_tools={"read_file"},
            )

    def test_allowlist_frozen_after_construction(self, tmp_path):
        reg = _FakeRegistry()
        root = tmp_path / "s"
        root.mkdir()
        s = SandboxedToolset(
            root=root, registry=reg, allowed_tools={"read_file"}
        )
        # After __post_init__, allowed_tools is a frozenset so attempts
        # to add at runtime raise AttributeError.
        with pytest.raises(AttributeError):
            s.allowed_tools.add("other")  # type: ignore[attr-defined]
