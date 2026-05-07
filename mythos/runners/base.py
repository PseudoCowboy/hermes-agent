"""Base Runner protocol: drive a sub-agent, get its text output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Protocol


@dataclass
class RunResult:
    role: str
    output: str
    exit_code: int
    duration_seconds: float
    truncated: bool = False
    error: Optional[str] = None


class Runner(Protocol):
    async def run(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> RunResult: ...
