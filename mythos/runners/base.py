"""AgentRunner interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol


class RunnerError(RuntimeError):
    """Raised on subprocess timeout / non-zero exit."""


@dataclass
class RunResult:
    stdout: str
    stderr: str = ""
    returncode: int = 0
    runner: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class AgentRunner(Protocol):
    """Uniform interface for every agent backend.

    ``prompt`` is the fully assembled prompt (system prompt + context bundle).
    ``cwd`` is the project's workspace directory; runners MUST honour it.
    ``extra_env`` is layered on top of the runner's default env.
    """

    name: str

    def run(
        self,
        prompt: str,
        *,
        cwd: str,
        extra_env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunResult: ...
