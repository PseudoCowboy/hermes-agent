"""Approval/review workflow tests.

Spec ref: openspec/changes/.../specs/approval-and-review-workflow/spec.md
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
from mythos.state import ProjectStatus


pytestmark = pytest.mark.asyncio


def _design_v(label: str) -> str:
    return (
        f"<<MYTHOS:DESIGN_BEGIN>>\n# design {label}\n<<MYTHOS:DESIGN_END>>\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )


def _changes_review() -> str:
    return (
        "- needs more on tests\n- backend endpoints unclear\n"
        "<<MYTHOS:DECISION:request_changes>>\n<<MYTHOS:STATUS:finished>>\n"
    )


def _approve_review() -> str:
    return "<<MYTHOS:DECISION:approve>>\n<<MYTHOS:STATUS:finished>>\n"


def _summary() -> str:
    return "Type approve.\n<<MYTHOS:STATUS:finished>>\n"


async def test_review_revision_loop(orchestrator, fake_discord, scripted_runner):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_v("v1"))
    scripted_runner.queue(ROLE_ARGUS, _changes_review())
    scripted_runner.queue(ROLE_ATHENA, "Sending back for revision.\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_v("v2"))
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())

    await fake_discord.user_says("Build me a thing")
    project = orchestrator.state.all_projects()[0]
    project = orchestrator.state.get(project.id)

    # Two design versions, two reviews.
    assert len(project.design_versions) == 2
    assert len(project.reviews) == 2
    assert project.reviews[0].decision == "request_changes"
    assert project.reviews[1].decision == "approve"
    assert project.status == ProjectStatus.AWAITING_APPROVAL


async def test_approval_records_user_design_and_decision(
    orchestrator, fake_discord, scripted_runner
):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_v("v1"))
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())
    scripted_runner.queue(ROLE_APOLLO, "<<MYTHOS:STATUS:received>>\nfe done\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_ATLAS, "<<MYTHOS:STATUS:received>>\nbe done\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_HEPHAESTUS, "<<MYTHOS:STATUS:received>>\ntests pass\n<<MYTHOS:STATUS:finished>>")

    await fake_discord.user_says("Translator")
    project = orchestrator.state.all_projects()[0]
    await fake_discord.user_says(
        "approve", channel_id=project.project_channel_id, author_id=99
    )
    project = orchestrator.state.get(project.id)

    appr = project.latest_approval()
    assert appr.user_id == 99
    assert appr.decision == "approved"
    assert appr.design_version == project.latest_design().version
    assert project.status == ProjectStatus.DONE


async def test_unknown_text_in_project_channel_does_not_approve(
    orchestrator, fake_discord, scripted_runner
):
    scripted_runner.queue(ROLE_ATHENA, "ok\n<<MYTHOS:STATUS:finished>>")
    scripted_runner.queue(ROLE_PROMETHEUS, _design_v("v1"))
    scripted_runner.queue(ROLE_ARGUS, _approve_review())
    scripted_runner.queue(ROLE_ATHENA, _summary())

    await fake_discord.user_says("Translator")
    project = orchestrator.state.all_projects()[0]
    # Random user message must not flip approval.
    await fake_discord.user_says(
        "I'm thinking about it", channel_id=project.project_channel_id, author_id=2
    )
    project = orchestrator.state.get(project.id)
    assert project.status == ProjectStatus.AWAITING_APPROVAL
    assert project.latest_approval() is None
