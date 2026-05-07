"""CLI runtime adapter — invokes external CLIs (Claude/Codex/Gemini) as subprocesses.

This module centralizes all subprocess management so individual agent
implementations can stay declarative. We support three CLI shapes:

* **Claude Code CLI** (``claude --print``): prompt is piped on stdin; output is
  read from stdout.
* **Codex CLI** (``codex exec``): prompt is appended as the trailing positional
  argument (``codex exec ... <prompt>``).
* **Gemini CLI** (``gemini -p <prompt> -y``): prompt is passed via ``-p``.

All three share the same :class:`CLIResult` shape so callers (agent role
implementations, the orchestrator, tests) do not care which CLI ran.

The adapter provides a ``mock`` runtime keyed by an injected callable so tests
can run the full happy path without spawning real processes or hitting the
network. The ``runtime`` field on :class:`AgentRuntimeConfig` selects the
shape — pass ``runtime="mock"`` and an entry in ``mock_responders`` to wire it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mythos.config import AgentRuntimeConfig
from mythos.models import AgentRole

MockResponder = Callable[["CLIInvocation"], "Awaitable[CLIResult] | CLIResult"]


@dataclass
class CLIInvocation:
    role: AgentRole
    prompt: str
    workspace: Path
    runtime: str
    command: list[str]
    env: dict[str, str]
    timeout_seconds: int


@dataclass
class CLIResult:
    role: AgentRole
    runtime: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    workspace: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None


def _build_env(cfg: AgentRuntimeConfig) -> dict[str, str]:
    env = os.environ.copy()
    if cfg.runtime == "claude":
        if cfg.base_url:
            env["ANTHROPIC_BASE_URL"] = cfg.base_url
        if cfg.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = cfg.auth_token
        if cfg.model:
            env["ANTHROPIC_MODEL"] = cfg.model
    elif cfg.runtime == "codex":
        if cfg.base_url:
            env["OPENAI_BASE_URL"] = cfg.base_url
        if cfg.auth_token:
            env["OPENAI_API_KEY"] = cfg.auth_token
    elif cfg.runtime == "gemini":
        # Gemini uses host's preconfigured creds; nothing to inject by default.
        pass
    env.update(cfg.extra_env)
    return env


def _build_command(cfg: AgentRuntimeConfig, prompt: str) -> tuple[list[str], bool]:
    """Returns (argv, send_prompt_via_stdin)."""

    base = list(cfg.command)
    if cfg.runtime == "claude":
        # Claude Code CLI consumes the prompt on stdin when `--print` is used.
        return base, True
    if cfg.runtime == "codex":
        # Codex `exec` takes the prompt as the trailing positional arg.
        return base + [prompt], False
    if cfg.runtime == "gemini":
        # gemini -p <prompt> ; ensure -p comes before -m to match docs.
        return ["gemini", "-p", prompt] + [a for a in base[1:] if a != "gemini"], False
    raise ValueError(f"Unknown runtime: {cfg.runtime}")


class CLIRuntime:
    """Spawns external CLIs and returns their output.

    Tests inject ``mock_responders`` keyed by AgentRole (or by runtime name) to
    bypass subprocess execution entirely.
    """

    def __init__(
        self,
        *,
        agent_configs: dict[AgentRole, AgentRuntimeConfig],
        mock_responders: dict[AgentRole, MockResponder] | None = None,
    ) -> None:
        self._configs = agent_configs
        self._mocks = dict(mock_responders or {})

    def set_mock(self, role: AgentRole, responder: MockResponder) -> None:
        self._mocks[role] = responder

    def has_mock(self, role: AgentRole) -> bool:
        return role in self._mocks

    async def run(self, role: AgentRole, prompt: str, workspace: Path) -> CLIResult:
        cfg = self._configs[role]
        workspace.mkdir(parents=True, exist_ok=True)
        env = _build_env(cfg)
        argv, via_stdin = _build_command(cfg, prompt)
        invocation = CLIInvocation(
            role=role,
            prompt=prompt,
            workspace=workspace,
            runtime=cfg.runtime,
            command=argv,
            env=env,
            timeout_seconds=cfg.timeout_seconds,
        )

        if role in self._mocks:
            return await self._invoke_mock(invocation)

        return await self._invoke_subprocess(invocation, via_stdin=via_stdin)

    async def _invoke_mock(self, invocation: CLIInvocation) -> CLIResult:
        responder = self._mocks[invocation.role]
        out: Any = responder(invocation)
        if asyncio.iscoroutine(out):
            out = await out
        if not isinstance(out, CLIResult):
            raise TypeError(
                f"Mock responder for {invocation.role.value} returned {type(out).__name__}, expected CLIResult"
            )
        return out

    async def _invoke_subprocess(self, invocation: CLIInvocation, *, via_stdin: bool) -> CLIResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *invocation.command,
                cwd=str(invocation.workspace),
                env=invocation.env,
                stdin=asyncio.subprocess.PIPE if via_stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            duration = time.monotonic() - start
            return CLIResult(
                role=invocation.role,
                runtime=invocation.runtime,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
                workspace=str(invocation.workspace),
                error=f"CLI binary not found: {invocation.command[0]}",
            )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=invocation.prompt.encode("utf-8") if via_stdin else None),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CLIResult(
                role=invocation.role,
                runtime=invocation.runtime,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                workspace=str(invocation.workspace),
                error=f"Timeout after {invocation.timeout_seconds}s",
            )
        duration = time.monotonic() - start
        return CLIResult(
            role=invocation.role,
            runtime=invocation.runtime,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_seconds=duration,
            workspace=str(invocation.workspace),
        )
