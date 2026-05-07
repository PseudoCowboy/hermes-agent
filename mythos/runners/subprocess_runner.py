"""SubprocessRunner: invoke the configured CLI for each role.

Driven by ``MythosConfig`` — the per-provider command, env, and timeout come
from there. The runner takes care of:

- Picking the right provider for the role (Claude/Codex/Gemini).
- Setting the cwd to the agent's assigned workspace.
- Merging the orchestrator's per-call env over the provider defaults.
- Streaming stdout/stderr back as a single string (truncated to a hard cap).

It deliberately does NOT parse the agent's output beyond returning it — the
orchestrator's prompt is what asks the agent to use a small marker grammar
(see ``mythos.protocol``).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from mythos.config import MythosConfig, ROLE_PROVIDERS
from .base import Runner, RunResult


_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB cap to avoid runaway agent outputs.


class SubprocessRunner:
    def __init__(self, config: MythosConfig) -> None:
        self.config = config

    async def run(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> RunResult:
        provider_name = ROLE_PROVIDERS[role]
        provider = self.config.providers[provider_name]
        env = {**os.environ, **provider.env, **(extra_env or {})}

        cmd: List[str] = list(provider.command)
        stdin_payload: Optional[bytes] = None
        if provider.prompt_as_argument:
            if provider.prompt_arg_flag:
                cmd.extend([provider.prompt_arg_flag, prompt])
            else:
                cmd.append(prompt)
        else:
            stdin_payload = prompt.encode("utf-8")

        workspace.mkdir(parents=True, exist_ok=True)
        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=env,
            )
        except FileNotFoundError as exc:
            return RunResult(
                role=role,
                output="",
                exit_code=127,
                duration_seconds=time.time() - start,
                error=f"CLI not installed: {exc}",
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_payload),
                timeout=provider.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(
                role=role,
                output="",
                exit_code=124,
                duration_seconds=time.time() - start,
                error=f"timed out after {provider.timeout_seconds}s",
            )

        out = stdout or b""
        err = stderr or b""
        truncated = False
        if len(out) > _MAX_OUTPUT_BYTES:
            out = out[:_MAX_OUTPUT_BYTES]
            truncated = True
        text = out.decode("utf-8", errors="replace")
        if proc.returncode and proc.returncode != 0:
            err_text = err.decode("utf-8", errors="replace")[:4096]
            return RunResult(
                role=role,
                output=text,
                exit_code=proc.returncode or 1,
                duration_seconds=time.time() - start,
                truncated=truncated,
                error=err_text,
            )

        return RunResult(
            role=role,
            output=text,
            exit_code=0,
            duration_seconds=time.time() - start,
            truncated=truncated,
        )
