"""Tests for hermes_cli.project_worktree — scope-aware git worktree helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.project_worktree import (
    WorktreeError,
    _safe_scope,
    ensure_integration_worktree,
    ensure_stream_worktree,
    integration_worktree_path,
    list_project_worktrees,
    list_registered_worktrees,
    project_branch_name,
    project_worktree_root,
    remove_worktree,
    stream_branch_name,
    stream_worktree_path,
    worktrees_root,
)


# =============================================================================
# Fixtures
# =============================================================================


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    """Initialize a minimal git repo with a ``main`` branch + one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "seed"], cwd=repo)
    monkeypatch.chdir(repo)
    return repo


# =============================================================================
# Path / branch templates
# =============================================================================


class TestPathTemplates:
    def test_worktree_root(self, git_repo):
        assert worktrees_root() == git_repo / ".worktrees"

    def test_project_worktree_root_splices_scope_and_slug(self, git_repo):
        p = project_worktree_root("12345", "demo")
        assert p == git_repo / ".worktrees" / "12345" / "demo"

    def test_integration_worktree_path(self, git_repo):
        p = integration_worktree_path("12345", "demo")
        assert p.name == "_integration"
        assert p.parent == git_repo / ".worktrees" / "12345" / "demo"

    def test_stream_worktree_path(self, git_repo):
        p = stream_worktree_path("12345", "demo", "frontend")
        assert p == git_repo / ".worktrees" / "12345" / "demo" / "frontend"

    def test_project_branch_name(self):
        assert project_branch_name("12345", "demo") == "project/12345/demo/_integration"

    def test_stream_branch_name(self):
        assert (
            stream_branch_name("12345", "demo", "frontend")
            == "project/12345/demo/frontend"
        )

    def test_scope_sanitized(self, git_repo):
        # Path-traversal scope id must not escape .worktrees/.
        p = project_worktree_root("../../etc", "demo")
        # Sanitized scope segment must live strictly under .worktrees/.
        p.relative_to(git_repo / ".worktrees")
        # Segment itself must not be ``..`` or ``.``.
        scope_seg = p.parent.name
        assert scope_seg not in ("..", ".")
        assert "/" not in scope_seg

    def test_slug_rejected_when_path_like(self):
        for bad in ("a/b", "..", ".", "-flag", ""):
            with pytest.raises(WorktreeError):
                project_worktree_root("s", bad)

    def test_stream_rejects_integration_collision(self):
        with pytest.raises(WorktreeError):
            stream_worktree_path("s", "demo", "_integration")
        with pytest.raises(WorktreeError):
            stream_branch_name("s", "demo", "_integration")

    def test_blank_scope_rejected(self):
        with pytest.raises(WorktreeError):
            project_worktree_root("", "demo")
        with pytest.raises(WorktreeError):
            project_worktree_root("   ", "demo")


# =============================================================================
# Creation / removal
# =============================================================================


class TestEnsureIntegrationWorktree:
    def test_creates_branch_and_worktree(self, git_repo):
        h = ensure_integration_worktree("cat1", "demo", base="main")
        expected_wt = git_repo / ".worktrees" / _safe_scope("cat1") / "demo" / "_integration"
        assert h.integration_worktree == expected_wt
        assert h.integration_worktree.is_dir()
        assert h.integration_branch == f"project/{_safe_scope('cat1')}/demo/_integration"
        # Branch exists
        out = subprocess.check_output(
            ["git", "branch", "--list", h.integration_branch],
            cwd=str(git_repo),
            text=True,
        )
        assert h.integration_branch in out

    def test_is_idempotent(self, git_repo):
        h1 = ensure_integration_worktree("cat1", "demo")
        h2 = ensure_integration_worktree("cat1", "demo")
        assert h1 == h2
        # Only one registered worktree (plus the main checkout).
        paths = list_registered_worktrees()
        assert h1.integration_worktree.resolve() in {p.resolve() for p in paths}

    def test_different_scopes_do_not_collide(self, git_repo):
        h1 = ensure_integration_worktree("cat1", "demo")
        h2 = ensure_integration_worktree("cat2", "demo")
        assert h1.integration_worktree != h2.integration_worktree
        assert h1.integration_branch != h2.integration_branch


class TestEnsureStreamWorktree:
    def test_creates_stream_off_integration(self, git_repo):
        ensure_integration_worktree("cat1", "demo")
        p = ensure_stream_worktree("cat1", "demo", "frontend")
        expected = git_repo / ".worktrees" / _safe_scope("cat1") / "demo" / "frontend"
        assert p == expected
        assert p.is_dir()
        # Stream branch exists.
        branch = stream_branch_name("cat1", "demo", "frontend")
        out = subprocess.check_output(
            ["git", "branch", "--list", branch],
            cwd=str(git_repo),
            text=True,
        )
        assert branch in out

    def test_rejects_if_no_integration(self, git_repo):
        with pytest.raises(WorktreeError):
            ensure_stream_worktree("cat1", "demo", "frontend")

    def test_is_idempotent(self, git_repo):
        ensure_integration_worktree("cat1", "demo")
        p1 = ensure_stream_worktree("cat1", "demo", "frontend")
        p2 = ensure_stream_worktree("cat1", "demo", "frontend")
        assert p1 == p2

    def test_multiple_streams(self, git_repo):
        ensure_integration_worktree("cat1", "demo")
        p_front = ensure_stream_worktree("cat1", "demo", "frontend")
        p_back = ensure_stream_worktree("cat1", "demo", "backend")
        assert p_front != p_back
        # Both streams inherit from the integration branch (same tip).
        head_front = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(p_front), text=True
        ).strip()
        head_back = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(p_back), text=True
        ).strip()
        assert head_front == head_back


class TestRemoveWorktree:
    def test_removes_registered_worktree(self, git_repo):
        h = ensure_integration_worktree("cat1", "demo")
        remove_worktree(h.integration_worktree, delete_branch=True)
        assert not h.integration_worktree.exists()
        out = subprocess.check_output(
            ["git", "branch", "--list", h.integration_branch],
            cwd=str(git_repo),
            text=True,
        )
        assert out.strip() == ""

    def test_refuses_paths_outside_worktrees_dir(self, git_repo):
        target = git_repo / "random"
        target.mkdir()
        with pytest.raises(WorktreeError):
            remove_worktree(target)
        assert target.is_dir(), "refusal must not have deleted the path"


# =============================================================================
# Introspection
# =============================================================================


class TestListProjectWorktrees:
    def test_lists_only_this_projects_worktrees(self, git_repo):
        # Two separate projects under two scopes.
        ensure_integration_worktree("cat1", "demo")
        ensure_integration_worktree("cat2", "demo")
        ensure_stream_worktree("cat1", "demo", "frontend")
        paths = list_project_worktrees("cat1", "demo")
        # Should see cat1 integration + cat1 frontend; not cat2.
        names = {p.name for p in paths}
        assert "_integration" in names
        assert "frontend" in names
        assert all("cat2" not in str(p) for p in paths)
