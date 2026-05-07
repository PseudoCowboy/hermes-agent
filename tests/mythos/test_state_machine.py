"""Unit tests for the project state machine."""

from mythos.models import ProjectPhase as P
from mythos.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_dispatch_implementation,
    can_transition,
)


def test_allowed_transitions_table_is_total():
    """Every phase must have an explicit row in the table."""
    for phase in P:
        assert phase in ALLOWED_TRANSITIONS, f"missing row for {phase}"


def test_intake_to_planning_is_allowed():
    assert can_transition(P.INTAKE, P.PLANNING)


def test_intake_cannot_jump_to_implementing():
    assert not can_transition(P.INTAKE, P.IMPLEMENTING)


def test_planning_clarification_loop():
    assert can_transition(P.PLANNING, P.CLARIFICATION)
    assert can_transition(P.CLARIFICATION, P.PLANNING)
    assert can_transition(P.CLARIFICATION, P.DRAFT_READY)


def test_review_to_revision_to_planning():
    assert can_transition(P.REVIEW, P.REVISION)
    assert can_transition(P.REVISION, P.PLANNING)


def test_approval_required_before_implementation_dispatch():
    """Approval gate: implementation cannot dispatch from non-approved phases."""
    not_approved = [
        P.INTAKE, P.PLANNING, P.CLARIFICATION, P.DRAFT_READY,
        P.REVIEW, P.REVISION, P.AWAITING_APPROVAL,
    ]
    for phase in not_approved:
        assert not can_dispatch_implementation(phase), (
            f"phase {phase.value} should NOT permit implementation dispatch"
        )
    assert can_dispatch_implementation(P.APPROVED)
    assert can_dispatch_implementation(P.DECOMPOSING)


def test_completed_is_terminal():
    assert ALLOWED_TRANSITIONS[P.COMPLETED] == frozenset()


def test_canceled_is_terminal():
    assert ALLOWED_TRANSITIONS[P.CANCELED] == frozenset()


def test_assert_transition_raises_on_illegal():
    import pytest
    with pytest.raises(IllegalTransition):
        assert_transition(P.INTAKE, P.IMPLEMENTING)
