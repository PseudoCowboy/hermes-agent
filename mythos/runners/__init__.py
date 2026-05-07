"""Runners: thin adapters over CLI subprocesses (and an in-memory fake).

Every runner exposes a single ``run(prompt, context, cwd) -> RunResult``.
Subprocesses are spawned fresh per call (no long-lived daemons), with
``cwd`` set to the project's workspace and the role-appropriate environment.

Tests inject ``FakeRunner`` to bypass subprocess execution.
"""

from .base import AgentRunner, RunResult, RunnerError
from .subprocess_runner import SubprocessRunner, build_runner_from_spec
from .fake import FakeRunner

__all__ = [
    "AgentRunner",
    "RunResult",
    "RunnerError",
    "SubprocessRunner",
    "FakeRunner",
    "build_runner_from_spec",
]
