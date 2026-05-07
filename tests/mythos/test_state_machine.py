"""State-machine guards: implementation cannot start before approval, etc."""

from __future__ import annotations

import pytest

from mythos.state import Project, ProjectState


def test_initial_state_is_intake():
    p = Project.new("build a thing", requester_id=1)
    assert p.state is ProjectState.INTAKE


def test_cannot_skip_to_implementation_from_intake():
    p = Project.new("x", requester_id=1)
    with pytest.raises(ValueError):
        p.transition_to(ProjectState.IMPLEMENTING)


def test_cannot_decompose_before_approval():
    p = Project.new("x", requester_id=1)
    p.transition_to(ProjectState.PLANNING)
    p.transition_to(ProjectState.REVIEW)
    # cannot jump straight to decomposing from REVIEW
    with pytest.raises(ValueError):
        p.transition_to(ProjectState.DECOMPOSING)


def test_normal_path_works():
    p = Project.new("x", requester_id=1)
    p.transition_to(ProjectState.PLANNING)
    p.transition_to(ProjectState.REVIEW)
    p.transition_to(ProjectState.AWAITING_APPROVAL)
    p.transition_to(ProjectState.DECOMPOSING)
    p.transition_to(ProjectState.IMPLEMENTING)
    p.transition_to(ProjectState.TESTING)
    p.transition_to(ProjectState.COMPLETE)


def test_review_can_loop_back_to_planning():
    p = Project.new("x", requester_id=1)
    p.transition_to(ProjectState.PLANNING)
    p.transition_to(ProjectState.REVIEW)
    p.transition_to(ProjectState.PLANNING)  # revision round


def test_specs_versioned():
    p = Project.new("x", requester_id=1)
    s1 = p.add_spec("v1 body")
    s2 = p.add_spec("v2 body")
    assert s1.version == 1
    assert s2.version == 2
    assert p.latest_spec().version == 2


def test_review_requires_a_spec():
    p = Project.new("x", requester_id=1)
    with pytest.raises(RuntimeError):
        p.add_review("oops")
