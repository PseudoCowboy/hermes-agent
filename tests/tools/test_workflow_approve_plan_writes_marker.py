"""Tests for P7a-1: workflow_approve_plan writes approval marker + project runstate.

Beyond the existing approve-plan happy/error coverage in
``test_workflow_tools.py``, P7a-1 adds three new effects on success:

1. ``control/approval-event.json`` is written, containing
   ``event="approved"``, ``approved_at`` ISO timestamp,
   ``approved_head_sha`` (or null on worktree failure), ``plan_slug``.
2. Project runstate JSON is written with ``phase="approved"`` and the
   captured head sha.
3. The returned JSON includes ``approved_head_sha``.

On failure path (e.g. invalid transition), neither the marker nor the
runstate is created.

Worktree integration is exercised via a real git repo fixture so the
ensure_integration_worktree call returns a real sha — that's the
contract the orchestrator worker depends on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.workflow_tools import (
    workflow_approve_plan,
    workflow_create_project,
    write_json_file,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture()
def git_repo_with_projects(tmp_path, monkeypatch):
    """Real git repo with projects root inside it.

    ``ensure_integration_worktree`` requires a real git repo (it calls
    ``git rev-parse --git-common-dir`` to find the root and then ``git
    worktree add``).  Putting the projects root inside the same repo
    matches production layout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "seed"], cwd=repo)

    projects_root = repo / "groups" / "shared_project" / "active"
    projects_root.mkdir(parents=True)
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(projects_root))
    monkeypatch.chdir(repo)
    return repo, projects_root


def _setup_project_with_plan(slug="approve-test", scope_id="scope-1"):
    """Create a project + plan under *scope_id*.  Returns the project root."""
    from tools.workflow_tools import project_path

    workflow_create_project(slug.replace("-", " ").title(), scope_id=scope_id)
    root = project_path(slug, scope_id)
    plan_dir = root / "plans" / "my-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.md").write_text("# The Plan\n")
    write_json_file(plan_dir / "plan-state.json", {"state": "under_review"})
    return root


# -----------------------------------------------------------------------------
# Success path
# -----------------------------------------------------------------------------


def test_approve_plan_writes_approval_event_marker(git_repo_with_projects):
    _, projects_root = git_repo_with_projects
    _setup_project_with_plan()

    raw = workflow_approve_plan("Approve Test", "my-plan", scope_id="scope-1")
    result = json.loads(raw)
    from tools.workflow_tools import project_path

    actual_root = project_path("approve-test", "scope-1")
    assert result["success"] is True, result
    marker = actual_root / "control" / "approval-event.json"
    assert marker.is_file(), f"marker not found at {marker}"
    payload = json.loads(marker.read_text())
    assert payload["event"] == "approved"
    assert payload["plan_slug"] == "my-plan"
    assert payload["approved_at"]
    assert payload["approved_head_sha"], (
        "approved_head_sha should be a real sha (integration worktree exists)"
    )
    assert len(payload["approved_head_sha"]) == 40
    assert all(c in "0123456789abcdef" for c in payload["approved_head_sha"])


def test_approve_plan_writes_project_runstate_with_phase_approved(
    git_repo_with_projects,
):
    _, projects_root = git_repo_with_projects
    _setup_project_with_plan()

    raw = workflow_approve_plan("Approve Test", "my-plan", scope_id="scope-1")
    result = json.loads(raw)
    assert result["success"] is True, result

    from tools.workflow_tools import project_path

    runstate_path = project_path("approve-test", "scope-1") / "project_runstate.json"
    assert runstate_path.is_file()
    runstate = json.loads(runstate_path.read_text())
    assert runstate["phase"] == "approved"
    assert runstate["approved_head_sha"] == result["approved_head_sha"]
    assert runstate["slug"] == "approve-test"


def test_approve_plan_returns_approved_head_sha_in_response(git_repo_with_projects):
    _setup_project_with_plan()

    raw = workflow_approve_plan("Approve Test", "my-plan", scope_id="scope-1")
    result = json.loads(raw)
    assert result["success"] is True, result
    assert "approved_head_sha" in result
    assert result["approved_head_sha"] is not None
    assert len(result["approved_head_sha"]) == 40


# -----------------------------------------------------------------------------
# Failure path: bad transition → no marker, no runstate
# -----------------------------------------------------------------------------


def test_approve_plan_failure_path_writes_no_marker_no_runstate(
    git_repo_with_projects,
):
    _, projects_root = git_repo_with_projects

    workflow_create_project("Already Approved", scope_id="scope-1")
    from tools.workflow_tools import project_path

    root = project_path("already-approved", "scope-1")
    plan_dir = root / "plans" / "my-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.md").write_text("# The Plan\n")
    # Already in 'approved' state — workflow_approve_plan must reject
    # the transition because PLAN_TRANSITIONS["approved"] = {"decomposed"}.
    write_json_file(plan_dir / "plan-state.json", {"state": "approved"})

    raw = workflow_approve_plan(
        "Already Approved", "my-plan", scope_id="scope-1"
    )
    result = json.loads(raw)
    assert "error" in result, result

    # Neither side effect should have happened.
    assert not (root / "control" / "approval-event.json").exists()
    assert not (root / "project_runstate.json").exists()


# -----------------------------------------------------------------------------
# Idempotency / re-run safety: re-approving (which fails) doesn't clobber
# a prior successful approval's marker or runstate.
# -----------------------------------------------------------------------------


def test_re_approving_after_decomposition_does_not_clobber_marker(
    git_repo_with_projects,
):
    """If the plan is later moved to 'approved' state and somebody calls
    workflow_approve_plan again, the call fails (invalid transition) and
    the existing marker must remain untouched — otherwise the
    orchestrator would re-trigger bootstrap on a project that's already
    past that phase.
    """
    _setup_project_with_plan()

    # First successful approval writes the marker.
    raw1 = workflow_approve_plan("Approve Test", "my-plan", scope_id="scope-1")
    assert json.loads(raw1)["success"] is True, json.loads(raw1)

    from tools.workflow_tools import project_path

    marker = project_path("approve-test", "scope-1") / "control" / "approval-event.json"
    original_marker = marker.read_text()

    # Second call: state is now 'approved' → transition forbidden.
    raw2 = workflow_approve_plan("Approve Test", "my-plan", scope_id="scope-1")
    assert "error" in json.loads(raw2)

    # Marker preserved.
    assert marker.read_text() == original_marker
