"""Tests for new implementer persona registration (P7a-2)."""

from __future__ import annotations

import pytest

from gateway import personas


def test_implementer_personas_registered():
    """Both persona ids must have prompt paths."""
    assert personas.IMPLEMENTER_FRONTEND in personas.PERSONA_PROMPT_PATHS
    assert personas.IMPLEMENTER_BACKEND in personas.PERSONA_PROMPT_PATHS


def test_implementer_personas_in_all_personas_tuple():
    """ALL_PERSONAS must enumerate the new ids so future code that iterates
    over registered personas (validation, list endpoints) sees them."""
    assert personas.IMPLEMENTER_FRONTEND in personas.ALL_PERSONAS
    assert personas.IMPLEMENTER_BACKEND in personas.ALL_PERSONAS


def test_load_persona_system_prompt_frontend_returns_nonempty():
    text = personas.load_persona_system_prompt(personas.IMPLEMENTER_FRONTEND)
    assert isinstance(text, str)
    assert text.strip(), "frontend prompt is empty"
    # Sanity: file should mention what the persona is for so the model
    # knows it's an implementer.  Avoid being over-specific so we don't
    # break on unrelated wording tweaks.
    assert "implementer" in text.lower() or "frontend" in text.lower()


def test_load_persona_system_prompt_backend_returns_nonempty():
    text = personas.load_persona_system_prompt(personas.IMPLEMENTER_BACKEND)
    assert isinstance(text, str)
    assert text.strip(), "backend prompt is empty"
    assert "implementer" in text.lower() or "backend" in text.lower()


def test_role_to_persona_frontend():
    assert personas.role_to_persona("frontend") == personas.IMPLEMENTER_FRONTEND


def test_role_to_persona_backend():
    assert personas.role_to_persona("backend") == personas.IMPLEMENTER_BACKEND


def test_role_to_persona_rejects_unknown():
    with pytest.raises(ValueError):
        personas.role_to_persona("fullstack")


def test_role_to_persona_rejects_empty_string():
    with pytest.raises(ValueError):
        personas.role_to_persona("")


def test_unknown_persona_raises():
    with pytest.raises(personas.UnknownPersonaError):
        personas.load_persona_system_prompt("test_agent")  # registered id, no prompt yet
