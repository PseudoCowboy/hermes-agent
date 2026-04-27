"""Tests for the P4 workflow-mutating toolset split.

Per ``plans/discord-orchestration-spec/02-technical-design.md`` §4 the
single ``workflow-mutating`` bucket is split into orchestrator-only and
stream-only halves so that future per-session toolset enforcement
(P5/P6) has clean buckets to attach to.

This test pins:

* The new per-tool toolset assignments on the live registry, so a
  refactor accidentally moving a tool back into ``workflow-mutating``
  is caught.
* The back-compat composite ``workflow-mutating`` still resolves to
  the union of both halves (config that names it must keep working).
* Composites that transitively include ``workflow-mutating``
  (``workflow``, ``hermes-discord``) still see all eight workflow
  mutators — i.e. the split is invisible to existing callers until
  P5/P6 wires per-session toolsets.
* The new ``discord-orchestration`` composite resolves to all six
  P3+P4 admin/stream tools.
"""

from __future__ import annotations

# Side-effect imports populate the registry; intentional.
from tools import discord_orchestration_tools  # noqa: F401
from tools import workflow_tools  # noqa: F401
from tools.registry import registry

from toolsets import resolve_toolset


_ORCHESTRATOR_MUTATORS = {
    "workflow_create_project",
    "workflow_save_plan",
    "workflow_approve_plan",
    "workflow_decompose",
    "workflow_handoff",
    "workflow_sync_tasks",
}

_STREAM_MUTATORS = {
    "workflow_checkpoint",
    "workflow_review_task",
}

_ALL_WORKFLOW_MUTATORS = _ORCHESTRATOR_MUTATORS | _STREAM_MUTATORS

_DISCORD_ADMIN = {
    "discord_create_project_category",
    "discord_create_stream_channel",
    "discord_archive_project_category",
}

_DISCORD_STREAM = {
    "discord_post_message",
    "discord_react_to_message",
    "discord_wait_for_reaction",
}


# ---------------------------------------------------------------------------
# Per-tool registry assignments
# ---------------------------------------------------------------------------


class TestPerToolToolsetAssignment:
    def test_orchestrator_mutators_in_orchestrator_bucket(self):
        for name in _ORCHESTRATOR_MUTATORS:
            assert (
                registry.get_toolset_for_tool(name)
                == "workflow-orchestrator-mutating"
            ), f"{name} should be in workflow-orchestrator-mutating"

    def test_stream_mutators_in_stream_bucket(self):
        for name in _STREAM_MUTATORS:
            assert (
                registry.get_toolset_for_tool(name)
                == "workflow-stream-mutating"
            ), f"{name} should be in workflow-stream-mutating"

    def test_workflow_status_stays_readonly(self):
        assert registry.get_toolset_for_tool("workflow_status") == "workflow-readonly"

    def test_no_workflow_tool_left_in_old_bucket(self):
        """Catch refactors that re-attach a tool to the deprecated bucket."""
        offenders = [
            n for n, ts in registry.get_tool_to_toolset_map().items()
            if ts == "workflow-mutating"
        ]
        assert offenders == [], (
            f"workflow-mutating is now a back-compat composite — no tool "
            f"should declare it directly: {offenders}"
        )

    def test_discord_admin_tools_in_admin_bucket(self):
        for name in _DISCORD_ADMIN:
            assert (
                registry.get_toolset_for_tool(name)
                == "discord-orchestration-admin"
            ), f"{name} should be in discord-orchestration-admin"

    def test_discord_stream_tools_in_stream_bucket(self):
        for name in _DISCORD_STREAM:
            assert (
                registry.get_toolset_for_tool(name)
                == "discord-orchestration-stream"
            ), f"{name} should be in discord-orchestration-stream"


# ---------------------------------------------------------------------------
# Back-compat composite resolution
# ---------------------------------------------------------------------------


class TestBackCompatResolution:
    def test_workflow_mutating_still_resolves_to_union(self):
        """Existing config naming ``workflow-mutating`` must keep working."""
        resolved = resolve_toolset("workflow-mutating")
        assert _ALL_WORKFLOW_MUTATORS.issubset(resolved), (
            f"workflow-mutating composite missing tools: "
            f"{_ALL_WORKFLOW_MUTATORS - resolved}"
        )

    def test_workflow_composite_still_resolves(self):
        """The ``workflow`` composite (status + mutators) is unchanged."""
        resolved = resolve_toolset("workflow")
        assert "workflow_status" in resolved
        assert _ALL_WORKFLOW_MUTATORS.issubset(resolved)

    def test_hermes_discord_unchanged(self):
        """The Discord agent's manifest still sees all 8 workflow mutators
        plus ``workflow_status``; the split is invisible to it until
        P5/P6 wires per-session toolsets."""
        resolved = resolve_toolset("hermes-discord")
        assert _ALL_WORKFLOW_MUTATORS.issubset(resolved)
        assert "workflow_status" in resolved


# ---------------------------------------------------------------------------
# New discord-orchestration composite
# ---------------------------------------------------------------------------


class TestDiscordOrchestrationComposite:
    def test_admin_composite_resolves(self):
        resolved = resolve_toolset("discord-orchestration-admin")
        assert _DISCORD_ADMIN.issubset(resolved)

    def test_stream_composite_resolves(self):
        resolved = resolve_toolset("discord-orchestration-stream")
        assert _DISCORD_STREAM.issubset(resolved)

    def test_full_discord_orchestration_composite(self):
        resolved = resolve_toolset("discord-orchestration")
        assert (_DISCORD_ADMIN | _DISCORD_STREAM).issubset(resolved)
