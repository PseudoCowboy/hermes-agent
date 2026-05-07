"""Agent roles, mythological codenames, and the CLI command they drive.

Defaults match the benchmark task spec exactly, but every value is overridable
via config so the operator can change models or endpoints without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional


class CliKind(str, Enum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"


class AgentRole(str, Enum):
    MAIN = "main"
    DRAFT_PLAN = "draft_plan"
    REVIEW = "review"
    TEST = "test"
    FRONTEND = "frontend"
    BACKEND = "backend"


@dataclass(frozen=True)
class AgentDefinition:
    role: AgentRole
    name: str               # mythological codename (Athena, Prometheus, ...)
    cli: CliKind
    # Subprocess command template — list form, no shell.
    # `{prompt}` placeholder is substituted in the *cwd-input* file (see runners.py)
    # rather than as an argv element for CLIs that read prompts from stdin/--print.
    argv: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


# Default roster. The benchmark task locks command + env; we ship these as
# defaults. Operators can override per-role in config.yaml -> mythos.agents.<role>.
ROLE_REGISTRY: Mapping[AgentRole, AgentDefinition] = {
    AgentRole.MAIN: AgentDefinition(
        role=AgentRole.MAIN,
        name="Athena",
        cli=CliKind.CLAUDE_CODE,
        argv=["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
    ),
    AgentRole.DRAFT_PLAN: AgentDefinition(
        role=AgentRole.DRAFT_PLAN,
        name="Prometheus",
        cli=CliKind.CLAUDE_CODE,
        argv=["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
    ),
    AgentRole.REVIEW: AgentDefinition(
        role=AgentRole.REVIEW,
        name="Argus",
        cli=CliKind.CODEX,
        argv=[
            "codex", "exec",
            "-m", "gpt-5.5",
            "-c", "model_reasoning_effort=high",
            "--skip-git-repo-check",
        ],
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
            "OPENAI_API_KEY": "dummy",
        },
    ),
    AgentRole.TEST: AgentDefinition(
        role=AgentRole.TEST,
        name="Hephaestus",
        cli=CliKind.CODEX,
        argv=[
            "codex", "exec",
            "-m", "gpt-5.5",
            "-c", "model_reasoning_effort=high",
            "--skip-git-repo-check",
        ],
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
            "OPENAI_API_KEY": "dummy",
        },
    ),
    AgentRole.FRONTEND: AgentDefinition(
        role=AgentRole.FRONTEND,
        name="Apollo",
        cli=CliKind.GEMINI,
        # Gemini reads prompt from -p; prompt is appended at runtime.
        argv=["gemini", "-m", "gemini-3.1-pro-preview", "-y"],
        env={},  # uses host's preconfigured gemini credentials
    ),
    AgentRole.BACKEND: AgentDefinition(
        role=AgentRole.BACKEND,
        name="Atlas",
        cli=CliKind.CLAUDE_CODE,
        argv=["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
    ),
}


def role_for_name(name: str) -> Optional[AgentRole]:
    """Reverse lookup: 'Athena' -> AgentRole.MAIN."""
    for role, definition in ROLE_REGISTRY.items():
        if definition.name.lower() == name.lower():
            return role
    return None
