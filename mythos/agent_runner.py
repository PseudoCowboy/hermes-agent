"""Subprocess agent runner.

Each role is driven by an external CLI:
  * Claude Code CLI for Athena, Prometheus, Atlas
  * Codex CLI for Argus, Hephaestus
  * Gemini CLI for Apollo

The runner takes a `RunRequest` and produces a `RunResult`. Subprocess
execution is asynchronous (asyncio) and time-bounded, with stdout/stderr
captured to a per-run log file inside the project workspace.

We deliberately keep this independent of hermes' `delegate_task`, because
that helper is in-process / thread-based; the spec mandates real CLI
subprocesses with specific flags + env vars.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from .config import AgentCLI, MythosConfig
from .roles import ChannelKind, Role, ROLE_TO_CLI


@dataclass
class RunRequest:
    project_id: str
    run_id: str
    role: Role
    channel_kind: ChannelKind
    prompt: str
    workspace_dir: Path
    log_dir: Path


@dataclass
class RunResult:
    role: Role
    status: str  # completed | needs_input | failed
    summary: str
    raw_output: str
    question: Optional[str] = None
    log_path: Optional[Path] = None
    duration_seconds: float = 0.0
    return_code: Optional[int] = None
    artifacts: List[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------


def cli_for_role(cfg: MythosConfig, role: Role) -> AgentCLI:
    cli = ROLE_TO_CLI[role]
    if cli == "claude":
        return cfg.claude_cli
    if cli == "codex":
        return cfg.codex_cli
    if cli == "gemini":
        return cfg.gemini_cli
    raise ValueError(f"Unknown CLI for role {role}: {cli}")


def build_command(cli_spec: AgentCLI, prompt: str) -> List[str]:
    """Compose the full argv for a CLI invocation.

    * Claude Code: prompt is fed via stdin (so the printable command stays
      stable across very long prompts).
    * Codex: prompt is fed via stdin too; `codex exec` accepts that.
    * Gemini: must use `-p <prompt>` per the spec.
    """
    if cli_spec.cli == "gemini":
        return [*cli_spec.command, "-p", prompt]
    return list(cli_spec.command)


def needs_stdin_prompt(cli_spec: AgentCLI) -> bool:
    return cli_spec.cli in {"claude", "codex"}


def build_env(cli_spec: AgentCLI) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(cli_spec.env)
    return env


def parse_status_line(text: str) -> str:
    """Find the last `STATUS: <value>` marker in agent output."""
    found = "completed"
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("STATUS:"):
            value = s.split(":", 1)[1].strip().lower()
            if value:
                found = value
    return found


def parse_question(text: str) -> Optional[str]:
    """Extract the first `QUESTION:` line, if any."""
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("QUESTION:"):
            return s.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------


# Type for a function that actually executes the subprocess. Tests inject
# a fake; production uses `_default_executor`.
SubprocessExecutor = Callable[
    [List[str], Optional[str], Dict[str, str], Path, float],
    Awaitable[tuple[int, str, str]],
]


async def _default_executor(
    argv: List[str],
    stdin_text: Optional[str],
    env: Dict[str, str],
    cwd: Path,
    timeout: float,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_text.encode("utf-8") if stdin_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return -1, "", f"[mythos] subprocess timed out after {timeout}s"
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------


class AgentRunner:
    """Runs an agent CLI for a single request."""

    def __init__(self, config: MythosConfig,
                 executor: Optional[SubprocessExecutor] = None):
        self.config = config
        self._executor = executor or _default_executor
        self._sem = asyncio.Semaphore(config.max_concurrent_agent_jobs)

    async def run(self, request: RunRequest) -> RunResult:
        cli_spec = cli_for_role(self.config, request.role)
        argv = build_command(cli_spec, request.prompt)
        env = build_env(cli_spec)
        stdin_text = request.prompt if needs_stdin_prompt(cli_spec) else None

        request.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = request.log_dir / f"{request.run_id}.log"
        start = time.time()
        async with self._sem:
            return_code, stdout, stderr = await self._executor(
                argv, stdin_text, env, request.workspace_dir,
                float(self.config.agent_timeout_seconds),
            )
        duration = time.time() - start

        # Persist a structured log
        log_path.write_text(
            f"# Mythos agent run\n"
            f"role: {request.role.value}\n"
            f"channel_kind: {request.channel_kind.value}\n"
            f"argv: {argv}\n"
            f"return_code: {return_code}\n"
            f"duration_seconds: {duration:.2f}\n"
            f"\n--- PROMPT ---\n{request.prompt}\n"
            f"\n--- STDOUT ---\n{stdout}\n"
            f"\n--- STDERR ---\n{stderr}\n"
        )

        if return_code != 0:
            return RunResult(
                role=request.role,
                status="failed",
                summary=(stderr.strip().splitlines()[-1] if stderr.strip()
                          else f"agent exited {return_code}"),
                raw_output=stdout,
                log_path=log_path,
                duration_seconds=duration,
                return_code=return_code,
            )

        status = parse_status_line(stdout)
        question = parse_question(stdout)
        # Map agent-claimed statuses to canonical orchestrator statuses
        if question and status not in {"completed", "in_progress"}:
            status = "needs_input"
        elif status not in {"completed", "in_progress", "needs_input", "failed"}:
            status = "completed"

        # Truncate summary for Discord; full output goes via Discord routing
        summary = stdout.strip()
        return RunResult(
            role=request.role,
            status=status,
            summary=summary,
            raw_output=stdout,
            question=question,
            log_path=log_path,
            duration_seconds=duration,
            return_code=return_code,
        )
