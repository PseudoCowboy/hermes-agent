"""Project isolation + cross-project guard tests.

Spec ref: openspec/changes/.../specs/project-isolation/spec.md
"""

from __future__ import annotations

import pytest

from mythos.config import (
    ROLE_APOLLO,
    ROLE_ARGUS,
    ROLE_ATHENA,
    ROLE_ATLAS,
    ROLE_HEPHAESTUS,
    ROLE_PROMETHEUS,
)
from mythos.state import (
    CHANNEL_TYPE_FRONTEND,
    CHANNEL_TYPE_PROJECT,
    CrossProjectError,
    ProjectStatus,
)


pytestmark = pytest.mark.asyncio


def _design_reply() -> str:
    return (
        "<<MYTHOS:DESIGN_BEGIN>>\n# design\n<<MYTHOS:DESIGN_END>>\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )


def _approve_review() -> str:
    return "ok\n<<MYTHOS:DECISION:approve>>\n<<MYTHOS:STATUS:finished>>\n"


def _summary() -> str:
    return "Approve please.\n<<MYTHOS:STATUS:finished>>\n"


def _impl() -> str:
    return "<<MYTHOS:STATUS:received>>\ndone\n<<MYTHOS:STATUS:finished>>\n"


async def test_two_concurrent_projects_get_distinct_channels(
    orchestrator, fake_discord, scripted_runner
):
    # Queue replies for two full project flows.
    for _ in range(2):
        scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
        scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
        scripted_runner.queue(ROLE_ARGUS, _approve_review())
        scripted_runner.queue(ROLE_ATHENA, _summary())
        scripted_runner.queue(ROLE_APOLLO, _impl())
        scripted_runner.queue(ROLE_ATLAS, _impl())
        scripted_runner.queue(ROLE_HEPHAESTUS, _impl())

    await fake_discord.user_says("Build a translator extension", author_id=1)
    await fake_discord.user_says("Build a markdown editor", author_id=2)

    projects = orchestrator.state.all_projects()
    assert len(projects) == 2
    a, b = projects

    # Distinct project channels.
    assert a.project_channel_id != b.project_channel_id

    # Approve both — channels must remain distinct.
    await fake_discord.user_says("approve", channel_id=a.project_channel_id, author_id=1)
    await fake_discord.user_says("approve", channel_id=b.project_channel_id, author_id=2)

    a = orchestrator.state.get(a.id)
    b = orchestrator.state.get(b.id)
    a_chans = {a.project_channel_id} | {b.channel_id for b in a.channels.values()}
    b_chans = {b.project_channel_id} | {bb.channel_id for bb in b.channels.values()}
    assert a_chans.isdisjoint(b_chans), "channels must not overlap across projects"

    # Workspaces are distinct.
    assert a.workspace_path != b.workspace_path


async def test_intake_is_idempotent(orchestrator, fake_discord, scripted_runner):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())

    msg_id = await fake_discord.user_says("Translator request", author_id=7)

    # Replay the same source message id by directly invoking the handler.
    from mythos.discord_client import IncomingMessage

    await orchestrator.on_message(
        IncomingMessage(
            message_id=msg_id,
            channel_id=fake_discord.main_channel_id,
            author_id=7,
            author_name="user",
            content="Translator request",
        )
    )
    assert len(orchestrator.state.all_projects()) == 1


async def test_cross_project_routing_is_blocked(orchestrator, fake_discord, scripted_runner):
    # Set up two projects so we have two project channels.
    for _ in range(2):
        scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
        scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
        scripted_runner.queue(ROLE_ARGUS, _approve_review())
        scripted_runner.queue(ROLE_ATHENA, _summary())

    await fake_discord.user_says("Project A", author_id=1)
    await fake_discord.user_says("Project B", author_id=2)
    a, b = orchestrator.state.all_projects()

    # Direct attempt to assert another project's channel belongs to this one.
    with pytest.raises(CrossProjectError):
        orchestrator.state.assert_channel_in_project(a.id, b.project_channel_id)

    # Cross-project routing guard.
    with pytest.raises(CrossProjectError):
        orchestrator.state.ensure_no_cross_project_routing(
            a.id, [b.project_channel_id]
        )

    # Audit trail recorded the rejected post.
    a_after = orchestrator.state.get(a.id)
    blocked = [e for e in a_after.audit_log if e["event"] == "cross_project_blocked"]
    assert blocked, "expected cross_project_blocked audit event"


async def test_state_is_persisted_per_project(
    orchestrator, fake_discord, scripted_runner, tmp_config
):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())
    await fake_discord.user_says("Translator request", author_id=1)
    project = orchestrator.state.all_projects()[0]
    # State file written.
    assert (tmp_config.state_dir / f"{project.id}.json").exists()


async def test_implementation_does_not_run_without_approval(
    orchestrator, fake_discord, scripted_runner
):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())

    await fake_discord.user_says("Build something")
    project = orchestrator.state.all_projects()[0]
    assert project.status == ProjectStatus.AWAITING_APPROVAL
    assert CHANNEL_TYPE_FRONTEND not in project.channels
    # Apollo/Atlas/Hephaestus queues are untouched (we never queued them).
    assert scripted_runner.queued_count(ROLE_APOLLO) == 0  # never queued, never called
    assert scripted_runner.queued_count(ROLE_ATLAS) == 0
    assert scripted_runner.queued_count(ROLE_HEPHAESTUS) == 0


async def test_user_rejection_loops_back_to_planning(
    orchestrator, fake_discord, scripted_runner
):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())

    await fake_discord.user_says("Build a thing")
    project = orchestrator.state.all_projects()[0]
    await fake_discord.user_says("reject", channel_id=project.project_channel_id, author_id=5)
    project = orchestrator.state.get(project.id)
    assert project.status == ProjectStatus.PLANNING
    assert project.latest_approval().decision == "rejected"
