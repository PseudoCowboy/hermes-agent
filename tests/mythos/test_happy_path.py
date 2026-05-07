"""End-to-end happy-path test for the Mythos orchestrator.

Drives the user-scenario.md flow with a ``FakeDiscordClient`` and a
``ScriptedRunner``. Verifies that:
  - The main channel intake creates exactly one project channel.
  - Prometheus drafts a spec; Argus reviews; Athena asks for approval.
  - Implementation does NOT start until the user types ``approve``.
  - After approval the system creates frontend, backend, and test channels
    and runs the right agent in each.
"""

from __future__ import annotations

import pytest

from mythos.config import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    ROLE_APOLLO,
    ROLE_ARGUS,
    ROLE_ATHENA,
    ROLE_ATLAS,
    ROLE_HEPHAESTUS,
    ROLE_PROMETHEUS,
    ROLE_PROVIDERS,
)
from mythos.state import (
    CHANNEL_TYPE_BACKEND,
    CHANNEL_TYPE_FRONTEND,
    CHANNEL_TYPE_PROJECT,
    CHANNEL_TYPE_TEST,
    ProjectStatus,
)


pytestmark = pytest.mark.asyncio


def _design_reply(slug: str = "translator") -> str:
    return (
        "Drafting now.\n"
        "<<MYTHOS:DESIGN_BEGIN>>\n"
        f"# {slug} extension design\n\n"
        "## Frontend\n- double-click handler\n- popup with translation\n"
        "## Backend\n- /lookup endpoint over a built-in dictionary\n"
        "## Tests\n- unit tests for /lookup\n- selenium for popup\n"
        "<<MYTHOS:DESIGN_END>>\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )


def _approve_review() -> str:
    return (
        "- Scope is reasonable.\n"
        "- Design is testable.\n"
        "<<MYTHOS:DECISION:approve>>\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )


def _athena_summary(text: str = "Reviewer was happy. Type `approve` to start.") -> str:
    return f"{text}\n<<MYTHOS:STATUS:finished>>\n"


def _impl_finished(role: str) -> str:
    return (
        "<<MYTHOS:STATUS:received>>\n"
        f"Implemented the {role} side.\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )


async def test_happy_path_translator_extension(
    orchestrator, fake_discord, scripted_runner
):
    # Queue the agent replies for the full flow.
    scripted_runner.queue(ROLE_ATHENA, "Acknowledged. Slug looks good.\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply("translator"))
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _athena_summary())
    scripted_runner.queue(ROLE_APOLLO, _impl_finished("frontend"))
    scripted_runner.queue(ROLE_ATLAS, _impl_finished("backend"))
    scripted_runner.queue(ROLE_HEPHAESTUS, _impl_finished("test"))

    # User posts the request in the main channel.
    request = (
        "I want to build a Chrome extension, a translator extension. When I "
        "double-click on a website, it should look up the word in a built-in "
        "dictionary and show me the translation."
    )
    await fake_discord.user_says(request, channel_id=fake_discord.main_channel_id)

    # Exactly one project should exist.
    projects = orchestrator.state.all_projects()
    assert len(projects) == 1, "expected exactly one project"
    project = projects[0]

    # Project status should be AWAITING_APPROVAL — implementation must NOT run.
    assert project.status == ProjectStatus.AWAITING_APPROVAL
    assert project.channels.get(CHANNEL_TYPE_PROJECT) is not None
    assert project.channels.get(CHANNEL_TYPE_FRONTEND) is None, (
        "decomposition must not run before approval"
    )

    # Apollo / Atlas / Hephaestus must NOT have been called yet.
    assert scripted_runner.queued_count(ROLE_APOLLO) == 1
    assert scripted_runner.queued_count(ROLE_ATLAS) == 1
    assert scripted_runner.queued_count(ROLE_HEPHAESTUS) == 1

    # The project channel should contain Athena's ack, Prometheus's draft,
    # Argus's review, and Athena's call for approval.
    proj_msgs = [c for _, c in fake_discord.messages_in(project.project_channel_id)]
    assert any("Athena" in m for m in proj_msgs)
    assert any("Prometheus" in m for m in proj_msgs)
    assert any("Argus" in m for m in proj_msgs)
    assert any("approve" in m.lower() for m in proj_msgs)

    # User approves in the project channel.
    await fake_discord.user_says(
        "approve", channel_id=project.project_channel_id, author_id=42
    )

    # Now frontend, backend, test channels exist with the right agents in them.
    project = orchestrator.state.get(project.id)
    assert project.status == ProjectStatus.DONE
    fe = project.channels[CHANNEL_TYPE_FRONTEND]
    be = project.channels[CHANNEL_TYPE_BACKEND]
    te = project.channels[CHANNEL_TYPE_TEST]
    assert fake_discord.channel_kind(fe.channel_id) == CHANNEL_TYPE_FRONTEND
    assert fake_discord.channel_kind(be.channel_id) == CHANNEL_TYPE_BACKEND
    assert fake_discord.channel_kind(te.channel_id) == CHANNEL_TYPE_TEST

    # All three implementation agents posted in their own channels.
    fe_msgs = [c for _, c in fake_discord.messages_in(fe.channel_id)]
    be_msgs = [c for _, c in fake_discord.messages_in(be.channel_id)]
    te_msgs = [c for _, c in fake_discord.messages_in(te.channel_id)]
    assert any("Apollo" in m for m in fe_msgs), fe_msgs
    assert any("Atlas" in m for m in be_msgs), be_msgs
    assert any("Hephaestus" in m for m in te_msgs), te_msgs

    # Approval was recorded with the right design version + user.
    appr = project.latest_approval()
    assert appr is not None and appr.decision == "approved"
    assert appr.user_id == 42
    assert appr.design_version == project.latest_design().version


async def test_provider_routing_is_fixed(
    orchestrator, fake_discord, scripted_runner
):
    # Confirm the role->provider map matches the spec: the orchestrator should
    # never request a different provider for a role.
    assert ROLE_PROVIDERS[ROLE_ATHENA] == PROVIDER_CLAUDE
    assert ROLE_PROVIDERS[ROLE_PROMETHEUS] == PROVIDER_CLAUDE
    assert ROLE_PROVIDERS[ROLE_ATLAS] == PROVIDER_CLAUDE
    assert ROLE_PROVIDERS[ROLE_ARGUS] == PROVIDER_CODEX
    assert ROLE_PROVIDERS[ROLE_HEPHAESTUS] == PROVIDER_CODEX
    assert ROLE_PROVIDERS[ROLE_APOLLO] == PROVIDER_GEMINI

    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_reply())
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _athena_summary())
    scripted_runner.queue(ROLE_APOLLO, _impl_finished("fe"))
    scripted_runner.queue(ROLE_ATLAS, _impl_finished("be"))
    scripted_runner.queue(ROLE_HEPHAESTUS, _impl_finished("te"))

    await fake_discord.user_says("test request please")
    project = orchestrator.state.all_projects()[0]
    await fake_discord.user_says("approve", channel_id=project.project_channel_id)

    # Each task records the provider it ran under.
    project = orchestrator.state.get(project.id)
    by_role = {t.role: t for t in project.tasks.values()}
    assert by_role[ROLE_APOLLO].provider == PROVIDER_GEMINI
    assert by_role[ROLE_ATLAS].provider == PROVIDER_CLAUDE
    assert by_role[ROLE_HEPHAESTUS].provider == PROVIDER_CODEX
