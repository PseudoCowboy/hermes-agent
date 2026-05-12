"""Discord orchestration role helpers.

Role bot configuration accepts both implementation role names
(``frontend``) and user-facing mythological names (``apollo``). Keep
normalization in one small module so config loading, Discord sending,
and tests agree on the same aliases without importing gateway config or
Discord adapter code.
"""

from __future__ import annotations

from typing import Dict, Tuple


ROLE_ALIASES: Dict[str, str] = {
    "main": "orchestrator",
    "athena": "orchestrator",
    "orchestrator": "orchestrator",
    "draft": "draft_plan",
    "draft_plan": "draft_plan",
    "draft-plan": "draft_plan",
    "prometheus": "draft_plan",
    "review": "review",
    "reviewer": "review",
    "argus": "review",
    "frontend": "frontend",
    "front_end": "frontend",
    "implementer_frontend": "frontend",
    "apollo": "frontend",
    "backend": "backend",
    "back_end": "backend",
    "implementer_backend": "backend",
    "atlas": "backend",
    "test": "test",
    "tester": "test",
    "test_agent": "test",
    "test-agent": "test",
    "hephaestus": "test",
}


ROLE_TOKEN_ENV_VARS: Dict[str, Tuple[str, ...]] = {
    "orchestrator": (
        "DISCORD_ORCHESTRATOR_BOT_TOKEN",
        "DISCORD_ATHENA_BOT_TOKEN",
    ),
    "draft_plan": (
        "DISCORD_DRAFT_PLAN_BOT_TOKEN",
        "DISCORD_PROMETHEUS_BOT_TOKEN",
    ),
    "review": (
        "DISCORD_REVIEW_BOT_TOKEN",
        "DISCORD_ARGUS_BOT_TOKEN",
    ),
    "frontend": (
        "DISCORD_FRONTEND_BOT_TOKEN",
        "DISCORD_APOLLO_BOT_TOKEN",
    ),
    "backend": (
        "DISCORD_BACKEND_BOT_TOKEN",
        "DISCORD_ATLAS_BOT_TOKEN",
    ),
    "test": (
        "DISCORD_TEST_BOT_TOKEN",
        "DISCORD_HEPHAESTUS_BOT_TOKEN",
    ),
}


def normalize_discord_role(role: str | None) -> str:
    """Return the canonical Discord bot role for *role*.

    Unknown role names are still normalized to a lowercase underscore
    segment so custom roles can be configured without changing this
    module.
    """
    raw = (role or "").strip().lower().replace(" ", "_")
    raw = raw.replace("-", "_")
    if not raw:
        return ""
    return ROLE_ALIASES.get(raw, raw)
