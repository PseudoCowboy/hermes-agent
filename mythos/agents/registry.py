"""Agent registry: dispatches an invocation to the right runner with the
right prompt assembled.

Logs every dispatch with role, project_id, channel_id, and runner name -
never with credential values. (This satisfies the ``agent-runtime``
``Credential handling`` requirement.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from mythos.agents.prompts import SYSTEM_PROMPTS
from mythos.config import AgentSpec, MythosConfig
from mythos.runners.base import AgentRunner, RunResult

logger = logging.getLogger(__name__)


@dataclass
class AgentInvocation:
    """Everything needed to dispatch an agent."""

    project_id: str
    channel_id: str
    channel_role: str  # "main" | "planning" | "frontend" | "backend" | "test"
    agent: AgentSpec
    workspace_path: str
    context_bundle: str  # ready-to-prepend channel context
    extra_directives: str = ""  # optional per-call directive (e.g. "Decompose now.")
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InvocationResult:
    invocation: AgentInvocation
    run: RunResult


class AgentRegistry:
    """Holds the per-role runner mapping."""

    def __init__(self, cfg: MythosConfig, runners: Dict[str, AgentRunner]):
        self._cfg = cfg
        self._runners = runners

    @property
    def config(self) -> MythosConfig:
        return self._cfg

    def runner_for(self, agent_name: str) -> AgentRunner:
        spec = self._cfg.agents[agent_name]
        return self._runners[spec.runner]

    def dispatch(self, invocation: AgentInvocation, *, timeout_seconds: Optional[int] = None) -> InvocationResult:
        agent = invocation.agent
        prompt = self._assemble_prompt(invocation)
        runner = self._runners[agent.runner]
        timeout = timeout_seconds if timeout_seconds is not None else self._cfg.timeout_seconds

        logger.info(
            "mythos.dispatch project_id=%s channel_id=%s role=%s agent=%s runner=%s",
            invocation.project_id,
            invocation.channel_id,
            agent.role,
            agent.name,
            agent.runner,
        )

        meta = {
            "role": agent.role,
            "agent": agent.name,
            "project_id": invocation.project_id,
            "channel_id": invocation.channel_id,
            "channel_role": invocation.channel_role,
            **invocation.metadata,
        }
        result = runner.run(
            prompt,
            cwd=invocation.workspace_path,
            extra_env=None,
            timeout_seconds=timeout,
            metadata=meta,
        )
        return InvocationResult(invocation=invocation, run=result)

    # ------------------------------------------------------------------ prompt

    def _assemble_prompt(self, invocation: AgentInvocation) -> str:
        agent = invocation.agent
        sys_prompt = SYSTEM_PROMPTS.get(agent.role, "You are an AI agent.")
        sections = [
            f"# System Prompt ({agent.display_name} — role={agent.role})",
            sys_prompt.strip(),
            "",
            f"# Project context",
            f"project_id: {invocation.project_id}",
            f"channel_role: {invocation.channel_role}",
            f"workspace: {invocation.workspace_path}",
            "",
            "# Channel context (most recent last):",
            invocation.context_bundle.strip() or "(empty)",
        ]
        if invocation.extra_directives:
            sections.extend(["", "# Directive for this turn:", invocation.extra_directives.strip()])
        return "\n".join(sections) + "\n"
