"""Project lifecycle state machine.

The orchestrator owns workflow transitions; agents do not. This module
defines the allowed transitions and a guard helper.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

from mythos.models import ProjectPhase as P


# Transition table: from-phase -> {allowed to-phases}
ALLOWED_TRANSITIONS: Dict[P, FrozenSet[P]] = {
    P.INTAKE: frozenset({P.PLANNING, P.FAILED, P.CANCELED}),
    P.PLANNING: frozenset({P.CLARIFICATION, P.DRAFT_READY, P.FAILED, P.CANCELED}),
    P.CLARIFICATION: frozenset({P.PLANNING, P.DRAFT_READY, P.FAILED, P.CANCELED}),
    P.DRAFT_READY: frozenset({P.REVIEW, P.FAILED, P.CANCELED}),
    P.REVIEW: frozenset({P.REVISION, P.AWAITING_APPROVAL, P.FAILED, P.CANCELED}),
    P.REVISION: frozenset({P.PLANNING, P.DRAFT_READY, P.FAILED, P.CANCELED}),
    P.AWAITING_APPROVAL: frozenset({P.APPROVED, P.REVISION, P.FAILED, P.CANCELED}),
    P.APPROVED: frozenset({P.DECOMPOSING, P.FAILED, P.CANCELED}),
    P.DECOMPOSING: frozenset({P.IMPLEMENTING, P.FAILED, P.CANCELED}),
    P.IMPLEMENTING: frozenset({P.TESTING, P.COMPLETED, P.FAILED, P.CANCELED}),
    P.TESTING: frozenset({P.COMPLETED, P.IMPLEMENTING, P.FAILED, P.CANCELED}),
    P.COMPLETED: frozenset(),
    P.FAILED: frozenset({P.PLANNING, P.IMPLEMENTING, P.TESTING}),  # retry from anchor
    P.CANCELED: frozenset(),
}


# Phases at which implementation dispatch is permitted.
IMPLEMENTATION_DISPATCH_PHASES = frozenset({P.APPROVED, P.DECOMPOSING})


class IllegalTransition(Exception):
    """Raised when an attempted phase transition is not allowed."""


def can_transition(from_phase: P, to_phase: P) -> bool:
    if from_phase == to_phase:
        return True
    return to_phase in ALLOWED_TRANSITIONS.get(from_phase, frozenset())


def assert_transition(from_phase: P, to_phase: P) -> None:
    if not can_transition(from_phase, to_phase):
        raise IllegalTransition(
            f"Illegal phase transition: {from_phase.value} -> {to_phase.value}"
        )


def can_dispatch_implementation(phase: P) -> bool:
    return phase in IMPLEMENTATION_DISPATCH_PHASES
