"""Agent runner abstractions.

A ``Runner`` takes a prompt + working directory + env, executes the configured
CLI, and returns the agent's text output. The orchestrator never sees the
subprocess; this layer also makes tests easy: ``ScriptedRunner`` lets tests
hand-roll responses keyed by role/turn.
"""

from .base import Runner, RunResult
from .scripted import ScriptedRunner
from .subprocess_runner import SubprocessRunner

__all__ = ["Runner", "RunResult", "ScriptedRunner", "SubprocessRunner"]
