"""ScriptedRunner: deterministic Runner used by tests.

You hand it a sequence of replies per role; each call to ``run`` consumes the
next reply for that role. Useful for asserting orchestrator behavior without
spawning real CLIs.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Union

from .base import Runner, RunResult


# A response can be a literal string or a callable that takes the prompt.
ScriptedResponse = Union[str, Callable[[str, Path], str]]


class ScriptedRunner:
    def __init__(self) -> None:
        self._queues: Dict[str, Deque[ScriptedResponse]] = defaultdict(deque)
        self.calls: List[Dict[str, object]] = []

    def queue(self, role: str, *responses: ScriptedResponse) -> None:
        for r in responses:
            self._queues[role].append(r)

    def queued_count(self, role: str) -> int:
        return len(self._queues[role])

    async def run(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> RunResult:
        start = time.time()
        if not self._queues[role]:
            return RunResult(
                role=role,
                output=f"(no scripted reply for {role})",
                exit_code=0,
                duration_seconds=time.time() - start,
            )
        nxt = self._queues[role].popleft()
        if callable(nxt):
            text = nxt(prompt, workspace)
        else:
            text = nxt
        self.calls.append(
            {
                "role": role,
                "prompt": prompt,
                "workspace": str(workspace),
                "extra_env": dict(extra_env or {}),
                "output": text,
            }
        )
        return RunResult(
            role=role,
            output=text,
            exit_code=0,
            duration_seconds=time.time() - start,
        )
