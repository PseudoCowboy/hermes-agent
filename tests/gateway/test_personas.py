"""Tests for gateway/personas.py (P6)."""

from __future__ import annotations

import pytest

from gateway.personas import (
    ALL_PERSONAS,
    IMPLEMENTER,
    IMPLEMENTER_BACKEND,
    IMPLEMENTER_FRONTEND,
    ORCHESTRATOR,
    PERSONA_PROMPT_PATHS,
    TEST_AGENT,
    UnknownPersonaError,
    load_persona_system_prompt,
)


def test_persona_constants_present():
    assert ORCHESTRATOR == "orchestrator"
    assert IMPLEMENTER == "implementer"
    assert TEST_AGENT == "test_agent"
    # P7a-2 added split implementer personas; the legacy IMPLEMENTER
    # constant is kept for back-compat but ALL_PERSONAS now also lists
    # the role-specific ids.
    assert set(ALL_PERSONAS) == {
        ORCHESTRATOR, IMPLEMENTER, TEST_AGENT,
        IMPLEMENTER_FRONTEND, IMPLEMENTER_BACKEND,
    }


def test_orchestrator_prompt_loads_nonempty():
    text = load_persona_system_prompt(ORCHESTRATOR)
    assert isinstance(text, str)
    assert text.strip(), "orchestrator persona prompt must not be empty"


def test_unknown_persona_raises():
    with pytest.raises(UnknownPersonaError):
        load_persona_system_prompt("definitely_not_a_persona")


def test_implementer_and_test_agent_unregistered_until_p7():
    """Implementer / test-agent prompts are intentionally not registered yet."""
    assert IMPLEMENTER not in PERSONA_PROMPT_PATHS
    assert TEST_AGENT not in PERSONA_PROMPT_PATHS
    with pytest.raises(UnknownPersonaError):
        load_persona_system_prompt(IMPLEMENTER)
    with pytest.raises(UnknownPersonaError):
        load_persona_system_prompt(TEST_AGENT)
