"""Async wrapper that runs an external coding-agent CLI as a subprocess.

Exposes a single coroutine: `run_cli(cli, prompt, cwd) -> CLIResult`.
Mockable: tests substitute a fake runner to avoid real subprocess calls.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import AgentCLIConfig


@dataclass
class CLIResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    command: str

    def display(self, max_chars: int = 1500) -> str:
        out = (self.stdout or "").strip()
        if len(out) > max_chars:
            out = out[:max_chars] + "\n…[truncated]"
        if not self.ok:
            err = (self.stderr or "").strip()[-500:]
            return f"[exit {self.returncode}] {out}\n--stderr--\n{err}"
        return out


# Type for the runner function — lets tests inject a fake.
Runner = Callable[[AgentCLIConfig, str, Path], Awaitable[CLIResult]]


async def run_cli(cli: AgentCLIConfig, prompt: str, cwd: Path) -> CLIResult:
    """Default runner: actually invokes the configured CLI as a subprocess."""
    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(cli.env)

    cmd = list(cli.command)
    stdin_payload: Optional[bytes] = None
    if cli.prompt_via.startswith("flag:"):
        flag = cli.prompt_via.split(":", 1)[1]
        cmd += [flag, prompt]
    else:
        stdin_payload = prompt.encode("utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
    except FileNotFoundError as e:
        return CLIResult(
            ok=False, stdout="", stderr=f"command not found: {e}",
            returncode=127, command=" ".join(shlex.quote(c) for c in cmd),
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_payload), timeout=cli.timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CLIResult(
            ok=False, stdout="", stderr=f"timeout after {cli.timeout_seconds}s",
            returncode=-1, command=" ".join(shlex.quote(c) for c in cmd),
        )

    return CLIResult(
        ok=(proc.returncode == 0),
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        returncode=proc.returncode or 0,
        command=" ".join(shlex.quote(c) for c in cmd),
    )
