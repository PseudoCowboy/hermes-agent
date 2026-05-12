"""Uniform Agent contract + per-CLI subprocess adapter.

All four backends (Claude Code, Codex, Gemini, plus the no-op test
double) implement the same ``Agent`` interface. The Router and
Supervisor never branch on backend.

The single concrete implementation, ``CliAgent``, drives the underlying
CLI as a subprocess. Prompt delivery follows the per-binding convention:

  * ``prompt_as_arg=True``  → prompt is appended as the final positional
    arg (Gemini's ``-p <prompt>`` style).
  * ``prompt_as_arg=False`` → prompt is piped to stdin (Claude Code's
    ``--print`` and Codex's ``exec`` both read stdin).

The CLI's stdout is captured verbatim and returned as the agent's
response. A non-zero exit code is surfaced as ``AgentError``.
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from mythos.roles import Role, RoleBinding, ROLE_TAG


@dataclass
class AgentInput:
    """A single agent invocation request."""

    prompt: str
    workdir: Path
    extra_env: Dict[str, str] = field(default_factory=dict)
    # Optional context files (paths) the agent can be told about; not auto-loaded.
    references: List[Path] = field(default_factory=list)


@dataclass
class AgentOutput:
    """Result of an agent invocation."""

    role: Role
    text: str
    exit_code: int = 0
    duration_seconds: float = 0.0
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class AgentError(RuntimeError):
    """Raised when an agent invocation fails outright (process error)."""


class Agent(ABC):
    """Uniform agent contract."""

    role: Role

    @abstractmethod
    async def invoke(self, request: AgentInput) -> AgentOutput:
        ...


class CliAgent(Agent):
    """Drive a coding CLI as a subprocess.

    Construct one of these per (project, role); the Supervisor caches them.
    """

    def __init__(
        self,
        binding: RoleBinding,
        timeout_seconds: int = 600,
    ) -> None:
        self.role = binding.role
        self.binding = binding
        self.timeout_seconds = timeout_seconds

    def _build_command(self, prompt: str) -> List[str]:
        cmd = list(self.binding.command)
        if self.binding.prompt_as_arg:
            cmd.append(prompt)
        return cmd

    def _build_env(self, extra: Dict[str, str]) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(self.binding.env)
        env.update(extra)
        return env

    async def invoke(self, request: AgentInput) -> AgentOutput:
        cmd = self._build_command(request.prompt)
        env = self._build_env(request.extra_env)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if not self.binding.prompt_as_arg else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(request.workdir),
                env=env,
            )
        except FileNotFoundError as e:
            raise AgentError(
                f"CLI binary not found for {self.role.value}: {cmd[0]} "
                f"({e}). Install or PATH-correct it before running mythos."
            ) from e

        try:
            stdin_payload = (
                None if self.binding.prompt_as_arg else request.prompt.encode("utf-8")
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_payload),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise AgentError(
                f"{self.role.value} CLI timed out after {self.timeout_seconds}s"
            )

        duration = time.monotonic() - start
        return AgentOutput(
            role=self.role,
            text=stdout_b.decode("utf-8", errors="replace"),
            exit_code=int(proc.returncode or 0),
            duration_seconds=duration,
            stderr=stderr_b.decode("utf-8", errors="replace"),
        )


class StubAgent(Agent):
    """Test double: returns a canned response without spawning anything.

    Wire one of these into the Supervisor in tests so the happy-path
    integration test runs without claude/codex/gemini installed.
    """

    def __init__(
        self,
        role: Role,
        response_fn,  # callable(AgentInput) -> str | callable(AgentInput) -> AgentOutput
    ) -> None:
        self.role = role
        self._fn = response_fn

    async def invoke(self, request: AgentInput) -> AgentOutput:
        result = self._fn(request)
        if isinstance(result, AgentOutput):
            return result
        return AgentOutput(role=self.role, text=str(result))


def role_tag(role: Role) -> str:
    """Public helper so non-agent code (router, mediator) can prefix messages."""
    return ROLE_TAG[role]
