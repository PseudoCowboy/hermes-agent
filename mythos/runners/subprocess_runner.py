"""Subprocess-based runner: spawns a fresh CLI per invocation."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any, Dict, Optional

from mythos.config import RunnerSpec
from mythos.runners.base import AgentRunner, RunResult, RunnerError

logger = logging.getLogger(__name__)


class SubprocessRunner:
    """Runs a CLI as a one-shot subprocess.

    Honours the role's command/env from ``RunnerSpec``; lets callers add
    extra env (typically nothing) and override timeout. Captures stdout
    and stderr.

    Prompt delivery:

    * If ``spec.prompt_via_argv`` is True (Gemini), the prompt is passed
      as ``-p <prompt>``.
    * Otherwise (Claude Code, Codex), the prompt goes via stdin.
    """

    def __init__(self, spec: RunnerSpec):
        self.spec = spec
        self.name = spec.name

    def run(
        self,
        prompt: str,
        *,
        cwd: str,
        extra_env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        env = {**os.environ, **self.spec.env}
        if extra_env:
            env.update(extra_env)

        argv = list(self.spec.command)
        stdin_payload: Optional[str] = None
        if self.spec.prompt_via_argv:
            argv.extend([self.spec.prompt_argv_flag, prompt])
        else:
            stdin_payload = prompt

        # Don't log the prompt body or env values; keep credential surface
        # to keys only as the agent-runtime spec requires.
        logger.info(
            "mythos.runner spawn name=%s cwd=%s argv0=%s env_keys=%s",
            self.name,
            cwd,
            argv[0],
            sorted(self.spec.env.keys()),
        )

        started = time.monotonic()
        timed_out = False
        returncode = -1
        stdout = ""
        stderr = ""

        try:
            proc = subprocess.run(  # noqa: S603 - command is operator-controlled
                argv,
                input=stdin_payload,
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
                timeout=timeout_seconds,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            returncode = -1
        except FileNotFoundError as exc:
            raise RunnerError(f"runner {self.name} command not found: {argv[0]}") from exc

        duration = time.monotonic() - started
        return RunResult(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            runner=self.name,
            duration_seconds=duration,
            timed_out=timed_out,
        )


def build_runner_from_spec(spec: RunnerSpec) -> AgentRunner:
    return SubprocessRunner(spec)
