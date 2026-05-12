"""Role registry and CLI binding for Mythos agents.

Each agent role has a stable mythological name, a backend kind
(claude / codex / gemini), and a default CLI command + env vars.
The mapping is data, not code — overriding it is a config change,
not a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Role(str, Enum):
    """The six roles in a Mythos project. Names are mythological."""

    HERMES = "hermes"           # Main / orchestrator
    PROMETHEUS = "prometheus"   # Draft plan
    ARGUS = "argus"             # Review
    HEPHAESTUS = "hephaestus"   # Test
    APOLLO = "apollo"           # Frontend
    ATLAS = "atlas"             # Backend

    @property
    def display(self) -> str:
        return self.value.capitalize()


class Backend(str, Enum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"


@dataclass
class RoleBinding:
    """Binds a logical role to a concrete CLI invocation.

    The CLI is invoked once per agent turn with the prompt either
    passed as an argument (gemini's ``-p``) or piped via stdin
    (claude code's ``--print`` and codex's ``exec``).
    """

    role: Role
    backend: Backend
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    # When True, prompt is appended as final positional arg (gemini -p <prompt>);
    # when False, prompt is piped to stdin (claude/codex).
    prompt_as_arg: bool = False


def _claude_code_binding(role: Role) -> RoleBinding:
    return RoleBinding(
        role=role,
        backend=Backend.CLAUDE_CODE,
        command=[
            "claude",
            "--dangerously-skip-permissions",
            "--effort",
            "high",
            "--print",
        ],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
    )


def _codex_binding(role: Role) -> RoleBinding:
    return RoleBinding(
        role=role,
        backend=Backend.CODEX,
        command=[
            "codex",
            "exec",
            "-m",
            "gpt-5.5",
            "-c",
            "model_reasoning_effort=high",
            "--skip-git-repo-check",
        ],
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
            "OPENAI_API_KEY": "dummy",
        },
    )


def _gemini_binding(role: Role) -> RoleBinding:
    # Gemini uses its own preconfigured credentials; no proxy.
    # Prompt goes after -p; the trailing literal `-p` is replaced
    # with the prompt string at invocation time. The env gets nothing.
    return RoleBinding(
        role=role,
        backend=Backend.GEMINI,
        command=[
            "gemini",
            "-m",
            "gemini-3.1-pro-preview",
            "-y",
            "-p",
        ],
        env={},
        prompt_as_arg=True,
    )


# Default role → CLI binding map. Override via MythosConfig.
ROLE_BINDINGS: Dict[Role, RoleBinding] = {
    Role.HERMES: _claude_code_binding(Role.HERMES),
    Role.PROMETHEUS: _claude_code_binding(Role.PROMETHEUS),
    Role.ATLAS: _claude_code_binding(Role.ATLAS),
    Role.ARGUS: _codex_binding(Role.ARGUS),
    Role.HEPHAESTUS: _codex_binding(Role.HEPHAESTUS),
    Role.APOLLO: _gemini_binding(Role.APOLLO),
}


# Mention/role tags posted with each agent message so users see who's talking.
ROLE_TAG: Dict[Role, str] = {
    Role.HERMES: "[Hermes · Main]",
    Role.PROMETHEUS: "[Prometheus · Draft Plan]",
    Role.ARGUS: "[Argus · Review]",
    Role.HEPHAESTUS: "[Hephaestus · Test]",
    Role.APOLLO: "[Apollo · Frontend]",
    Role.ATLAS: "[Atlas · Backend]",
}


def role_from_string(s: str) -> Optional[Role]:
    """Parse a role name (case-insensitive). Returns None on miss."""
    s = (s or "").strip().lower()
    for r in Role:
        if r.value == s:
            return r
    return None


def apply_env_overrides(bindings: Dict[Role, RoleBinding]) -> Dict[Role, RoleBinding]:
    """Apply MYTHOS_<ROLE>_MODEL etc. env overrides to a binding map.

    Environment variables (all optional):
      * ``MYTHOS_CLAUDE_BASE_URL`` / ``MYTHOS_CLAUDE_MODEL`` /
        ``MYTHOS_CLAUDE_AUTH_TOKEN`` — override Claude defaults.
      * ``MYTHOS_CODEX_BASE_URL`` / ``MYTHOS_CODEX_MODEL`` /
        ``MYTHOS_CODEX_API_KEY`` — override Codex defaults.
      * ``MYTHOS_GEMINI_MODEL`` — override Gemini model.
    """
    out: Dict[Role, RoleBinding] = {}
    for role, binding in bindings.items():
        new_env = dict(binding.env)
        new_command = list(binding.command)

        if binding.backend == Backend.CLAUDE_CODE:
            if v := os.getenv("MYTHOS_CLAUDE_BASE_URL"):
                new_env["ANTHROPIC_BASE_URL"] = v
            if v := os.getenv("MYTHOS_CLAUDE_AUTH_TOKEN"):
                new_env["ANTHROPIC_AUTH_TOKEN"] = v
            if v := os.getenv("MYTHOS_CLAUDE_MODEL"):
                new_env["ANTHROPIC_MODEL"] = v
        elif binding.backend == Backend.CODEX:
            if v := os.getenv("MYTHOS_CODEX_BASE_URL"):
                new_env["OPENAI_BASE_URL"] = v
            if v := os.getenv("MYTHOS_CODEX_API_KEY"):
                new_env["OPENAI_API_KEY"] = v
            if v := os.getenv("MYTHOS_CODEX_MODEL"):
                # Replace the value after `-m`
                if "-m" in new_command:
                    i = new_command.index("-m")
                    if i + 1 < len(new_command):
                        new_command[i + 1] = v
        elif binding.backend == Backend.GEMINI:
            if v := os.getenv("MYTHOS_GEMINI_MODEL"):
                if "-m" in new_command:
                    i = new_command.index("-m")
                    if i + 1 < len(new_command):
                        new_command[i + 1] = v

        out[role] = RoleBinding(
            role=binding.role,
            backend=binding.backend,
            command=new_command,
            env=new_env,
            prompt_as_arg=binding.prompt_as_arg,
        )
    return out
