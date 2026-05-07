"""Subprocess runners for each CLI backend.

Each runner takes a prompt + workdir, spawns the appropriate CLI as a
subprocess with the configured env/args, and returns the stdout. The
runners are async so the orchestrator can fan out to multiple agents
concurrently without blocking the Discord event loop.

These commands match the build instructions exactly:

* Claude Code: ``claude --dangerously-skip-permissions --effort high --print``
  with ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_MODEL``.
* Codex: ``codex exec -m gpt-5.5 -c model_reasoning_effort=high
  --skip-git-repo-check`` with ``OPENAI_BASE_URL``, ``OPENAI_API_KEY``.
* Gemini: ``gemini -p <prompt> -m gemini-3.1-pro-preview -y`` (uses host's
  preconfigured creds).

For tests, runners can be swapped with ``FakeAgentRunner`` which returns
canned responses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Protocol

from mythos.agents import AGENT_ROSTER, AgentBackend, AgentRole
from mythos.config import AgentConfig

logger = logging.getLogger(__name__)


class AgentTimeoutError(RuntimeError):
    """Raised when an agent CLI exceeds its configured timeout."""


class AgentExecError(RuntimeError):
    """Raised when an agent CLI exits non-zero."""


@dataclass
class AgentResult:
    role: AgentRole
    stdout: str
    stderr: str
    returncode: int
    duration_s: float


class AgentRunner(Protocol):
    """Drives one CLI invocation per call. Stateless — caller assembles prompts."""

    async def run(
        self,
        role: AgentRole,
        prompt: str,
        workdir: Path,
        cfg: AgentConfig,
    ) -> AgentResult: ...


# ---- Real subprocess runner ----------------------------------------------

class SubprocessAgentRunner:
    """Default runner: spawns the configured CLI command as a subprocess."""

    async def run(
        self,
        role: AgentRole,
        prompt: str,
        workdir: Path,
        cfg: AgentConfig,
    ) -> AgentResult:
        spec = AGENT_ROSTER[role]
        cmd, stdin_payload, env = self._build_invocation(spec.backend, prompt, cfg)
        logger.info(
            "[mythos] spawning %s (%s) in %s: %s",
            spec.name,
            spec.backend.value,
            workdir,
            " ".join(shlex.quote(p) for p in cmd),
        )

        workdir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        start = loop.time()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_payload),
                timeout=cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise AgentTimeoutError(
                f"{spec.name} ({spec.backend.value}) timed out after "
                f"{cfg.timeout_seconds}s"
            )

        duration = loop.time() - start
        result = AgentResult(
            role=role,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
            duration_s=duration,
        )
        if result.returncode != 0:
            raise AgentExecError(
                f"{spec.name} exited {result.returncode}: {result.stderr[:500]}"
            )
        return result

    # -- command construction --------------------------------------------

    def _build_invocation(
        self,
        backend: AgentBackend,
        prompt: str,
        cfg: AgentConfig,
    ) -> tuple[List[str], Optional[bytes], Dict[str, str]]:
        """Return ``(argv, stdin_payload, env)`` for one CLI invocation."""
        env = os.environ.copy()
        env.update(cfg.auth_env)

        if backend is AgentBackend.CLAUDE_CODE:
            cmd = [
                "claude",
                "--dangerously-skip-permissions",
                "--effort",
                cfg.effort or "high",
                "--print",
            ]
            cmd.extend(cfg.extra_args)
            return cmd, prompt.encode("utf-8"), env

        if backend is AgentBackend.CODEX:
            # ``codex exec`` reads the prompt from stdin (or arg). We pass it
            # as a positional after the flags.
            cmd = [
                "codex",
                "exec",
                "-m",
                cfg.model,
                "-c",
                f"model_reasoning_effort={cfg.effort or 'high'}",
                "--skip-git-repo-check",
            ]
            cmd.extend(cfg.extra_args)
            cmd.append(prompt)
            return cmd, None, env

        if backend is AgentBackend.GEMINI:
            # Gemini CLI reads -p as the prompt argument; -y is yolo mode.
            cmd = ["gemini", "-p", prompt, "-m", cfg.model, "-y"]
            cmd.extend(cfg.extra_args)
            return cmd, None, env

        raise ValueError(f"unknown backend {backend}")


# ---- Fake runner for tests ------------------------------------------------

ResponderFn = Callable[[AgentRole, str, Path, AgentConfig], Awaitable[str] | str]


class FakeAgentRunner:
    """Deterministic runner for tests.

    Pass either a dict ``{role: response}`` of canned strings, or a callable
    ``(role, prompt, workdir, cfg) -> str`` for dynamic responses. The
    callable may be sync or async.
    """

    def __init__(
        self,
        responses: Optional[Dict[AgentRole, str]] = None,
        responder: Optional[ResponderFn] = None,
    ) -> None:
        self._canned = dict(responses or {})
        self._responder = responder
        self.calls: List[tuple[AgentRole, str, Path]] = []

    def set_response(self, role: AgentRole, response: str) -> None:
        self._canned[role] = response

    async def run(
        self,
        role: AgentRole,
        prompt: str,
        workdir: Path,
        cfg: AgentConfig,
    ) -> AgentResult:
        self.calls.append((role, prompt, workdir))
        workdir.mkdir(parents=True, exist_ok=True)
        if self._responder is not None:
            result = self._responder(role, prompt, workdir, cfg)
            if asyncio.iscoroutine(result):
                response = await result
            else:
                response = result  # type: ignore[assignment]
        elif role in self._canned:
            response = self._canned[role]
        else:
            response = f"[fake-{role.value}] ok"
        return AgentResult(
            role=role,
            stdout=str(response),
            stderr="",
            returncode=0,
            duration_s=0.0,
        )
