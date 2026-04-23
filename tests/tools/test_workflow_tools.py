"""Tests for tools/workflow_tools.py — project creation, plan approval, status."""

import json
import os
from unittest.mock import patch

import pytest
from pathlib import Path

import tools.workflow_tools as workflow_tools_module

from tools.workflow_tools import (
    slugify,
    workflow_save_plan,
    workflow_create_project,
    workflow_approve_plan,
    workflow_status,
    workflow_decompose,
    workflow_handoff,
    workflow_checkpoint,
    workflow_sync_tasks,
    workflow_review_task,
    project_path,
    read_json_file,
    write_json_file,
    write_text_file,
    safe_write_text_file,
    check_workflow_requirements,
    _contained_child,
    _MISSING,
    _CORRUPT,
    VALID_PLAN_STATES,
    PLAN_TRANSITIONS,
    VALID_TASK_STATES,
    TASK_TRANSITIONS,
    VALID_COMPLETION_MODES,
    VALID_HANDOFF_STATUSES,
    _PROJECT_DIRS,
)


# =========================================================================
# Helpers for PR2 tests
# =========================================================================

def _bootstrap_approved_project(projects_root, name="Decomp Test", plan_state=None):
    """Create a project whose plan has been approved (approved-plan.md present).

    Returns the project root path and the plan directory path.
    """
    workflow_create_project(name)
    slug_parts = name.lower().replace("  ", " ").strip().split()
    slug = "-".join(slug_parts)
    root = projects_root / slug
    plan_dir = root / "plans" / "the-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text("# Plan\n")
    write_json_file(
        plan_dir / "plan-state.json",
        plan_state if plan_state is not None else {"state": "under_review"},
    )
    # Run the approval path so control/approved-plan.md exists.
    workflow_approve_plan(name, "the-plan")
    return root, plan_dir


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    """Set WORKFLOW_PROJECTS_ROOT to a temp directory."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(root))
    return root


# =========================================================================
# slugify
# =========================================================================

class TestSlugify:
    def test_basic(self):
        assert slugify("Billing Rewrite") == "billing-rewrite"

    def test_special_chars(self):
        assert slugify("  my--project!!  ") == "my-project"

    def test_numbers(self):
        assert slugify("Phase 2 Backend") == "phase-2-backend"

    def test_empty(self):
        assert slugify("") == ""
        assert slugify("   ") == ""

    def test_already_slug(self):
        assert slugify("already-a-slug") == "already-a-slug"

    def test_unicode_stripped(self):
        assert slugify("café project") == "caf-project"


# =========================================================================
# safe_write_text_file
# =========================================================================

class TestSafeWriteTextFile:
    def test_creates_new_file(self, tmp_path):
        p = tmp_path / "sub" / "file.md"
        assert safe_write_text_file(p, "hello") is True
        assert p.read_text() == "hello"

    def test_does_not_overwrite_existing(self, tmp_path):
        p = tmp_path / "file.md"
        p.write_text("original")
        assert safe_write_text_file(p, "new content") is False
        assert p.read_text() == "original"


# =========================================================================
# read_json_file / write_json_file
# =========================================================================

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "data.json"
        data = {"key": "value", "count": 42}
        write_json_file(p, data)
        assert read_json_file(p) == data

    def test_read_missing_returns_sentinel(self, tmp_path):
        assert read_json_file(tmp_path / "nope.json") is _MISSING

    def test_read_invalid_json_returns_corrupt(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json {{{")
        assert read_json_file(p) is _CORRUPT

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "c.json"
        write_json_file(p, [1, 2, 3])
        assert read_json_file(p) == [1, 2, 3]


# =========================================================================
# _contained_child
# =========================================================================

class TestContainedChild:
    def test_simple_name(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        assert _contained_child(tmp_path, "sub") == child.resolve()

    def test_traversal_blocked(self, tmp_path):
        assert _contained_child(tmp_path, "../../etc") is None

    def test_dot_dot_slash_blocked(self, tmp_path):
        assert _contained_child(tmp_path, "../other") is None

    def test_parent_itself_blocked(self, tmp_path):
        assert _contained_child(tmp_path, ".") is None

    def test_nested_path_blocked(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        assert _contained_child(tmp_path, "a/b") is None


# =========================================================================
# _project_lock
# =========================================================================

class TestProjectLock:
    def test_no_fcntl_fallback_still_enters_context(self, projects_root, monkeypatch):
        monkeypatch.setattr(workflow_tools_module, "fcntl", None)

        with workflow_tools_module._project_lock("demo-project"):
            lock_path = projects_root / ".workflow-locks" / "demo-project.lock"
            assert lock_path.is_file()


# =========================================================================
# workflow_save_plan
# =========================================================================

class TestWorkflowSavePlan:
    def test_writes_draft_plan_and_state(self, projects_root):
        workflow_create_project("Plan Save Test")

        result = json.loads(
            workflow_save_plan(
                "Plan Save Test",
                "auth-rollout",
                "# Auth Rollout\n\nInitial draft.",
            )
        )

        assert result["success"] is True
        assert result["artifact"] == "plan.md"
        root = projects_root / "plan-save-test"
        assert (root / "plans" / "auth-rollout" / "plan.md").read_text() == "# Auth Rollout\n\nInitial draft.\n"
        state = read_json_file(root / "plans" / "auth-rollout" / "plan-state.json")
        assert state["state"] == "draft"
        assert state["lastUpdatedArtifact"] == "plan.md"

    def test_writes_plan_v2_questions_and_decision_log(self, projects_root):
        workflow_create_project("Plan Save Test")

        result = json.loads(
            workflow_save_plan(
                "Plan Save Test",
                "auth-rollout",
                "# Auth Rollout v2\n\nRefined plan.",
                artifact="plan-v2",
                state="under_review",
                questions_markdown="# Open Questions\n\n- SSO provider?",
                decision_log_markdown="# Decision Log\n\n- Keep auth local for MVP.",
            )
        )

        assert result["success"] is True
        root = projects_root / "plan-save-test"
        assert (root / "plans" / "auth-rollout" / "plan-v2.md").read_text() == "# Auth Rollout v2\n\nRefined plan.\n"
        assert (root / "plans" / "auth-rollout" / "questions.md").read_text() == "# Open Questions\n\n- SSO provider?\n"
        assert (root / "plans" / "auth-rollout" / "decision-log.md").read_text() == "# Decision Log\n\n- Keep auth local for MVP.\n"
        state = read_json_file(root / "plans" / "auth-rollout" / "plan-state.json")
        assert state["state"] == "under_review"
        assert state["lastUpdatedArtifact"] == "plan-v2.md"
        assert state["questionsPath"] == "plans/auth-rollout/questions.md"
        assert state["decisionLogPath"] == "plans/auth-rollout/decision-log.md"

    def test_rejects_terminal_target_states(self, projects_root):
        workflow_create_project("Plan Save Test")

        result = json.loads(
            workflow_save_plan(
                "Plan Save Test",
                "auth-rollout",
                "# Plan\n\nBody.",
                state="approved",
            )
        )

        assert "error" in result
        assert "workflow_approve_plan" in result["error"]

    def test_rejects_mutating_approved_plan(self, projects_root):
        workflow_create_project("Plan Save Test")
        root = projects_root / "plan-save-test"
        plan_dir = root / "plans" / "auth-rollout"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan-v2.md").write_text("# Approved\n", encoding="utf-8")
        write_json_file(plan_dir / "plan-state.json", {"state": "approved"})

        result = json.loads(
            workflow_save_plan(
                "Plan Save Test",
                "auth-rollout",
                "# Mutated\n\nNope.",
                artifact="plan-v2",
            )
        )

        assert "error" in result
        assert "terminal state 'approved'" in result["error"]

    def test_rejects_nested_plan_slug(self, projects_root):
        workflow_create_project("Plan Save Test")

        result = json.loads(
            workflow_save_plan(
                "Plan Save Test",
                "foo/bar",
                "# Plan\n\nBody.",
            )
        )

        assert "error" in result
        assert "Invalid plan_slug" in result["error"]


# =========================================================================
# workflow_create_project
# =========================================================================

class TestWorkflowCreateProject:
    def test_creates_full_structure(self, projects_root):
        result = json.loads(workflow_create_project("Billing Rewrite"))
        assert result["success"] is True
        assert result["project_slug"] == "billing-rewrite"

        root = projects_root / "billing-rewrite"
        for d in _PROJECT_DIRS:
            assert (root / d).is_dir(), f"Missing directory: {d}"

        # Starter files
        assert (root / "control" / "draft-plan.md").is_file()
        assert (root / "coordination" / "status-board.md").is_file()
        assert (root / "coordination" / "dependencies.md").is_file()
        assert (root / "coordination" / "integration-points.md").is_file()

    def test_reports_written_files(self, projects_root):
        result = json.loads(workflow_create_project("Test Project"))
        assert "control/draft-plan.md" in result["files_written"]
        assert "coordination/status-board.md" in result["files_written"]
        assert len(result["files_skipped"]) == 0

    def test_does_not_overwrite_existing(self, projects_root):
        workflow_create_project("My Project")
        root = projects_root / "my-project"
        (root / "control" / "draft-plan.md").write_text("custom content")

        result = json.loads(workflow_create_project("My Project"))
        assert result["success"] is True
        assert "control/draft-plan.md" in result["files_skipped"]
        assert (root / "control" / "draft-plan.md").read_text() == "custom content"

    def test_empty_name_returns_error(self, projects_root):
        result = json.loads(workflow_create_project(""))
        assert "error" in result

    def test_whitespace_only_name_returns_error(self, projects_root):
        result = json.loads(workflow_create_project("   "))
        assert "error" in result

    def test_unsluggable_name_returns_error(self, projects_root):
        result = json.loads(workflow_create_project("!!!"))
        assert "error" in result


# =========================================================================
# workflow_approve_plan
# =========================================================================

class TestWorkflowApprovePlan:
    def _setup_project_with_plan(self, projects_root, plan_content="# The Plan\n",
                                  plan_file="plan.md", plan_state=None):
        """Helper: create a project with one plan."""
        workflow_create_project("Approve Test")
        root = projects_root / "approve-test"
        plan_dir = root / "plans" / "my-plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / plan_file).write_text(plan_content)
        if plan_state:
            write_json_file(plan_dir / "plan-state.json", plan_state)
        return root

    def test_approve_with_plan_md(self, projects_root):
        self._setup_project_with_plan(projects_root)
        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert result["success"] is True
        assert result["approved_from"] == "plan.md"

        root = projects_root / "approve-test"
        assert (root / "control" / "approved-plan.md").is_file()
        assert (root / "control" / "approved-plan.md").read_text() == "# The Plan\n"

        state = read_json_file(root / "plans" / "my-plan" / "plan-state.json")
        assert state["state"] == "approved"
        assert "approvedAt" in state

    def test_prefers_plan_v2(self, projects_root):
        self._setup_project_with_plan(projects_root, plan_content="v1")
        root = projects_root / "approve-test"
        (root / "plans" / "my-plan" / "plan-v2.md").write_text("v2 content")

        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert result["approved_from"] == "plan-v2.md"
        assert (root / "control" / "approved-plan.md").read_text() == "v2 content"

    def test_auto_discovers_single_plan(self, projects_root):
        self._setup_project_with_plan(projects_root)
        result = json.loads(workflow_approve_plan("Approve Test"))
        assert result["success"] is True
        assert result["plan_slug"] == "my-plan"

    def test_errors_on_multiple_plans_without_slug(self, projects_root):
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        (root / "plans" / "second-plan").mkdir()
        (root / "plans" / "second-plan" / "plan.md").write_text("another")

        result = json.loads(workflow_approve_plan("Approve Test"))
        assert "error" in result
        assert "Multiple" in result["error"]

    def test_errors_on_no_plans(self, projects_root):
        workflow_create_project("Empty Project")
        result = json.loads(workflow_approve_plan("Empty Project"))
        assert "error" in result

    def test_errors_on_missing_plan_artifact(self, projects_root):
        workflow_create_project("No Artifact")
        root = projects_root / "no-artifact"
        (root / "plans" / "empty-plan").mkdir(parents=True)
        result = json.loads(workflow_approve_plan("No Artifact", "empty-plan"))
        assert "error" in result
        assert "No plan artifact" in result["error"]

    def test_errors_on_nonexistent_project(self, projects_root):
        result = json.loads(workflow_approve_plan("Nonexistent"))
        assert "error" in result

    def test_errors_on_already_decomposed(self, projects_root):
        self._setup_project_with_plan(
            projects_root,
            plan_state={"state": "decomposed"},
        )
        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert "error" in result
        assert "Cannot approve" in result["error"]

    def test_respects_plan_state_transitions(self, projects_root):
        self._setup_project_with_plan(
            projects_root,
            plan_state={"state": "draft"},
        )
        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert result["success"] is True

    def test_rejects_invalid_transition(self, projects_root):
        self._setup_project_with_plan(
            projects_root,
            plan_state={"state": "approved"},
        )
        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert "error" in result

    def test_unsluggable_name_returns_error(self, projects_root):
        result = json.loads(workflow_approve_plan("!!!"))
        assert "error" in result
        assert "slug" in result["error"].lower()

    def test_rejects_nested_plan_slug(self, projects_root):
        self._setup_project_with_plan(projects_root)
        result = json.loads(workflow_approve_plan("Approve Test", "foo/bar"))
        assert "error" in result
        assert "Invalid plan_slug" in result["error"]

    # --- Path traversal (finding #1) ---

    def test_rejects_path_traversal_in_plan_slug(self, projects_root):
        self._setup_project_with_plan(projects_root)
        result = json.loads(
            workflow_approve_plan("Approve Test", "../../other-project/plans/x")
        )
        assert "error" in result
        assert "Invalid plan_slug" in result["error"]

    def test_rejects_dot_dot_plan_slug(self, projects_root):
        self._setup_project_with_plan(projects_root)
        result = json.loads(workflow_approve_plan("Approve Test", ".."))
        assert "error" in result

    def test_approve_rolls_back_when_batch_write_fails(self, projects_root):
        """If the approval's file writes fail mid-way, neither
        control/approved-plan.md nor plan-state.json should be left in
        a partially-updated state."""
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        approved_path = root / "control" / "approved-plan.md"
        state_path = root / "plans" / "my-plan" / "plan-state.json"
        assert not approved_path.exists()
        before_state_raw = read_json_file(state_path)

        with patch.object(
            workflow_tools_module,
            "_atomic_write_many_text",
            side_effect=OSError("disk full"),
        ):
            result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))

        assert "error" in result
        assert not approved_path.exists(), (
            "approved-plan.md must not be written when the batch write fails"
        )
        # plan-state.json should be unchanged (either still missing or unchanged shape).
        assert read_json_file(state_path) == before_state_raw

    # --- Corrupted JSON (finding #3) ---

    def test_errors_on_corrupt_plan_state_json(self, projects_root):
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        (root / "plans" / "my-plan" / "plan-state.json").write_text("not json{{{")

        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert "error" in result
        assert "corrupted" in result["error"]

    # --- Wrong-shape plan-state.json (finding #2) ---

    def test_errors_on_list_shaped_plan_state(self, projects_root):
        """plan-state.json is valid JSON but a list, not a dict."""
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        write_json_file(root / "plans" / "my-plan" / "plan-state.json", ["not", "a", "dict"])

        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert "error" in result
        assert "unexpected shape" in result["error"]

    def test_errors_on_string_shaped_plan_state(self, projects_root):
        """plan-state.json is valid JSON but a bare string."""
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        write_json_file(root / "plans" / "my-plan" / "plan-state.json", "approved")

        result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
        assert "error" in result
        assert "unexpected shape" in result["error"]

    def test_errors_on_unhashable_plan_state_value(self, projects_root):
        """plan-state.json has a list/dict as its 'state' value — must not crash."""
        self._setup_project_with_plan(projects_root)
        root = projects_root / "approve-test"
        for bad in ([], {}):
            write_json_file(root / "plans" / "my-plan" / "plan-state.json", {"state": bad})
            result = json.loads(workflow_approve_plan("Approve Test", "my-plan"))
            assert "error" in result


# =========================================================================
# workflow_status
# =========================================================================

class TestWorkflowStatus:
    def test_status_of_new_project(self, projects_root):
        workflow_create_project("Status Test")
        result = json.loads(workflow_status("Status Test"))
        assert result["success"] is True
        assert result["project_slug"] == "status-test"
        assert result["approved_plan_exists"] is False
        assert result["workstreams"] == []
        assert result["plans"] == []
        assert result["coordination"]["has_status_board"] is True

    def test_status_after_plan_approval(self, projects_root):
        workflow_create_project("With Plan")
        root = projects_root / "with-plan"
        plan_dir = root / "plans" / "the-plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text("# Plan")
        write_json_file(plan_dir / "plan-state.json", {"state": "under_review"})

        workflow_approve_plan("With Plan", "the-plan")

        result = json.loads(workflow_status("With Plan"))
        assert result["approved_plan_exists"] is True
        assert len(result["plans"]) == 1
        assert result["plans"][0]["state"] == "approved"

    def test_status_reports_all_plans(self, projects_root):
        """Finding #4: status should report all plans, not just the first."""
        workflow_create_project("Multi Plan")
        root = projects_root / "multi-plan"

        for slug, state in [("alpha", "draft"), ("beta", "approved")]:
            d = root / "plans" / slug
            d.mkdir(parents=True)
            (d / "plan.md").write_text(f"# {slug}")
            write_json_file(d / "plan-state.json", {"state": state})

        result = json.loads(workflow_status("Multi Plan"))
        assert len(result["plans"]) == 2
        slugs = {p["slug"] for p in result["plans"]}
        assert slugs == {"alpha", "beta"}

    def test_status_with_nanoclaw_task_state(self, projects_root):
        """Finding #2: NanoClaw task-state.json uses object shape with 'status' field."""
        workflow_create_project("NC Test")
        root = projects_root / "nc-test"

        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        (ws / "scope.md").write_text("# Backend scope")
        (ws / "tasks.md").write_text("- [ ] Task 1")
        # NanoClaw canonical shape
        write_json_file(ws / "task-state.json", {
            "tasks": [
                {"id": 1, "status": "pending", "reviewRounds": 0},
                {"id": 2, "status": "in_progress", "reviewRounds": 0},
                {"id": 3, "status": "approved", "reviewRounds": 1},
            ],
            "currentTask": 2,
            "lastReviewedBy": "argus",
        })
        write_json_file(root / "workstreams" / "manifest.json", {
            "backend": {
                "owner": "atlas",
                "reviewer": "argus",
                "completionMode": "code",
            }
        })

        result = json.loads(workflow_status("NC Test"))
        assert len(result["workstreams"]) == 1
        ws_info = result["workstreams"][0]
        assert ws_info["stream"] == "backend"
        assert ws_info["has_scope"] is True
        assert ws_info["has_tasks"] is True
        assert ws_info["task_state"]["total"] == 3
        assert ws_info["task_state"]["by_status"]["pending"] == 1
        assert ws_info["task_state"]["by_status"]["in_progress"] == 1
        assert ws_info["task_state"]["by_status"]["approved"] == 1
        assert ws_info["task_state"]["currentTask"] == 2
        assert ws_info["task_state"]["lastReviewedBy"] == "argus"
        assert ws_info["owner"] == "atlas"
        assert ws_info["reviewer"] == "argus"
        assert ws_info["completionMode"] == "code"

    def test_status_with_legacy_list_task_state(self, projects_root):
        """Legacy flat-list shape should still work."""
        workflow_create_project("Legacy Test")
        root = projects_root / "legacy-test"

        ws = root / "workstreams" / "frontend"
        ws.mkdir(parents=True)
        write_json_file(ws / "task-state.json", [
            {"id": 1, "title": "Task 1", "status": "pending"},
            {"id": 2, "title": "Task 2", "status": "approved"},
        ])

        result = json.loads(workflow_status("Legacy Test"))
        ws_info = result["workstreams"][0]
        assert ws_info["task_state"]["total"] == 2
        assert ws_info["task_state"]["by_status"]["pending"] == 1
        assert ws_info["task_state"]["by_status"]["approved"] == 1

    def test_status_with_no_manifest(self, projects_root):
        """Finding #1: missing manifest should not crash."""
        workflow_create_project("No Manifest")
        root = projects_root / "no-manifest"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        (ws / "scope.md").write_text("# Scope")

        result = json.loads(workflow_status("No Manifest"))
        assert result["success"] is True
        assert len(result["workstreams"]) == 1
        ws_info = result["workstreams"][0]
        assert ws_info["stream"] == "backend"
        assert "owner" not in ws_info  # no manifest, no owner info

    def test_status_with_corrupt_manifest(self, projects_root):
        """Finding #1: corrupt manifest should not crash."""
        workflow_create_project("Bad Manifest")
        root = projects_root / "bad-manifest"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        (ws / "scope.md").write_text("# Scope")
        (root / "workstreams" / "manifest.json").write_text("not json{{{")

        result = json.loads(workflow_status("Bad Manifest"))
        assert result["success"] is True
        assert result["manifest_error"] == "corrupt-manifest"
        assert len(result["workstreams"]) == 1

    def test_status_with_non_dict_manifest_entry(self, projects_root):
        """Manifest entry is not a dict (e.g. a string) — degrade, not crash."""
        workflow_create_project("Flat Manifest")
        root = projects_root / "flat-manifest"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        (ws / "scope.md").write_text("# Scope")
        write_json_file(root / "workstreams" / "manifest.json", {
            "backend": "atlas",  # wrong shape — should be a dict
        })

        result = json.loads(workflow_status("Flat Manifest"))
        assert result["success"] is True
        ws_info = result["workstreams"][0]
        assert "owner" not in ws_info  # non-dict entry skipped

    def test_status_with_non_list_tasks_field(self, projects_root):
        """task-state.json has 'tasks' key but it's not a list — degrade, not crash."""
        workflow_create_project("Bad Tasks Shape")
        root = projects_root / "bad-tasks-shape"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        write_json_file(ws / "task-state.json", {
            "tasks": "not a list",
            "currentTask": None,
        })

        result = json.loads(workflow_status("Bad Tasks Shape"))
        ws_info = result["workstreams"][0]
        assert ws_info["task_state"]["error"] == "invalid-tasks-shape"

    def test_status_with_non_dict_task_entries(self, projects_root):
        """task-state.json tasks list contains non-dict entries — skip them."""
        workflow_create_project("Mixed Tasks")
        root = projects_root / "mixed-tasks"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        write_json_file(ws / "task-state.json", {
            "tasks": [
                {"id": 1, "status": "pending"},
                42,        # not a dict
                "garbage",  # not a dict
            ],
            "currentTask": 1,
            "lastReviewedBy": None,
        })

        result = json.loads(workflow_status("Mixed Tasks"))
        ws_info = result["workstreams"][0]
        assert ws_info["task_state"]["total"] == 3
        # Only the one valid dict entry should be counted
        assert ws_info["task_state"]["by_status"] == {"pending": 1}

    def test_status_with_corrupt_task_state(self, projects_root):
        """Corrupt task-state.json should report error, not crash."""
        workflow_create_project("Bad Tasks")
        root = projects_root / "bad-tasks"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        (ws / "task-state.json").write_text("broken{{{")

        result = json.loads(workflow_status("Bad Tasks"))
        ws_info = result["workstreams"][0]
        assert ws_info["task_state"]["error"] == "corrupt-task-state-file"

    def test_status_with_unhashable_task_status(self, projects_root):
        """A task whose status is a list/dict (JSON-valid but unhashable) must
        not crash the status renderer — it should summarize gracefully.
        """
        workflow_create_project("Weird Status")
        root = projects_root / "weird-status"
        ws = root / "workstreams" / "backend"
        ws.mkdir(parents=True)
        write_json_file(
            ws / "task-state.json",
            {"tasks": [
                {"id": 1, "status": []},
                {"id": 2, "status": {}},
                {"id": 3, "status": "in_progress"},
            ], "currentTask": 1, "lastReviewedBy": None},
        )

        result = json.loads(workflow_status("Weird Status"))
        ws_info = result["workstreams"][0]
        assert ws_info["task_state"]["total"] == 3
        # The well-formed entry should still be counted correctly.
        assert ws_info["task_state"]["by_status"].get("in_progress") == 1

    def test_status_with_corrupt_plan_state(self, projects_root):
        """Corrupt plan-state.json should report state, not crash."""
        workflow_create_project("Bad Plan State")
        root = projects_root / "bad-plan-state"
        plan_dir = root / "plans" / "alpha"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text("# Plan")
        (plan_dir / "plan-state.json").write_text("broken{{{")

        result = json.loads(workflow_status("Bad Plan State"))
        assert result["plans"][0]["state"] == "corrupt-state-file"

    def test_status_with_wrong_shape_plan_state(self, projects_root):
        """List-shaped plan-state.json should degrade, not crash."""
        workflow_create_project("List Plan State")
        root = projects_root / "list-plan-state"
        plan_dir = root / "plans" / "alpha"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text("# Plan")
        write_json_file(plan_dir / "plan-state.json", ["not", "a", "dict"])

        result = json.loads(workflow_status("List Plan State"))
        assert result["plans"][0]["state"] == "invalid-state-shape"

    def test_status_nonexistent_project(self, projects_root):
        result = json.loads(workflow_status("Nonexistent"))
        assert "error" in result

    def test_status_empty_name(self, projects_root):
        result = json.loads(workflow_status(""))
        assert "error" in result

    def test_status_unsluggable_name(self, projects_root):
        result = json.loads(workflow_status("!!!"))
        assert "error" in result
        assert "slug" in result["error"].lower()


# =========================================================================
# check_workflow_requirements
# =========================================================================

class TestCheckRequirements:
    def test_always_available(self):
        assert check_workflow_requirements() is True


# =========================================================================
# Plan state transition invariants
# =========================================================================

class TestPlanTransitions:
    def test_all_states_have_transition_entry(self):
        for state in VALID_PLAN_STATES:
            assert state in PLAN_TRANSITIONS

    def test_approved_can_reach_decomposed(self):
        assert "decomposed" in PLAN_TRANSITIONS["approved"]

    def test_decomposed_is_terminal(self):
        assert PLAN_TRANSITIONS["decomposed"] == set()

    def test_draft_cannot_reach_decomposed(self):
        assert "decomposed" not in PLAN_TRANSITIONS["draft"]


# =========================================================================
# Task state transition invariants
# =========================================================================

class TestTaskTransitions:
    def test_all_states_have_transition_entry(self):
        for s in VALID_TASK_STATES:
            assert s in TASK_TRANSITIONS

    def test_approved_is_terminal(self):
        assert TASK_TRANSITIONS["approved"] == set()

    def test_in_review_can_reach_approved(self):
        assert "approved" in TASK_TRANSITIONS["in_review"]

    def test_changes_requested_returns_to_in_progress(self):
        assert TASK_TRANSITIONS["changes_requested"] == {"in_progress"}

    def test_pending_cannot_skip_to_approved(self):
        assert "approved" not in TASK_TRANSITIONS["pending"]


# =========================================================================
# workflow_decompose
# =========================================================================

class TestWorkflowDecompose:
    def _valid_streams(self):
        return [
            {
                "name": "backend",
                "owner": "atlas",
                "reviewer": "argus",
                "completionMode": "code",
                "dependencies": [],
                "acceptanceCriteria": ["Auth middleware validates JWTs."],
                "scope": "# Backend\n\nAuth + persistence.\n",
            },
            {
                "name": "qa",
                "owner": "argus",
                "reviewer": None,
                "completionMode": "report",
                "dependencies": ["backend"],
                "acceptanceCriteria": ["QA report delivered."],
            },
        ]

    def test_happy_path_creates_manifest_and_streams(self, projects_root):
        root, _ = _bootstrap_approved_project(projects_root)
        result = json.loads(workflow_decompose(
            "Decomp Test",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        ))
        assert result["success"] is True
        assert set(result["streams_created"]) == {"backend", "qa"}

        manifest = read_json_file(root / "workstreams" / "manifest.json")
        assert manifest["backend"]["owner"] == "atlas"
        assert manifest["backend"]["reviewer"] == "argus"
        assert manifest["backend"]["completionMode"] == "code"
        assert manifest["qa"]["reviewer"] is None
        assert manifest["qa"]["dependencies"] == ["backend"]

        for name in ("backend", "qa"):
            sdir = root / "workstreams" / name
            assert (sdir / "scope.md").is_file()
            assert (sdir / "tasks.md").is_file()
            assert (sdir / "progress.md").is_file()
            assert (sdir / "handoffs.md").is_file()
            ts = read_json_file(sdir / "task-state.json")
            assert ts == {"tasks": [], "currentTask": None, "lastReviewedBy": None}

    def test_updates_plan_state_to_decomposed(self, projects_root):
        _bootstrap_approved_project(projects_root)
        workflow_decompose(
            "Decomp Test",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        )
        state = read_json_file(
            projects_root / "decomp-test" / "plans" / "the-plan" / "plan-state.json"
        )
        assert state["state"] == "decomposed"
        assert "decomposedAt" in state

    def test_requires_approved_plan(self, projects_root):
        """No approved-plan.md ⇒ refuse."""
        workflow_create_project("No Approval")
        result = json.loads(workflow_decompose(
            "No Approval", streams=self._valid_streams()
        ))
        assert "error" in result
        assert "approved plan" in result["error"].lower()

    def test_refuses_if_manifest_already_exists(self, projects_root):
        root, _ = _bootstrap_approved_project(projects_root)
        # First decomposition succeeds.
        workflow_decompose("Decomp Test", streams=self._valid_streams(),
                           plan_slug="the-plan")
        # Second refuses.
        result = json.loads(workflow_decompose(
            "Decomp Test", streams=self._valid_streams(), plan_slug="the-plan"
        ))
        assert "error" in result
        assert "already" in result["error"].lower()

    def test_empty_streams_rejected(self, projects_root):
        _bootstrap_approved_project(projects_root)
        result = json.loads(workflow_decompose("Decomp Test", streams=[]))
        assert "error" in result

    def test_code_stream_requires_reviewer(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas", "reviewer": None,
            "completionMode": "code", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "reviewer" in result["error"].lower()

    def test_code_stream_reviewer_must_differ_from_owner(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas", "reviewer": "atlas",
            "completionMode": "code", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "distinct" in result["error"].lower() or "same" in result["error"].lower()

    def test_invalid_completion_mode(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "x", "owner": "atlas", "reviewer": "argus",
            "completionMode": "bogus", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "completionMode" in result["error"] or "completion mode" in result["error"].lower()

    def test_stream_name_must_be_simple(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "../evil", "owner": "atlas", "reviewer": "argus",
            "completionMode": "code", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result

    def test_duplicate_stream_names_rejected(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [
            {"name": "backend", "owner": "a", "reviewer": "b",
             "completionMode": "code", "acceptanceCriteria": ["x"]},
            {"name": "backend", "owner": "c", "reviewer": "d",
             "completionMode": "code", "acceptanceCriteria": ["x"]},
        ]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "duplicate" in result["error"].lower()

    def test_dependency_must_reference_known_stream(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas", "reviewer": "argus",
            "completionMode": "code", "dependencies": ["nonexistent"],
            "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "dependency" in result["error"].lower() or "dependencies" in result["error"].lower()

    def test_missing_acceptance_criteria_rejected(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas", "reviewer": "argus",
            "completionMode": "code", "acceptanceCriteria": [],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "acceptance" in result["error"].lower()

    def test_missing_owner_rejected(self, projects_root):
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "", "reviewer": "argus",
            "completionMode": "code", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose("Decomp Test", streams=streams))
        assert "error" in result
        assert "owner" in result["error"].lower()

    def test_scope_defaults_when_omitted(self, projects_root):
        root, _ = _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas", "reviewer": "argus",
            "completionMode": "code", "acceptanceCriteria": ["Auth works."],
        }]
        result = json.loads(workflow_decompose(
            "Decomp Test", streams=streams, plan_slug="the-plan"
        ))
        assert result["success"] is True
        scope = (root / "workstreams" / "backend" / "scope.md").read_text()
        assert "backend" in scope.lower()
        # The criterion should be embedded.
        assert "Auth works." in scope

    def test_nonexistent_project_rejected(self, projects_root):
        result = json.loads(workflow_decompose(
            "Nope", streams=self._valid_streams()
        ))
        assert "error" in result

    def test_refuses_when_resolved_plan_not_approved(self, projects_root):
        """approved-plan.md may be from plan A, but decomposing plan B
        (still in draft/under_review) must not rewrite B's state to 'decomposed'.
        """
        # Bootstrap an approved plan (creates control/approved-plan.md).
        root, _ = _bootstrap_approved_project(projects_root, "Two Plans")
        # Create a second plan in draft state.
        other = root / "plans" / "other-plan"
        other.mkdir(parents=True)
        (other / "plan.md").write_text("# Other\n")
        write_json_file(other / "plan-state.json", {"state": "draft"})

        result = json.loads(workflow_decompose(
            "Two Plans",
            streams=self._valid_streams(),
            plan_slug="other-plan",
        ))
        assert "error" in result
        assert "approved" in result["error"].lower()
        # draft plan state must be unchanged.
        state = read_json_file(other / "plan-state.json")
        assert state["state"] == "draft"
        # No manifest should have been written.
        assert not (root / "workstreams" / "manifest.json").exists()

    def test_refuses_when_approved_plan_file_does_not_match_resolved_plan(self, projects_root):
        root, _ = _bootstrap_approved_project(projects_root, "Two Approved Plans")
        other = root / "plans" / "other-plan"
        other.mkdir(parents=True)
        (other / "plan.md").write_text("# Other approved plan\n", encoding="utf-8")
        write_json_file(other / "plan-state.json", {"state": "approved"})

        result = json.loads(workflow_decompose(
            "Two Approved Plans",
            streams=self._valid_streams(),
            plan_slug="other-plan",
        ))

        assert "error" in result
        assert "does not match" in result["error"].lower()
        assert not (root / "workstreams" / "manifest.json").exists()

    def test_finalize_failure_does_not_leave_visible_partial_streams(self, projects_root, monkeypatch):
        root, _ = _bootstrap_approved_project(projects_root, "Finalize Fail")
        original_replace = workflow_tools_module.Path.replace

        def flaky_replace(self, target):
            if self.name == "workstreams" and Path(target).name == "workstreams":
                raise OSError("simulated finalize failure")
            return original_replace(self, target)

        monkeypatch.setattr(workflow_tools_module.Path, "replace", flaky_replace)

        result = json.loads(workflow_decompose(
            "Finalize Fail",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        ))

        assert "error" in result
        assert "finalize" in result["error"].lower()
        assert list((root / "workstreams").iterdir()) == []
        staging_root = root / ".workflow-staging"
        if staging_root.exists():
            assert list(staging_root.iterdir()) == []

    def test_plan_state_write_failure_rolls_back_decomposition(self, projects_root, monkeypatch):
        """If the plan-state write fails after the workstreams swap, the live
        workstreams/ tree must be rolled back to empty and plan-state restored,
        so the repo doesn't end up with decomposed streams but an 'approved' plan.
        """
        root, plan_dir = _bootstrap_approved_project(projects_root, "Rollback Fail")
        original_plan_state_text = (plan_dir / "plan-state.json").read_text(encoding="utf-8")

        original_write_json = workflow_tools_module.write_json_file
        state_path = plan_dir / "plan-state.json"

        def flaky_write_json(path, data):
            if Path(path) == state_path and isinstance(data, dict) and data.get("state") == "decomposed":
                raise OSError("simulated plan-state write failure")
            return original_write_json(path, data)

        monkeypatch.setattr(workflow_tools_module, "write_json_file", flaky_write_json)

        result = json.loads(workflow_decompose(
            "Rollback Fail",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        ))

        assert "error" in result
        # workstreams/ must be back to empty (no manifest, no stream dirs).
        assert list((root / "workstreams").iterdir()) == []
        # plan-state.json must still read as 'approved'.
        assert (plan_dir / "plan-state.json").read_text(encoding="utf-8") == original_plan_state_text

    def test_plan_state_write_nonoserror_also_rolls_back(self, projects_root, monkeypatch):
        """Rollback must cover any post-swap write failure, not only OSError."""
        root, plan_dir = _bootstrap_approved_project(projects_root, "Rollback NonOS")
        original_plan_state_text = (plan_dir / "plan-state.json").read_text(encoding="utf-8")

        original_write_json = workflow_tools_module.write_json_file
        state_path = plan_dir / "plan-state.json"

        def flaky_write_json(path, data):
            if Path(path) == state_path and isinstance(data, dict) and data.get("state") == "decomposed":
                raise RuntimeError("simulated non-OS plan-state failure")
            return original_write_json(path, data)

        monkeypatch.setattr(workflow_tools_module, "write_json_file", flaky_write_json)

        result = json.loads(workflow_decompose(
            "Rollback NonOS",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        ))

        assert "error" in result
        assert list((root / "workstreams").iterdir()) == []
        assert (plan_dir / "plan-state.json").read_text(encoding="utf-8") == original_plan_state_text

    def test_refuses_if_stream_dirs_exist_without_manifest(self, projects_root):
        """Partial prior decomposition (stream dirs present, manifest missing)
        must refuse rather than silently resetting task-state.json.
        """
        root, _ = _bootstrap_approved_project(projects_root, "Partial Decomp")
        # Simulate a partial run: stream dir with work, no manifest.
        leftover = root / "workstreams" / "backend"
        leftover.mkdir(parents=True)
        (leftover / "scope.md").write_text("# old scope\n")
        write_json_file(
            leftover / "task-state.json",
            {"tasks": [{"id": 1, "status": "approved"}],
             "currentTask": 1, "lastReviewedBy": "argus"},
        )

        result = json.loads(workflow_decompose(
            "Partial Decomp",
            streams=self._valid_streams(),
            plan_slug="the-plan",
        ))
        assert "error" in result
        # Task state must be untouched.
        ts = read_json_file(leftover / "task-state.json")
        assert ts["tasks"] == [{"id": 1, "status": "approved"}]
        assert ts["lastReviewedBy"] == "argus"

    def test_whitespace_owner_reviewer_rejected_as_same(self, projects_root):
        """'atlas ' and 'atlas' must not count as distinct identities."""
        _bootstrap_approved_project(projects_root)
        streams = [{
            "name": "backend", "owner": "atlas ", "reviewer": "atlas",
            "completionMode": "code", "acceptanceCriteria": ["x"],
        }]
        result = json.loads(workflow_decompose(
            "Decomp Test", streams=streams, plan_slug="the-plan"
        ))
        assert "error" in result
        assert "distinct" in result["error"].lower() or "same" in result["error"].lower()

    def test_non_string_dependency_rejected(self, projects_root):
        """Dependency elements that aren't strings must yield a tool_error,
        never a TypeError from set-membership checks.
        """
        _bootstrap_approved_project(projects_root)
        for bad in ([[]], [{}], [123], [None]):
            streams = [{
                "name": "backend", "owner": "atlas", "reviewer": "argus",
                "completionMode": "code", "dependencies": bad,
                "acceptanceCriteria": ["x"],
            }]
            result = json.loads(workflow_decompose(
                "Decomp Test", streams=streams, plan_slug="the-plan"
            ))
            assert "error" in result, f"dependencies={bad!r} should have been rejected"

    def test_non_string_completion_mode_rejected(self, projects_root):
        """completionMode that isn't a string must yield tool_error, not TypeError."""
        _bootstrap_approved_project(projects_root)
        for bad in ([], {}, 123, None):
            streams = [{
                "name": "backend", "owner": "atlas", "reviewer": "argus",
                "completionMode": bad, "acceptanceCriteria": ["x"],
            }]
            result = json.loads(workflow_decompose(
                "Decomp Test", streams=streams, plan_slug="the-plan"
            ))
            assert "error" in result, f"completionMode={bad!r} should have been rejected"


# =========================================================================
# workflow_handoff
# =========================================================================

class TestWorkflowHandoff:
    def _setup_two_streams(self, projects_root):
        root, _ = _bootstrap_approved_project(projects_root, "Handoff Test")
        workflow_decompose(
            "Handoff Test",
            streams=[
                {"name": "backend", "owner": "atlas", "reviewer": "argus",
                 "completionMode": "code", "acceptanceCriteria": ["x"]},
                {"name": "frontend", "owner": "apollo", "reviewer": "argus",
                 "completionMode": "code", "acceptanceCriteria": ["x"]},
            ],
            plan_slug="the-plan",
        )
        return root

    def test_happy_path_writes_both_handoffs_files(self, projects_root):
        root = self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test",
            from_stream="backend",
            to_stream="frontend",
            description="API schema ready for UI",
        ))
        assert result["success"] is True
        assert result["from_stream"] == "backend"
        assert result["to_stream"] == "frontend"
        assert result["status"] == "pending"

        fh = (root / "workstreams" / "backend" / "handoffs.md").read_text()
        th = (root / "workstreams" / "frontend" / "handoffs.md").read_text()
        assert "API schema ready for UI" in fh
        assert "API schema ready for UI" in th
        assert "backend -> frontend" in fh
        assert "backend -> frontend" in th

    def test_custom_status_recorded(self, projects_root):
        root = self._setup_two_streams(projects_root)
        workflow_handoff(
            "Handoff Test", from_stream="backend", to_stream="frontend",
            description="done", status="ready",
        )
        fh = (root / "workstreams" / "backend" / "handoffs.md").read_text()
        assert "ready" in fh

    def test_sanitizes_markdown_injection(self, projects_root):
        root = self._setup_two_streams(projects_root)
        workflow_handoff(
            "Handoff Test",
            from_stream="backend",
            to_stream="frontend",
            description="# Injected\n- [ ] fake task `boom`",
            artifact_path="evidence/report.md\n## Spoofed",
        )

        handoff_md = (root / "workstreams" / "backend" / "handoffs.md").read_text()
        assert "# Injected\n- [ ] fake task `boom`" not in handoff_md
        assert r"\# Injected\n- [ ] fake task \`boom\`" in handoff_md
        assert r"artifact: evidence/report.md\n## Spoofed" in handoff_md
        assert "\n## Spoofed" not in handoff_md
        assert len([line for line in handoff_md.splitlines() if line.startswith("- [")]) == 1

    def test_rolls_back_if_second_handoff_write_fails(self, projects_root, monkeypatch):
        root = self._setup_two_streams(projects_root)
        backend_path = root / "workstreams" / "backend" / "handoffs.md"
        frontend_path = root / "workstreams" / "frontend" / "handoffs.md"
        backend_before = backend_path.read_text()
        frontend_before = frontend_path.read_text()

        original_replace = workflow_tools_module.os.replace
        state = {"handoff_replaces": 0}

        def flaky_replace(src, dst):
            dst_path = Path(dst)
            if dst_path.name == "handoffs.md":
                state["handoff_replaces"] += 1
                if state["handoff_replaces"] == 2:
                    raise OSError("simulated second write failure")
            return original_replace(src, dst)

        monkeypatch.setattr(workflow_tools_module.os, "replace", flaky_replace)

        result = json.loads(workflow_handoff(
            "Handoff Test",
            from_stream="backend",
            to_stream="frontend",
            description="API schema ready for UI",
        ))

        assert "error" in result
        assert backend_path.read_text() == backend_before
        assert frontend_path.read_text() == frontend_before

    def test_invalid_status_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="backend", to_stream="frontend",
            description="x", status="bogus",
        ))
        assert "error" in result
        assert "status" in result["error"].lower()

    def test_non_string_status_rejected(self, projects_root):
        """status=[]/{}/123 must yield tool_error, not TypeError."""
        self._setup_two_streams(projects_root)
        for bad in ([], {}, 123):
            result = json.loads(workflow_handoff(
                "Handoff Test", from_stream="backend", to_stream="frontend",
                description="x", status=bad,
            ))
            assert "error" in result, f"status={bad!r} should have been rejected"

    def test_same_stream_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="backend", to_stream="backend",
            description="x",
        ))
        assert "error" in result

    def test_missing_from_stream_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="nope", to_stream="frontend",
            description="x",
        ))
        assert "error" in result
        assert "stream" in result["error"].lower()

    def test_missing_to_stream_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="backend", to_stream="nope",
            description="x",
        ))
        assert "error" in result

    def test_empty_description_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="backend", to_stream="frontend",
            description="   ",
        ))
        assert "error" in result
        assert "description" in result["error"].lower()

    def test_path_traversal_in_stream_names_rejected(self, projects_root):
        self._setup_two_streams(projects_root)
        result = json.loads(workflow_handoff(
            "Handoff Test", from_stream="../etc", to_stream="frontend",
            description="x",
        ))
        assert "error" in result

    def test_nonexistent_project_rejected(self, projects_root):
        result = json.loads(workflow_handoff(
            "Nope", from_stream="a", to_stream="b", description="x",
        ))
        assert "error" in result


# =========================================================================
# workflow_checkpoint
# =========================================================================

class TestWorkflowCheckpoint:
    def _setup_with_task(self, projects_root, completion_mode="code",
                         initial_status="pending", owner="atlas", reviewer="argus"):
        root, _ = _bootstrap_approved_project(projects_root, "Check Test")
        workflow_decompose(
            "Check Test",
            streams=[{
                "name": "backend", "owner": owner, "reviewer": reviewer,
                "completionMode": completion_mode,
                "acceptanceCriteria": ["x"],
            }],
            plan_slug="the-plan",
        )
        write_json_file(
            root / "workstreams" / "backend" / "task-state.json",
            {
                "tasks": [
                    {"id": 1, "title": "Task 1", "status": initial_status,
                     "owner": owner, "reviewer": reviewer, "reviewRounds": 0},
                ],
                "currentTask": 1,
                "lastReviewedBy": None,
            },
        )
        return root

    def test_note_only_appends_progress(self, projects_root):
        root = self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", note="starting work",
        ))
        assert result["success"] is True
        progress = (root / "workstreams" / "backend" / "progress.md").read_text()
        assert "starting work" in progress

    def test_status_transition_updates_task_state(self, projects_root):
        root = self._setup_with_task(projects_root, initial_status="pending")
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="in_progress", note="kicked off", actor="atlas",
        ))
        assert result["success"] is True
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        task = ts["tasks"][0]
        assert task["status"] == "in_progress"
        assert "updatedAt" in task

    def test_invalid_transition_rejected(self, projects_root):
        """pending -> approved is not allowed."""
        self._setup_with_task(projects_root, initial_status="pending")
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="skip", actor="argus",
        ))
        assert "error" in result
        assert "transition" in result["error"].lower() or "allowed" in result["error"].lower()

    def test_invalid_transition_does_not_append_progress(self, projects_root):
        root = self._setup_with_task(projects_root, initial_status="pending")
        progress_path = root / "workstreams" / "backend" / "progress.md"
        before = progress_path.read_text()

        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="skip", actor="argus",
        ))

        assert "error" in result
        assert progress_path.read_text() == before

    def test_unknown_task_does_not_append_progress(self, projects_root):
        root = self._setup_with_task(projects_root)
        progress_path = root / "workstreams" / "backend" / "progress.md"
        before = progress_path.read_text()

        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=999,
            new_status="in_progress", note="ghost task", actor="atlas",
        ))

        assert "error" in result
        assert progress_path.read_text() == before

    def test_transition_rolls_back_when_batch_write_fails(self, projects_root):
        root = self._setup_with_task(projects_root, initial_status="pending")
        progress_path = root / "workstreams" / "backend" / "progress.md"
        state_path = root / "workstreams" / "backend" / "task-state.json"
        before_progress = progress_path.read_text(encoding="utf-8")
        before_state = read_json_file(state_path)

        with patch.object(
            workflow_tools_module,
            "_atomic_write_many_text",
            side_effect=OSError("disk full"),
        ):
            result = json.loads(workflow_checkpoint(
                "Check Test", stream="backend", task_id=1,
                new_status="in_progress", note="go", actor="atlas",
            ))

        assert "error" in result
        assert result["error"].startswith("failed to record checkpoint")
        assert progress_path.read_text(encoding="utf-8") == before_progress
        assert read_json_file(state_path) == before_state

    def test_code_stream_no_self_approve(self, projects_root):
        """A code-mode task cannot be moved to 'approved' by its own owner."""
        self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="lgtm", actor="atlas",
        ))
        assert "error" in result
        assert "self" in result["error"].lower() or "review" in result["error"].lower()

    def test_code_stream_reviewer_can_approve(self, projects_root):
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="lgtm", actor="argus",
        ))
        assert result["success"] is True
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "approved"
        assert ts["lastReviewedBy"] == "argus"

    def test_report_stream_allows_self_complete(self, projects_root):
        """Non-code streams may self-approve per spec."""
        root = self._setup_with_task(
            projects_root, completion_mode="report",
            initial_status="in_review", owner="argus", reviewer=None,
        )
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="report filed", actor="argus",
        ))
        assert result["success"] is True

    def test_unknown_task_id_rejected(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=999,
            new_status="in_progress", note="x", actor="atlas",
        ))
        assert "error" in result

    def test_unknown_stream_rejected(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="nope", note="x",
        ))
        assert "error" in result

    def test_empty_note_rejected(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", note="   ",
        ))
        assert "error" in result
        assert "note" in result["error"].lower()

    def test_invalid_new_status_rejected(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="bogus", note="x", actor="atlas",
        ))
        assert "error" in result

    def test_evidence_appended_to_task(self, projects_root):
        root = self._setup_with_task(projects_root, initial_status="in_progress")
        workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="implemented", note="built",
            actor="atlas",
            evidence=["evidence/task-1/test.log"],
        )
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert "evidence/task-1/test.log" in ts["tasks"][0].get("evidence", [])

    def test_sanitizes_markdown_injection_in_progress(self, projects_root):
        root = self._setup_with_task(projects_root)
        workflow_checkpoint(
            "Check Test",
            stream="backend",
            note="# Injected\n- [ ] fake task `boom`",
            actor="#reviewer",
        )

        progress = (root / "workstreams" / "backend" / "progress.md").read_text()
        assert "# Injected\n- [ ] fake task `boom`" not in progress
        assert r"(\#reviewer)" in progress
        assert r"\# Injected\n- [ ] fake task \`boom\`" in progress
        assert len([line for line in progress.splitlines() if line.startswith("- [")]) == 1

    def test_path_traversal_in_stream_rejected(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="../etc", note="x",
        ))
        assert "error" in result

    def test_missing_task_state_file_handled(self, projects_root):
        """Stream exists but task-state.json missing — status-only checkpoint still works."""
        root, _ = _bootstrap_approved_project(projects_root, "Check Test")
        workflow_decompose(
            "Check Test",
            streams=[{
                "name": "backend", "owner": "atlas", "reviewer": "argus",
                "completionMode": "code", "acceptanceCriteria": ["x"],
            }],
            plan_slug="the-plan",
        )
        # Remove the task-state.json created by decompose.
        (root / "workstreams" / "backend" / "task-state.json").unlink()
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", note="notes only",
        ))
        assert result["success"] is True

    def test_code_stream_approve_requires_actor(self, projects_root):
        """Approval on a code stream requires an explicit actor; omitting it
        must not slip past the review gate.
        """
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="no actor",
            # actor intentionally omitted
        ))
        assert "error" in result
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_self_approve_uses_manifest_owner(self, projects_root):
        """Self-approval guard must compare actor to manifest owner, not a
        task-level owner field (which may be absent or stale).
        """
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        # Strip the task-level owner field — manifest still says atlas owns backend.
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        ts = read_json_file(ts_path)
        ts["tasks"][0].pop("owner", None)
        write_json_file(ts_path, ts)

        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="sneaky", actor="atlas",
        ))
        assert "error" in result
        ts = read_json_file(ts_path)
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_third_party_cannot_approve(self, projects_root):
        """Only the manifest-designated reviewer may approve a code task."""
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="rubber stamp", actor="apollo",
        ))
        assert "error" in result
        assert "reviewer" in result["error"].lower()
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_approve_refuses_when_manifest_missing(self, projects_root):
        """If the manifest can't be read, the review gate must fail closed."""
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        # Remove the manifest to simulate corruption / missing file.
        (root / "workstreams" / "manifest.json").unlink()
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="no manifest", actor="argus",
        ))
        assert "error" in result
        assert "manifest" in result["error"].lower()
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_approve_refuses_when_manifest_entry_missing(self, projects_root):
        """Manifest present but stream entry absent → refuse rather than skip gate."""
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        # Strip the stream entry.
        mpath = root / "workstreams" / "manifest.json"
        write_json_file(mpath, {})
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="empty manifest", actor="argus",
        ))
        assert "error" in result
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_approve_refuses_when_manifest_entry_empty(self, projects_root):
        """Manifest stream entry present but empty dict → fail closed."""
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        mpath = root / "workstreams" / "manifest.json"
        write_json_file(mpath, {"backend": {}})
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="empty entry", actor="argus",
        ))
        assert "error" in result
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_code_stream_approve_refuses_when_reviewer_missing(self, projects_root):
        """Manifest stream entry declares completionMode=code but no reviewer → fail closed."""
        root = self._setup_with_task(
            projects_root, completion_mode="code",
            initial_status="in_review", owner="atlas", reviewer="argus",
        )
        mpath = root / "workstreams" / "manifest.json"
        write_json_file(mpath, {"backend": {"completionMode": "code", "owner": "atlas"}})
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="approved", note="no reviewer", actor="argus",
        ))
        assert "error" in result
        assert "reviewer" in result["error"].lower()
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"

    def test_task_id_matches_string_stored_ids(self, projects_root):
        """Integer task_id from coerced CLI calls must still match tasks
        whose JSON id is stored as a string.
        """
        root = self._setup_with_task(projects_root, initial_status="pending")
        # Rewrite the task with a string id to match spec flexibility.
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        ts = read_json_file(ts_path)
        ts["tasks"][0]["id"] = "1"
        write_json_file(ts_path, ts)

        # Call with int — the schema/coercion path would deliver this.
        result = json.loads(workflow_checkpoint(
            "Check Test", stream="backend", task_id=1,
            new_status="in_progress", note="go", actor="atlas",
        ))
        assert result["success"] is True
        ts = read_json_file(ts_path)
        assert ts["tasks"][0]["status"] == "in_progress"

    def test_unhashable_task_status_does_not_crash(self, projects_root):
        """A task with status=[] or status={} must yield a clean tool_error,
        not a TypeError from TASK_TRANSITIONS lookup.
        """
        root = self._setup_with_task(projects_root)
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        for bad in ([], {}):
            ts = read_json_file(ts_path)
            ts["tasks"][0]["status"] = bad
            write_json_file(ts_path, ts)
            result = json.loads(workflow_checkpoint(
                "Check Test", stream="backend", task_id=1,
                new_status="in_progress", note="x", actor="atlas",
            ))
            assert "error" in result, f"status={bad!r} should return tool_error"


# =========================================================================
# workflow_sync_tasks
# =========================================================================

class TestWorkflowSyncTasks:
    def _setup_stream(self, projects_root, completion_mode="code", reviewer="argus"):
        root, _ = _bootstrap_approved_project(projects_root, "Sync Test")
        workflow_decompose(
            "Sync Test",
            streams=[{
                "name": "backend",
                "owner": "atlas",
                "reviewer": reviewer,
                "completionMode": completion_mode,
                "acceptanceCriteria": ["Auth flow works."],
            }],
            plan_slug="the-plan",
        )
        return root

    def test_populates_empty_task_state_from_tasks_md(self, projects_root):
        root = self._setup_stream(projects_root)
        tasks_md = root / "workstreams" / "backend" / "tasks.md"
        tasks_md.write_text(
            "# Tasks\n\n"
            "- [x] Confirm API contract\n"
            "- [ ] Implement auth middleware\n"
            "- [ ] Add regression tests\n"
        )

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert result["success"] is True
        assert result["tasks_synced"] == 3
        assert result["approved_count"] == 1
        assert result["pending_count"] == 2
        assert result["current_task_id"] == 2

        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["currentTask"] == 2
        assert ts["lastReviewedBy"] is None
        assert ts["tasks"] == [
            {
                "id": 1,
                "title": "Confirm API contract",
                "status": "approved",
                "owner": "atlas",
                "reviewer": "argus",
                "dependsOn": [],
                "reviewRounds": 0,
            },
            {
                "id": 2,
                "title": "Implement auth middleware",
                "status": "pending",
                "owner": "atlas",
                "reviewer": "argus",
                "dependsOn": [],
                "reviewRounds": 0,
            },
            {
                "id": 3,
                "title": "Add regression tests",
                "status": "pending",
                "owner": "atlas",
                "reviewer": "argus",
                "dependsOn": [],
                "reviewRounds": 0,
            },
        ]

    def test_rejects_existing_non_empty_task_state_without_overwrite(self, projects_root):
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text("- [ ] New task\n")
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        write_json_file(ts_path, {
            "tasks": [
                {
                    "id": 1,
                    "title": "Existing task",
                    "status": "in_progress",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 0,
                },
            ],
            "currentTask": 1,
            "lastReviewedBy": None,
        })

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert "error" in result
        assert "overwrite" in result["error"].lower()

        ts = read_json_file(ts_path)
        assert ts["tasks"][0]["title"] == "Existing task"
        assert ts["tasks"][0]["status"] == "in_progress"

    def test_overwrite_replaces_existing_task_state_when_explicit(self, projects_root):
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "- [ ] Task A\n- [x] Task B\n"
        )
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        write_json_file(ts_path, {
            "tasks": [{"id": 99, "title": "Stale", "status": "blocked"}],
            "currentTask": 99,
            "lastReviewedBy": "argus",
        })

        result = json.loads(
            workflow_sync_tasks("Sync Test", stream="backend", overwrite=True)
        )
        assert result["success"] is True
        assert result["tasks_synced"] == 2
        assert result["current_task_id"] == 1

        ts = read_json_file(ts_path)
        assert ts["lastReviewedBy"] is None
        assert [task["id"] for task in ts["tasks"]] == [1, 2]
        assert [task["title"] for task in ts["tasks"]] == ["Task A", "Task B"]
        assert [task["status"] for task in ts["tasks"]] == ["pending", "approved"]

    def test_overwrite_drops_review_metadata_conservatively(self, projects_root):
        """Overwrite must not carry review metadata forward. Title-based
        identity is ambiguous across renames/reuses, so we rebuild task-state
        fresh from tasks.md. Durable review artifacts remain on disk."""
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "- [ ] Task A\n- [x] Task B\n"
        )
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        write_json_file(ts_path, {
            "tasks": [
                {
                    "id": 1,
                    "title": "Task A",
                    "status": "approved",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 2,
                    "evidence": ["evidence/review-task-1-round-1.json"],
                    "updatedAt": "2026-04-21T00:00:00Z",
                },
                {
                    "id": 2,
                    "title": "Task B",
                    "status": "pending",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 0,
                },
            ],
            "currentTask": 2,
            "lastReviewedBy": "argus",
        })

        result = json.loads(
            workflow_sync_tasks("Sync Test", stream="backend", overwrite=True)
        )

        assert result["success"] is True
        ts = read_json_file(ts_path)
        assert ts["lastReviewedBy"] is None
        assert ts["tasks"][0]["title"] == "Task A"
        assert ts["tasks"][0]["status"] == "pending"
        assert ts["tasks"][0]["reviewRounds"] == 0
        assert "evidence" not in ts["tasks"][0]
        assert "updatedAt" not in ts["tasks"][0]

    def test_overwrite_preserves_review_metadata_by_title_not_position(self, projects_root):
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "- [ ] New Task\n- [ ] Task A\n- [x] Task B\n"
        )
        ts_path = root / "workstreams" / "backend" / "task-state.json"
        write_json_file(ts_path, {
            "tasks": [
                {
                    "id": 1,
                    "title": "Task A",
                    "status": "approved",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 1,
                    "evidence": ["evidence/task-a-review.json"],
                },
                {
                    "id": 2,
                    "title": "Task B",
                    "status": "changes_requested",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 2,
                    "evidence": ["evidence/task-b-review.json"],
                },
            ],
            "currentTask": 2,
            "lastReviewedBy": "argus",
        })

        result = json.loads(
            workflow_sync_tasks("Sync Test", stream="backend", overwrite=True)
        )

        assert result["success"] is True
        ts = read_json_file(ts_path)
        # Conservative policy: overwrite drops review metadata carryover so
        # title reuse across different task instances cannot silently inherit
        # stale reviewRounds / evidence / lastReviewedBy. Durable review
        # artifacts remain on disk in review-report.md / evidence/*.json.
        assert ts["lastReviewedBy"] is None
        for task in ts["tasks"]:
            assert task["reviewRounds"] == 0
            assert "evidence" not in task

    def test_overwrite_does_not_carry_review_history_across_title_reuse(self, projects_root):
        """Removing a reviewed task and reusing its title for a new task must
        NOT grant the new task the old review history."""
        root = self._setup_stream(projects_root)
        ts_path = root / "workstreams" / "backend" / "task-state.json"

        # Prior state: one approved task titled 'Fix lint' with review history.
        write_json_file(ts_path, {
            "tasks": [
                {
                    "id": 1,
                    "title": "Fix lint",
                    "status": "approved",
                    "owner": "atlas",
                    "reviewer": "argus",
                    "reviewRounds": 3,
                    "evidence": ["evidence/fix-lint-r1.json"],
                    "updatedAt": "2026-04-20T00:00:00Z",
                },
            ],
            "currentTask": None,
            "lastReviewedBy": "argus",
        })

        # New tasks.md: the old 'Fix lint' task is gone; a new task also
        # happens to be titled 'Fix lint'. This is a different work item.
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "- [ ] Add feature\n- [ ] Fix lint\n"
        )

        result = json.loads(
            workflow_sync_tasks("Sync Test", stream="backend", overwrite=True)
        )
        assert result["success"] is True

        ts = read_json_file(ts_path)
        assert ts["lastReviewedBy"] is None
        new_fix_lint = next(t for t in ts["tasks"] if t["title"] == "Fix lint")
        assert new_fix_lint["status"] == "pending"
        assert new_fix_lint["reviewRounds"] == 0
        assert "evidence" not in new_fix_lint


    def test_rejects_tasks_md_without_checkbox_tasks(self, projects_root):
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "# Tasks\n\nNo tasks defined yet.\n"
        )

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert "error" in result
        assert "checkbox tasks" in result["error"].lower()

    def test_report_stream_allows_missing_reviewer(self, projects_root):
        root = self._setup_stream(projects_root, completion_mode="report", reviewer=None)
        (root / "workstreams" / "backend" / "tasks.md").write_text("- [ ] Capture QA results\n")

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert result["success"] is True

        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["reviewer"] is None

    def test_rejects_duplicate_checkbox_titles(self, projects_root):
        root = self._setup_stream(projects_root)
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            "- [ ] Task A\n- [x] Task A\n"
        )

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert "error" in result
        assert "duplicate checkbox task title" in result["error"].lower()

    def test_rejects_overlong_checkbox_titles(self, projects_root):
        root = self._setup_stream(projects_root)
        long_title = "x" * 501
        (root / "workstreams" / "backend" / "tasks.md").write_text(
            f"- [ ] {long_title}\n"
        )

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert "error" in result
        assert "exceeds" in result["error"].lower()

    def test_rejects_malformed_parsed_checkbox_tasks(self, projects_root, monkeypatch):
        self._setup_stream(projects_root)
        monkeypatch.setattr(
            workflow_tools_module,
            "_parse_checkbox_tasks_markdown",
            lambda _content: [{"title": "Task A", "status": "pending"}, "bad-task"],
        )

        result = json.loads(workflow_sync_tasks("Sync Test", stream="backend"))
        assert "error" in result
        assert "parsed task 2" in result["error"].lower()


# =========================================================================
# workflow_review_task
# =========================================================================

class TestWorkflowReviewTask:
    def _setup_with_task(self, projects_root, completion_mode="code",
                         initial_status="in_review", owner="atlas", reviewer="argus"):
        root, _ = _bootstrap_approved_project(projects_root, "Review Test")
        workflow_decompose(
            "Review Test",
            streams=[{
                "name": "backend", "owner": owner, "reviewer": reviewer,
                "completionMode": completion_mode,
                "acceptanceCriteria": ["Auth flow works."],
            }],
            plan_slug="the-plan",
        )
        write_json_file(
            root / "workstreams" / "backend" / "task-state.json",
            {
                "tasks": [
                    {
                        "id": 1,
                        "title": "Task 1",
                        "status": initial_status,
                        "owner": owner,
                        "reviewer": reviewer,
                        "reviewRounds": 0,
                    },
                ],
                "currentTask": 1,
                "lastReviewedBy": None,
            },
        )
        return root

    def test_approved_review_writes_markdown_and_json_artifacts(self, projects_root):
        root = self._setup_with_task(projects_root)
        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="approved",
            reviewer="argus",
            summary="Reviewed auth flow and tests; ready to land.",
            behavior_coverage=["Auth flow works."],
            evidence=["evidence/test.log"],
        ))
        assert result["success"] is True
        assert result["review_round"] == 1

        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        task = ts["tasks"][0]
        assert task["status"] == "approved"
        assert task["reviewRounds"] == 1
        assert ts["lastReviewedBy"] == "argus"
        assert "evidence/test.log" in task["evidence"]
        assert any(p.endswith("review-task-1-round-1.json") for p in task["evidence"])

        review_md = (root / "workstreams" / "backend" / "review-report.md").read_text()
        assert "Reviewed auth flow and tests; ready to land." in review_md
        assert "Task 1" in review_md
        assert "approved" in review_md

        review_json = read_json_file(
            root / "workstreams" / "backend" / "evidence" / "review-task-1-round-1.json"
        )
        assert review_json["verdict"] == "approved"
        assert review_json["reviewer"] == "argus"
        assert review_json["behaviorCoverage"] == ["Auth flow works."]
        assert review_json["artifacts"] == ["evidence/test.log"]

    def test_changes_requested_review_records_issues(self, projects_root):
        root = self._setup_with_task(projects_root)
        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="changes_requested",
            reviewer="argus",
            summary="Requesting changes before merge.",
            issues=["Missing regression test for token refresh."],
            missing_evidence=["No failing test reproduction attached."],
        ))
        assert result["success"] is True
        assert result["review_round"] == 1

        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        task = ts["tasks"][0]
        assert task["status"] == "changes_requested"
        assert task["reviewRounds"] == 1
        assert ts["lastReviewedBy"] == "argus"

        review_json = read_json_file(
            root / "workstreams" / "backend" / "evidence" / "review-task-1-round-1.json"
        )
        assert review_json["verdict"] == "changes_requested"
        assert review_json["issues"] == ["Missing regression test for token refresh."]
        assert review_json["missingEvidence"] == ["No failing test reproduction attached."]

    def test_code_review_rejects_wrong_reviewer(self, projects_root):
        root = self._setup_with_task(projects_root)
        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="approved",
            reviewer="apollo",
            summary="rubber stamp",
        ))
        assert "error" in result
        assert "designated reviewer" in result["error"].lower()

        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"
        assert not (root / "workstreams" / "backend" / "review-report.md").exists()

    def test_changes_requested_review_still_requires_designated_reviewer(self, projects_root):
        root = self._setup_with_task(projects_root)
        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="changes_requested",
            reviewer="apollo",
            summary="needs more work",
        ))

        assert "error" in result
        assert "designated reviewer" in result["error"].lower()
        ts = read_json_file(root / "workstreams" / "backend" / "task-state.json")
        assert ts["tasks"][0]["status"] == "in_review"
        assert not (root / "workstreams" / "backend" / "review-report.md").exists()

    def test_review_rejects_missing_manifest_entry(self, projects_root):
        root = self._setup_with_task(projects_root)
        write_json_file(root / "workstreams" / "manifest.json", {})

        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="approved",
            reviewer="argus",
            summary="no manifest",
        ))
        assert "error" in result
        assert "manifest" in result["error"].lower()

    def test_review_rejects_non_string_issue_lists(self, projects_root):
        self._setup_with_task(projects_root)
        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=1,
            verdict="approved",
            reviewer="argus",
            summary="x",
            issues=["ok", 123],
        ))
        assert "error" in result
        assert "issues" in result["error"].lower()

    def test_review_rolls_back_when_batch_write_fails(self, projects_root):
        root = self._setup_with_task(projects_root)
        progress_path = root / "workstreams" / "backend" / "progress.md"
        review_md_path = root / "workstreams" / "backend" / "review-report.md"
        review_json_path = (
            root / "workstreams" / "backend" / "evidence" / "review-task-1-round-1.json"
        )
        state_path = root / "workstreams" / "backend" / "task-state.json"
        before_progress = progress_path.read_text(encoding="utf-8")
        before_state = read_json_file(state_path)

        with patch.object(
            workflow_tools_module,
            "_atomic_write_many_text",
            side_effect=OSError("disk full"),
        ):
            result = json.loads(workflow_review_task(
                "Review Test",
                stream="backend",
                task_id=1,
                verdict="approved",
                reviewer="argus",
                summary="ready",
                evidence=["evidence/test.log"],
            ))

        assert "error" in result
        assert result["error"].startswith("failed to record review")
        assert progress_path.read_text(encoding="utf-8") == before_progress
        assert read_json_file(state_path) == before_state
        assert not review_md_path.exists()
        assert not review_json_path.exists()

    def test_review_sanitizes_task_id_in_artifact_filename(self, projects_root):
        """A malicious task id (path separators, leading dots) must not cause
        the review JSON artifact to land outside the stream's evidence/ dir."""
        root = self._setup_with_task(projects_root)
        stream_dir = root / "workstreams" / "backend"
        ts_path = stream_dir / "task-state.json"

        # Need enough `..` segments to escape `evidence/`.  The prefix
        # `review-task-` absorbs one level so two `..` stays inside evidence;
        # three `..` escapes up to the stream dir.
        malicious_id = "../../../escape"
        state = read_json_file(ts_path)
        state["tasks"][0]["id"] = malicious_id
        state["currentTask"] = malicious_id
        write_json_file(ts_path, state)

        result = json.loads(workflow_review_task(
            "Review Test",
            stream="backend",
            task_id=malicious_id,
            verdict="approved",
            reviewer="argus",
            summary="traversal attempt",
        ))

        evidence_dir = (stream_dir / "evidence").resolve()
        escape_candidates = [
            stream_dir / "escape-round-1.json",
            stream_dir.parent / "escape-round-1.json",
            root / "escape-round-1.json",
        ]
        for cand in escape_candidates:
            assert not cand.exists(), f"review artifact escaped to {cand}"

        # If the tool reported success, the file must be within evidence/.
        if result.get("success"):
            review_json_path = Path(result["review_json_path"]).resolve()
            assert evidence_dir in review_json_path.parents, (
                f"review artifact {review_json_path} not under {evidence_dir}"
            )

    def test_safe_task_id_is_collision_resistant(self):
        """Sanitized task ids used in filenames must disambiguate distinct
        inputs that would otherwise collide after character replacement or
        truncation."""
        from tools.workflow_tools import _safe_task_id_for_filename

        # Pure decimal integer ids keep their original shape (common case).
        assert _safe_task_id_for_filename("1") == "1"
        assert _safe_task_id_for_filename(1) == "1"
        assert _safe_task_id_for_filename("42") == "42"

        # Non-digit ids get a hash suffix to resist case-insensitive FS
        # collisions (APFS/NTFS) and post-sanitization collisions.
        lower = _safe_task_id_for_filename("task-1")
        upper = _safe_task_id_for_filename("TASK-1")
        assert lower != upper, f"case-insensitive collision: {lower!r} == {upper!r}"

        # Distinct tainted ids must not collapse to the same filename.
        a = _safe_task_id_for_filename("a/b")
        b = _safe_task_id_for_filename("a_b")
        assert a != b, f"distinct ids collide: {a!r} == {b!r}"

        # Long ids that differ only past the truncation boundary must differ.
        long1 = "x" * 100 + "_one"
        long2 = "x" * 100 + "_two"
        assert _safe_task_id_for_filename(long1) != _safe_task_id_for_filename(long2)

        # Final value must itself be safe (no path separators).
        for val in ["a/b", "../../escape", "x" * 200, "\x00bad"]:
            safe = _safe_task_id_for_filename(val)
            assert "/" not in safe and "\\" not in safe and "\x00" not in safe
            assert not safe.startswith(".")
