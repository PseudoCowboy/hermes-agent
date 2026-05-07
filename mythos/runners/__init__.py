"""Agent runner abstraction.

A runner is an adapter around a CLI agent. It accepts an `AgentPromptPacket`
(see architecture spec section 9), invokes the configured CLI as a
subprocess with role-scoped env vars, captures stdout/stderr to log files,
and returns an `AgentRunResult`.

Two implementations:
  - SubprocessRunner: real CLI invocation (Claude Code / Codex / Gemini).
  - FakeRunner: deterministic in-memory responses for tests.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol


@dataclass
class AgentPromptPacket:
    """Structured packet handed to every agent run (architecture §9)."""
    project_id: str
    project_title: str
    role: str                            # AgentRole value
    phase: str                           # ProjectPhase value
    target_channel_id: str
    workspace_path: str
    allowed_questions_channel_id: str
    approved_design_artifact: Optional[str] = None  # text or path
    task_id: Optional[str] = None
    task_brief: Optional[str] = None
    request_text: str = ""
    extra_context: str = ""
    prior_messages: List[Dict[str, str]] = field(default_factory=list)

    def render_prompt(self) -> str:
        """Render packet to a single textual prompt for the CLI agent."""
        lines: List[str] = []
        lines.append(f"# Mythos agent run")
        lines.append(f"Project: {self.project_title} (id={self.project_id})")
        lines.append(f"Role: {self.role}")
        lines.append(f"Phase: {self.phase}")
        lines.append(f"Workspace: {self.workspace_path}")
        lines.append(
            f"Channel discipline: only post questions in channel "
            f"{self.allowed_questions_channel_id}."
        )
        lines.append("")
        lines.append("## Original user request")
        lines.append(self.request_text or "(no request text)")
        if self.approved_design_artifact:
            lines.append("")
            lines.append("## Approved design")
            lines.append(self.approved_design_artifact)
        if self.task_brief:
            lines.append("")
            lines.append(f"## Task brief (task_id={self.task_id})")
            lines.append(self.task_brief)
        if self.prior_messages:
            lines.append("")
            lines.append("## Prior channel context")
            for msg in self.prior_messages[-20:]:
                author = msg.get("author", "?")
                text = msg.get("text", "")
                lines.append(f"- {author}: {text}")
        if self.extra_context:
            lines.append("")
            lines.append("## Additional context")
            lines.append(self.extra_context)
        lines.append("")
        lines.append("## Output contract")
        lines.append(
            "Reply with a concise message suitable for posting in the assigned "
            "Discord channel. If clarifying questions are needed, ask them; "
            "otherwise produce the deliverable for this phase."
        )
        return "\n".join(lines)


@dataclass
class AgentRunResult:
    role: str
    provider: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    started_at: float
    ended_at: float
    stdout_path: str = ""
    stderr_path: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class AgentRunner(Protocol):
    provider: str

    def run(
        self,
        packet: AgentPromptPacket,
        command: List[str],
        env: Dict[str, str],
        log_dir: Path,
        timeout_seconds: int = 1200,
    ) -> AgentRunResult: ...


# ---------------------------------------------------------------------------
# Subprocess implementations
# ---------------------------------------------------------------------------


def _scoped_env(base_env: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    """Build a scoped env: real os.environ + agent-specific overrides.

    We deliberately keep PATH so the CLI is discoverable, but the role-
    specific credentials in `extra` win.
    """
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (extra or {}).items()})
    env.update({k: str(v) for k, v in (base_env or {}).items()})
    return env


def _write_logs(log_dir: Path, run_id: str, stdout: str, stderr: str) -> tuple[str, str]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{run_id}.stdout.log"
    stderr_path = log_dir / f"{run_id}.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    return str(stdout_path), str(stderr_path)


class ClaudeCodeRunner:
    """Runner for `claude --print` style invocations.

    The Claude Code CLI reads the prompt from stdin when --print is set.
    """
    provider = "claude"

    def run(
        self,
        packet: AgentPromptPacket,
        command: List[str],
        env: Dict[str, str],
        log_dir: Path,
        timeout_seconds: int = 1200,
    ) -> AgentRunResult:
        prompt = packet.render_prompt()
        run_env = _scoped_env(env, {})
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                env=run_env,
                cwd=packet.workspace_path,
                timeout=timeout_seconds,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (
                (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                + f"\n[mythos] timed out after {timeout_seconds}s"
            )
            code = 124
        ended = time.time()
        run_id = f"{packet.role}-{int(started)}"
        stdout_path, stderr_path = _write_logs(log_dir, run_id, stdout, stderr)
        return AgentRunResult(
            role=packet.role, provider=self.provider,
            command=" ".join(shlex.quote(x) for x in command),
            stdout=stdout, stderr=stderr, exit_code=code,
            duration_seconds=ended - started, started_at=started, ended_at=ended,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )


class CodexRunner:
    """Runner for `codex exec` style invocations.

    Codex exec accepts the prompt as a positional argument after the flags.
    """
    provider = "codex"

    def run(
        self,
        packet: AgentPromptPacket,
        command: List[str],
        env: Dict[str, str],
        log_dir: Path,
        timeout_seconds: int = 1200,
    ) -> AgentRunResult:
        prompt = packet.render_prompt()
        full_cmd = list(command) + [prompt]
        run_env = _scoped_env(env, {})
        started = time.time()
        try:
            proc = subprocess.run(
                full_cmd, capture_output=True, text=True, env=run_env,
                cwd=packet.workspace_path, timeout=timeout_seconds,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (
                (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                + f"\n[mythos] timed out after {timeout_seconds}s"
            )
            code = 124
        ended = time.time()
        run_id = f"{packet.role}-{int(started)}"
        stdout_path, stderr_path = _write_logs(log_dir, run_id, stdout, stderr)
        return AgentRunResult(
            role=packet.role, provider=self.provider,
            command=" ".join(shlex.quote(x) for x in full_cmd),
            stdout=stdout, stderr=stderr, exit_code=code,
            duration_seconds=ended - started, started_at=started, ended_at=ended,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )


class GeminiRunner:
    """Runner for `gemini -p <prompt> -m <model> -y` invocations.

    Gemini CLI takes the prompt via -p; uses its own preconfigured creds.
    """
    provider = "gemini"

    def run(
        self,
        packet: AgentPromptPacket,
        command: List[str],
        env: Dict[str, str],
        log_dir: Path,
        timeout_seconds: int = 1200,
    ) -> AgentRunResult:
        prompt = packet.render_prompt()
        full_cmd = list(command) + ["-p", prompt]
        run_env = _scoped_env(env, {})
        started = time.time()
        try:
            proc = subprocess.run(
                full_cmd, capture_output=True, text=True, env=run_env,
                cwd=packet.workspace_path, timeout=timeout_seconds,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (
                (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                + f"\n[mythos] timed out after {timeout_seconds}s"
            )
            code = 124
        ended = time.time()
        run_id = f"{packet.role}-{int(started)}"
        stdout_path, stderr_path = _write_logs(log_dir, run_id, stdout, stderr)
        return AgentRunResult(
            role=packet.role, provider=self.provider,
            command=" ".join(shlex.quote(x) for x in full_cmd),
            stdout=stdout, stderr=stderr, exit_code=code,
            duration_seconds=ended - started, started_at=started, ended_at=ended,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )


PROVIDER_RUNNERS: Dict[str, AgentRunner] = {
    "claude": ClaudeCodeRunner(),
    "codex": CodexRunner(),
    "gemini": GeminiRunner(),
}


# ---------------------------------------------------------------------------
# Fake runner for tests
# ---------------------------------------------------------------------------


FakeResponder = Callable[[AgentPromptPacket], str]


class FakeRunner:
    """Deterministic test runner: a callable per role returning stdout text."""

    provider = "fake"

    def __init__(self, responders: Optional[Dict[str, FakeResponder]] = None):
        self.responders: Dict[str, FakeResponder] = dict(responders or {})
        self.calls: List[AgentPromptPacket] = []

    def register(self, role: str, responder: FakeResponder) -> None:
        self.responders[role] = responder

    def run(
        self,
        packet: AgentPromptPacket,
        command: List[str],
        env: Dict[str, str],
        log_dir: Path,
        timeout_seconds: int = 1200,
    ) -> AgentRunResult:
        self.calls.append(packet)
        responder = self.responders.get(packet.role)
        if responder is None:
            stdout = f"[fake-{packet.role}] no responder configured"
            code = 0
        else:
            try:
                stdout = responder(packet)
                code = 0
            except Exception as exc:
                stdout = f"[fake-{packet.role}] error: {exc}"
                code = 1
        started = time.time()
        ended = started + 0.001
        stdout_path = ""
        stderr_path = ""
        if log_dir is not None:
            try:
                stdout_path, stderr_path = _write_logs(
                    log_dir, f"{packet.role}-{int(started)}", stdout, ""
                )
            except OSError:
                pass
        return AgentRunResult(
            role=packet.role, provider=self.provider,
            command=" ".join(shlex.quote(x) for x in command),
            stdout=stdout, stderr="", exit_code=code,
            duration_seconds=ended - started, started_at=started, ended_at=ended,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )
