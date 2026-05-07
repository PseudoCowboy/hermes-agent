"""End-to-end happy path: a user posts a request and the orchestrator runs
the full Athena → Prometheus → Argus → approval → Apollo/Atlas/Hephaestus
sequence using a fake Discord client and scripted CLI subprocess outputs.
"""
from __future__ import annotations

import asyncio

import pytest

from mythos.config import default_claude_cli, default_codex_cli, default_gemini_cli
from mythos.roles import ChannelKind, ProjectStatus, Role, ROLE_TO_CLI

from .conftest import script_happy_path


pytestmark = pytest.mark.asyncio


USER_REQUEST = (
    "I want to build a Chrome extension, a translator extension, when I "
    "double click on a word it will look it up in a built-in dictionary "
    "and show the translation."
)


@pytest.mark.asyncio
async def test_full_happy_path(orchestrator, fake_discord, fake_executor,
                                mythos_config, tmp_workspace_root):
    script_happy_path(fake_executor)

    # 1) User posts in main channel
    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=7777,
        content=USER_REQUEST,
    )

    # Orchestrator should now have:
    # - A category and a plan channel for this project
    # - Posted Athena ack in main + project channel
    # - Posted Prometheus clarifying questions in plan channel
    projects = orchestrator.store.list()
    assert len(projects) == 1, f"expected 1 project, got {len(projects)}"
    project = projects[0]
    assert project.status == ProjectStatus.CLARIFYING_QUESTIONS

    # Category was created
    assert project.category_id is not None
    assert fake_discord.categories[project.category_id].startswith(
        f"project-{project.short_id}-"
    )

    # Plan channel exists, frontend/backend/test do NOT yet
    assert ChannelKind.PLAN.value in project.channels
    assert ChannelKind.FRONTEND.value not in project.channels
    assert ChannelKind.BACKEND.value not in project.channels
    assert ChannelKind.TEST.value not in project.channels

    plan_channel_id = project.channels[ChannelKind.PLAN.value].discord_channel_id
    plan_msgs = fake_discord.messages_in(plan_channel_id)
    assert any("Athena" in m.content for m in plan_msgs)
    assert any("Prometheus" in m.content and "QUESTION" in m.content
               for m in plan_msgs)

    # 2) User answers in the plan channel → triggers spec drafting
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id,
        author_id=7777,
        content="English<->Chinese, offline only, simple popup is fine.",
    )

    # Now we should be awaiting approval, with a spec + review on file.
    assert project.status == ProjectStatus.AWAITING_USER_APPROVAL
    assert len(project.design_specs) == 1
    assert project.design_specs[0].version == 1
    assert len(project.review_comments) == 1

    plan_msgs = fake_discord.messages_in(plan_channel_id)
    assert any("design spec" in m.content.lower() for m in plan_msgs)
    assert any("Argus" in m.content for m in plan_msgs)
    assert any("approve" in m.content.lower() for m in plan_msgs)

    # 3) User approves
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id,
        author_id=7777,
        content="approve",
    )

    # Implementation should now be in progress, with all 3 channels created.
    assert project.status == ProjectStatus.IMPLEMENTATION_IN_PROGRESS
    assert ChannelKind.FRONTEND.value in project.channels
    assert ChannelKind.BACKEND.value in project.channels
    assert ChannelKind.TEST.value in project.channels

    fe = project.channels[ChannelKind.FRONTEND.value].discord_channel_id
    be = project.channels[ChannelKind.BACKEND.value].discord_channel_id
    te = project.channels[ChannelKind.TEST.value].discord_channel_id

    # Apollo should only post in frontend, Atlas in backend, Hephaestus in test.
    assert any("Apollo" in m.content for m in fake_discord.messages_in(fe))
    assert any("Atlas" in m.content for m in fake_discord.messages_in(be))
    assert any("Hephaestus" in m.content for m in fake_discord.messages_in(te))

    # And specialists must NOT have posted in each other's channels
    for m in fake_discord.messages_in(fe):
        assert "Atlas" not in m.content and "Hephaestus" not in m.content
    for m in fake_discord.messages_in(be):
        assert "Apollo" not in m.content and "Hephaestus" not in m.content
    for m in fake_discord.messages_in(te):
        assert "Apollo" not in m.content and "Atlas" not in m.content

    # Approval is recorded against the right spec version
    assert project.is_approved()
    assert project.approvals[-1].spec_version == project.design_specs[-1].version


@pytest.mark.asyncio
async def test_revision_round(orchestrator, fake_discord, fake_executor,
                              mythos_config):
    """User can request revisions and a second spec is drafted."""
    script_happy_path(fake_executor, include_revision=True)

    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=7777,
        content=USER_REQUEST,
    )
    project = orchestrator.store.list()[0]
    plan_channel_id = project.channels[ChannelKind.PLAN.value].discord_channel_id

    # Answer questions → first spec drafted
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id,
        author_id=7777,
        content="answers go here",
    )
    assert len(project.design_specs) == 1
    assert project.status == ProjectStatus.AWAITING_USER_APPROVAL

    # Ask for revision → second spec drafted, awaiting approval again
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id,
        author_id=7777,
        content="revise: please add a permissions section",
    )
    assert len(project.design_specs) == 2
    assert project.design_specs[1].version == 2
    assert project.status == ProjectStatus.AWAITING_USER_APPROVAL


@pytest.mark.asyncio
async def test_isolation_between_concurrent_projects(orchestrator, fake_discord,
                                                     fake_executor, mythos_config):
    """Two projects spun up simultaneously should not see each other's channels."""
    # Each project re-uses the same canned-response sequence; push two copies.
    script_happy_path(fake_executor)
    script_happy_path(fake_executor)

    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=1, content=USER_REQUEST,
    )
    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=2, content="I want to build a different project: a calendar app.",
    )

    projects = orchestrator.store.list()
    assert len(projects) == 2
    p1, p2 = projects[0], projects[1]
    assert p1.short_id != p2.short_id
    assert p1.workspace_path != p2.workspace_path
    assert p1.category_id != p2.category_id

    p1_plan = p1.channels[ChannelKind.PLAN.value].discord_channel_id
    p2_plan = p2.channels[ChannelKind.PLAN.value].discord_channel_id
    assert p1_plan != p2_plan

    # Each project's plan channel only has messages addressed to it.
    p1_msgs = fake_discord.messages_in(p1_plan)
    p2_msgs = fake_discord.messages_in(p2_plan)
    assert p1_msgs and p2_msgs
    # Ensure neither leaks the other's short_id.
    for m in p1_msgs:
        assert p2.short_id not in m.content
    for m in p2_msgs:
        assert p1.short_id not in m.content


@pytest.mark.asyncio
async def test_state_persists_to_disk(orchestrator, fake_discord, fake_executor,
                                       mythos_config, tmp_state_path):
    """Project records survive store re-load (NFR1)."""
    script_happy_path(fake_executor)
    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=99, content=USER_REQUEST,
    )
    proj_id = orchestrator.store.list()[0].project_id

    # Re-create the store from disk
    from mythos.state import ProjectStore
    fresh = ProjectStore(tmp_state_path)
    reloaded = fresh.get(proj_id)
    assert reloaded is not None
    assert reloaded.original_request == USER_REQUEST
    assert reloaded.short_id == orchestrator.store.list()[0].short_id


@pytest.mark.asyncio
async def test_cli_invocation_uses_required_flags(orchestrator, fake_discord,
                                                   fake_executor, mythos_config):
    """Each role must invoke the CLI with the spec-mandated flags + env."""
    script_happy_path(fake_executor)
    await fake_discord.simulate_user_message(
        channel_id=mythos_config.main_channel_id,
        author_id=99, content=USER_REQUEST,
    )
    plan_channel_id = orchestrator.store.list()[0].channels[ChannelKind.PLAN.value].discord_channel_id
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id, author_id=99,
        content="answers",
    )
    await fake_discord.simulate_user_message(
        channel_id=plan_channel_id, author_id=99,
        content="approve",
    )

    # Ensure each CLI was actually invoked
    clis = [c["cli"] for c in fake_executor.calls]
    assert "claude" in clis    # athena/prometheus/atlas
    assert "codex" in clis     # argus/hephaestus
    assert "gemini" in clis    # apollo

    # Validate flags for each CLI flavor
    for call in fake_executor.calls:
        argv = call["argv"]
        env = call["env"]
        if call["cli"] == "claude":
            assert "--dangerously-skip-permissions" in argv
            assert "--effort" in argv and "high" in argv
            assert "--print" in argv
            assert env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:4141"
            assert env.get("ANTHROPIC_AUTH_TOKEN") == "dummy"
            assert env.get("ANTHROPIC_MODEL") == "claude-opus-4.7-1m-internal"
            # Claude takes the prompt via stdin
            assert call["stdin_text"] is not None
        elif call["cli"] == "codex":
            assert "exec" in argv
            assert "-m" in argv and "gpt-5.5" in argv
            assert "--skip-git-repo-check" in argv
            assert "model_reasoning_effort=high" in " ".join(argv)
            assert env.get("OPENAI_BASE_URL") == "http://127.0.0.1:4141/v1"
            assert env.get("OPENAI_API_KEY") == "dummy"
            assert call["stdin_text"] is not None
        elif call["cli"] == "gemini":
            assert "-m" in argv and "gemini-3.1-pro-preview" in argv
            # Gemini must use -p <prompt> per spec
            assert "-p" in argv
            assert "-y" in argv
            # Prompt is in argv, not stdin
            assert call["stdin_text"] is None
