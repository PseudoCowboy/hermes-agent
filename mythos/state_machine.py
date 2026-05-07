"""State machine for Mythos projects.

Enforces legal transitions between project states and records history.
Per spec FR-024: intake, clarifying, drafting, reviewing, awaiting_approval,
decomposing, implementing, blocked, completed, archived.
"""

from __future__ import annotations

import time

from mythos.models import Project, ProjectState

# Legal transitions. blocked / archived can be reached from anywhere.
_TRANSITIONS: dict[ProjectState, set[ProjectState]] = {
    ProjectState.INTAKE: {ProjectState.CLARIFYING, ProjectState.DRAFTING, ProjectState.INTAKE_FAILED},
    ProjectState.INTAKE_FAILED: {ProjectState.INTAKE, ProjectState.ARCHIVED},
    ProjectState.CLARIFYING: {ProjectState.DRAFTING, ProjectState.CLARIFYING},
    ProjectState.DRAFTING: {ProjectState.REVIEWING, ProjectState.CLARIFYING},
    ProjectState.REVIEWING: {ProjectState.AWAITING_APPROVAL, ProjectState.DRAFTING},
    ProjectState.AWAITING_APPROVAL: {ProjectState.DECOMPOSING, ProjectState.DRAFTING, ProjectState.AWAITING_APPROVAL},
    ProjectState.DECOMPOSING: {ProjectState.IMPLEMENTING, ProjectState.BLOCKED},
    ProjectState.IMPLEMENTING: {ProjectState.COMPLETED, ProjectState.BLOCKED, ProjectState.IMPLEMENTING},
    ProjectState.BLOCKED: {ProjectState.IMPLEMENTING, ProjectState.DECOMPOSING, ProjectState.AWAITING_APPROVAL, ProjectState.ARCHIVED},
    ProjectState.COMPLETED: {ProjectState.ARCHIVED, ProjectState.IMPLEMENTING},
    ProjectState.ARCHIVED: set(),
}


class IllegalTransitionError(RuntimeError):
    pass


def can_transition(from_state: ProjectState, to_state: ProjectState) -> bool:
    if to_state == from_state:
        # Idempotent self-transitions allowed for re-entry semantics (e.g. another clarification round).
        return to_state in _TRANSITIONS.get(from_state, set()) or to_state == from_state
    return to_state in _TRANSITIONS.get(from_state, set())


def transition(project: Project, to_state: ProjectState) -> None:
    if not can_transition(project.state, to_state):
        raise IllegalTransitionError(
            f"Project {project.id}: illegal transition {project.state.value} -> {to_state.value}"
        )
    project.state_history.append((time.time(), to_state))
    project.state = to_state
    if to_state == ProjectState.ARCHIVED:
        project.archived_at = time.time()
