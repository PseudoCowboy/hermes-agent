"""Approval mediator: change requests and escalation behavior."""

from __future__ import annotations

import pytest

from mythos.discord_io import InMemoryDiscordIO
from mythos.mediator import (
    parse_review_verdict,
    parse_user_response,
)
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import GENERAL_CHANNEL
from mythos.state import ProjectPhase


def test_parse_user_response_approve():
    v = parse_user_response("approve")
    assert v is not None and v.approved


def test_parse_user_response_lgtm():
    v = parse_user_response("lgtm, ship it")
    assert v is not None and v.approved


def test_parse_user_response_change_request_keyword():
    v = parse_user_response("Request changes: add multi-user support")
    assert v is not None and not v.approved
    assert "multi-user" in v.feedback


def test_parse_user_response_freeform_treated_as_change_request():
    v = parse_user_response("the offline-only behavior should also handle selections")
    assert v is not None and not v.approved
    assert "offline" in v.feedback


def test_parse_user_response_short_noise_ignored():
    assert parse_user_response("ok") is None


def test_parse_review_verdict_approved():
    text = "## Verdict\nVerdict: APPROVED\n\n## Comments\n- looks ok"
    assert parse_review_verdict(text) == "approved"


def test_parse_review_verdict_changes():
    text = "## Verdict\nVerdict: CHANGES_REQUESTED\n\n## Comments\n- nope"
    assert parse_review_verdict(text) == "changes_requested"


def test_parse_review_verdict_default_is_changes():
    """Malformed reviews must not silently approve."""
    assert parse_review_verdict("this is freeform garbage") == "changes_requested"


@pytest.mark.asyncio
async def test_change_request_round_two(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    """User requests changes once, then approves."""
    await discord.inject_user_message(cfg.main_channel_id, "build a habit tracker app")
    await orchestrator.await_pending()
    rec = orchestrator.pm.list_projects()[0]
    general_id = rec.state.channels[GENERAL_CHANNEL]

    assert rec.state.phase == ProjectPhase.AWAITING_USER

    await discord.inject_user_message(
        general_id, "I'd like the spec to also cover offline mode and gamification"
    )
    await orchestrator.await_pending()
    assert rec.state.approval_round == 1
    # After revision, we're back in AWAITING_USER.
    assert rec.state.phase == ProjectPhase.AWAITING_USER

    await discord.inject_user_message(general_id, "approve")
    await orchestrator.await_pending()
    assert rec.state.phase == ProjectPhase.COMPLETE


@pytest.mark.asyncio
async def test_change_request_escalates_after_max_rounds(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    cfg.max_approval_rounds = 1
    await discord.inject_user_message(cfg.main_channel_id, "build a recipe planner")
    await orchestrator.await_pending()
    rec = orchestrator.pm.list_projects()[0]
    general_id = rec.state.channels[GENERAL_CHANNEL]

    # First change request — moves to round 1.
    await discord.inject_user_message(
        general_id, "I want it to also support meal planning across weeks"
    )
    await orchestrator.await_pending()
    assert rec.state.approval_round == 1

    # Second change request — exceeds max; should escalate.
    await discord.inject_user_message(
        general_id, "and add shopping list integration too please"
    )
    await orchestrator.await_pending()
    assert rec.state.phase == ProjectPhase.ESCALATED
