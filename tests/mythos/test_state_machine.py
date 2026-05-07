"""Unit tests for the project state machine."""

from __future__ import annotations

import pytest

from mythos.models import Project, ProjectState
from mythos.state_machine import IllegalTransitionError, transition


def _new_project() -> Project:
    return Project(
        display_name="Demo",
        requester="alice",
        request_text="build a thing",
        source_message_id="msg_1",
    )


def test_happy_path_transitions():
    p = _new_project()
    for state in [
        ProjectState.DRAFTING,
        ProjectState.REVIEWING,
        ProjectState.AWAITING_APPROVAL,
        ProjectState.DECOMPOSING,
        ProjectState.IMPLEMENTING,
        ProjectState.COMPLETED,
        ProjectState.ARCHIVED,
    ]:
        transition(p, state)
    assert p.state is ProjectState.ARCHIVED
    assert p.archived_at is not None


def test_revision_loop_allowed():
    p = _new_project()
    transition(p, ProjectState.DRAFTING)
    transition(p, ProjectState.REVIEWING)
    transition(p, ProjectState.AWAITING_APPROVAL)
    # User requests changes -> back to drafting -> review again -> approval.
    transition(p, ProjectState.DRAFTING)
    transition(p, ProjectState.REVIEWING)
    transition(p, ProjectState.AWAITING_APPROVAL)
    transition(p, ProjectState.DECOMPOSING)


def test_illegal_jumps_rejected():
    p = _new_project()
    with pytest.raises(IllegalTransitionError):
        transition(p, ProjectState.IMPLEMENTING)


def test_intake_failure_recoverable():
    p = _new_project()
    transition(p, ProjectState.INTAKE_FAILED)
    transition(p, ProjectState.INTAKE)
    transition(p, ProjectState.DRAFTING)


def test_archived_is_terminal():
    p = _new_project()
    transition(p, ProjectState.DRAFTING)
    transition(p, ProjectState.REVIEWING)
    transition(p, ProjectState.AWAITING_APPROVAL)
    transition(p, ProjectState.DECOMPOSING)
    transition(p, ProjectState.IMPLEMENTING)
    transition(p, ProjectState.COMPLETED)
    transition(p, ProjectState.ARCHIVED)
    with pytest.raises(IllegalTransitionError):
        transition(p, ProjectState.IMPLEMENTING)
