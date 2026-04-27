"""Persona definitions for per-session agent workers (P6).

The Discord orchestration product spec (§4 plan structure, §5 stream
execution, §6 verification) names three agent roles:

- ``orchestrator`` — owns clarification, plan drafting, plan posting,
  and reaction-driven approval.  P6 ships this one end-to-end.
- ``implementer`` — bound to a single stream channel, executes one
  workstream.  Registered as an enum value here so P7 only needs to
  add the prompt and the worker integration.
- ``test_agent`` — verifies acceptance criteria.  Same story; P7+.

The persona an agent runs is selected at session-construction time and
stored on ``LongLivedSession.persona``.  The session worker
(``gateway.session_agent_worker``) consults this module to load the
matching system prompt at first-turn time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict


# Public persona identifiers.  String constants (not an Enum) so they
# round-trip cleanly through dataclass attributes, JSON, and SQLite if
# we ever persist them.
ORCHESTRATOR = "orchestrator"
IMPLEMENTER = "implementer"  # P7
TEST_AGENT = "test_agent"  # P7

ALL_PERSONAS = (ORCHESTRATOR, IMPLEMENTER, TEST_AGENT)


# Repo root resolution: this file lives at gateway/personas.py, so the
# repo root is two parents up.  Prompts are versioned alongside the code
# rather than read from ~/.hermes/ so different deployments can't drift
# silently from the contract the worker code expects.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _REPO_ROOT / "prompts"


# Map persona → prompt file (relative to repo root).  Only personas
# with a registered prompt can be loaded; P7 will register IMPLEMENTER
# and TEST_AGENT here.
PERSONA_PROMPT_PATHS: Dict[str, str] = {
    ORCHESTRATOR: "prompts/orchestrator.md",
    # IMPLEMENTER: "prompts/implementer.md",  # P7
    # TEST_AGENT: "prompts/test_agent.md",    # P7
}


class UnknownPersonaError(ValueError):
    """Raised when ``load_persona_system_prompt`` is called with a
    persona id that has no registered prompt path."""


def load_persona_system_prompt(persona: str) -> str:
    """Read the system prompt for ``persona`` off disk.

    Raises ``UnknownPersonaError`` for personas that have no registered
    prompt (P7 personas before they ship); raises ``FileNotFoundError``
    if the prompt file is missing (deployment drift).
    """
    rel = PERSONA_PROMPT_PATHS.get(persona)
    if rel is None:
        raise UnknownPersonaError(
            f"persona {persona!r} has no registered prompt path; "
            f"known personas: {sorted(PERSONA_PROMPT_PATHS)}"
        )
    path = _REPO_ROOT / rel
    return path.read_text(encoding="utf-8")
