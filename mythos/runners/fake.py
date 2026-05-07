"""In-memory runner used by integration tests.

The test wires up role-specific scripted responses; the orchestrator can
then exercise the full workflow without spawning real CLI subprocesses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mythos.runners.base import AgentRunner, RunResult


# A response handler returns a string given (prompt, cwd, metadata).
ResponseFn = Callable[[str, str, Dict[str, Any]], str]


@dataclass
class FakeRunnerCall:
    prompt: str
    cwd: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    extra_env: Dict[str, str] = field(default_factory=dict)


class FakeRunner:
    """A configurable, deterministic stand-in for ``SubprocessRunner``.

    ``responses`` may be:
    * a list of strings (consumed FIFO),
    * a callable ``(prompt, cwd, metadata) -> str``,
    * or a dict keyed by ``metadata['role']`` mapping to lists or callables.
    """

    def __init__(self, name: str, responses: Any = None):
        self.name = name
        self._responses = responses if responses is not None else []
        self._lock = threading.Lock()
        self.calls: List[FakeRunnerCall] = []

    def run(
        self,
        prompt: str,
        *,
        cwd: str,
        extra_env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        meta = dict(metadata or {})
        with self._lock:
            self.calls.append(FakeRunnerCall(prompt=prompt, cwd=cwd, metadata=meta, extra_env=dict(extra_env or {})))
            stdout = self._next_response(meta, prompt, cwd)
        return RunResult(stdout=stdout, returncode=0, runner=self.name)

    def _next_response(self, meta: Dict[str, Any], prompt: str, cwd: str) -> str:
        responses = self._responses
        if callable(responses):
            return responses(prompt, cwd, meta)
        if isinstance(responses, dict):
            role = meta.get("role", "")
            bucket = responses.get(role)
            if bucket is None:
                return f"[fake:{self.name}] no scripted response for role={role}"
            if callable(bucket):
                return bucket(prompt, cwd, meta)
            if isinstance(bucket, list) and bucket:
                return bucket.pop(0)
            return f"[fake:{self.name}] no remaining responses for role={role}"
        if isinstance(responses, list):
            if responses:
                return responses.pop(0)
            return f"[fake:{self.name}] no remaining responses"
        return str(responses)
