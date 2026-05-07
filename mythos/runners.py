"""Subprocess runners that drive the agent CLIs.

Three concrete CLIs (Claude Code, Codex, Gemini) and a fake runner that
tests use to simulate scripted outputs without spawning real binaries.

Each runner takes a structured `AgentRequest` (role, prompt, workspace,
context blob) and returns an `AgentResult` (stdout/stderr/exit/runtime,
plus parsed messages the orchestrator should post to Discord).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from mythos.roles import AgentDefinition, AgentRole, CliKind, ROLE_REGISTRY


@dataclass
class AgentRequest:
    role: AgentRole
    prompt: str
    workspace: Path
    context: str = ""
    timeout: float = 600.0
    extra_env: Dict[str, str] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class AgentResult:
    request_id: str
    role: AgentRole
    name: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    messages: List[str] = field(default_factory=list)  # parsed Discord-ready chunks
    error: Optional[str] = None


class AgentRunner(ABC):
    """Strategy that turns an AgentRequest into a process invocation."""

    @abstractmethod
    def run(self, request: AgentRequest, definition: AgentDefinition) -> AgentResult:
        ...


# ── Real subprocess runner ───────────────────────────────────────────────

class SubprocessRunner(AgentRunner):
    """Spawns the configured CLI in a clean working directory.

    Prompt routing per CLI:
      - Claude Code (`--print`)  : prompt on stdin
      - Codex      (`exec`)      : prompt on stdin  (codex exec reads from stdin)
      - Gemini     (`-p`)        : prompt appended to argv as `-p <prompt>`
    """

    def __init__(self, *, popen: Optional[Callable] = None):
        # popen injection point so tests can capture command lines without
        # actually spawning anything.
        self._popen = popen or subprocess.Popen

    def run(self, request: AgentRequest, definition: AgentDefinition) -> AgentResult:
        argv, stdin_blob = self._build_invocation(request, definition)
        env = self._build_env(request, definition)
        request.workspace.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        try:
            proc = self._popen(
                argv,
                cwd=str(request.workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                name=definition.name,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_s=0.0,
                error=f"CLI not found: {argv[0]}",
            )

        try:
            stdout, stderr = proc.communicate(input=stdin_blob, timeout=request.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            duration = time.monotonic() - start
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                name=definition.name,
                exit_code=124,
                stdout=stdout or "",
                stderr=stderr or "",
                duration_s=duration,
                error="timeout",
            )

        duration = time.monotonic() - start
        return AgentResult(
            request_id=request.request_id,
            role=request.role,
            name=definition.name,
            exit_code=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=duration,
            messages=_chunk_for_discord(stdout or ""),
        )

    @staticmethod
    def _build_invocation(request: AgentRequest, definition: AgentDefinition):
        argv = list(definition.argv)
        full_prompt = (
            f"You are {definition.name}, a specialized agent.\n"
            f"Role: {request.role.value}\n"
            f"Workspace: {request.workspace}\n\n"
            f"Context:\n{request.context}\n\n"
            f"Task:\n{request.prompt}\n"
        )
        if definition.cli is CliKind.GEMINI:
            argv = argv + ["-p", full_prompt]
            stdin_blob = ""
        else:
            stdin_blob = full_prompt
        return argv, stdin_blob

    @staticmethod
    def _build_env(request: AgentRequest, definition: AgentDefinition) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(definition.env)
        env.update(request.extra_env)
        env["MYTHOS_AGENT_ROLE"] = request.role.value
        env["MYTHOS_AGENT_NAME"] = definition.name
        env["MYTHOS_REQUEST_ID"] = request.request_id
        return env


# ── Test fake ────────────────────────────────────────────────────────────

class ScriptedRunner(AgentRunner):
    """Returns canned outputs for each role. Used by integration tests.

    Scripts is a dict ``{AgentRole: List[str-or-callable]}``. Each invocation
    pops the next entry. A callable receives the AgentRequest and returns the
    string output.
    """

    def __init__(self, scripts: Dict[AgentRole, List]):
        self._scripts: Dict[AgentRole, List] = {k: list(v) for k, v in scripts.items()}
        self.calls: List[AgentRequest] = []

    def run(self, request: AgentRequest, definition: AgentDefinition) -> AgentResult:
        self.calls.append(request)
        queue = self._scripts.get(request.role, [])
        if not queue:
            output = f"[{definition.name}] (no scripted reply)"
        else:
            entry = queue.pop(0)
            output = entry(request) if callable(entry) else entry
        return AgentResult(
            request_id=request.request_id,
            role=request.role,
            name=definition.name,
            exit_code=0,
            stdout=output,
            stderr="",
            duration_s=0.001,
            messages=_chunk_for_discord(output),
        )


# ── Helpers ──────────────────────────────────────────────────────────────

DISCORD_LIMIT = 1900  # leave headroom under Discord's 2000 char message cap


def _chunk_for_discord(text: str, limit: int = DISCORD_LIMIT) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # Try to break on the last newline before the limit.
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def shell_preview(definition: AgentDefinition, prompt: str) -> str:
    """Render the exact shell command an operator can run by hand to debug."""
    argv = list(definition.argv)
    if definition.cli is CliKind.GEMINI:
        argv += ["-p", prompt]
    return " ".join(shlex.quote(p) for p in argv)


def default_runner_for(definitions: Dict[AgentRole, AgentDefinition]) -> SubprocessRunner:
    return SubprocessRunner()
