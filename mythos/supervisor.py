"""Agent supervisor: tracks per-(project, role) Agent instances."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from mythos.agents import Agent, AgentInput, AgentOutput, CliAgent
from mythos.config import MythosConfig
from mythos.roles import Role, RoleBinding


AgentFactory = Callable[[Role, RoleBinding], Agent]


def default_factory(role: Role, binding: RoleBinding) -> Agent:
    """Default: build a CliAgent that drives the configured CLI."""
    return CliAgent(binding)


@dataclass
class Supervisor:
    """Owns the lifecycle of agent instances.

    For now this is a per-process cache: there is no separate child
    process between invocations — each ``invoke()`` spawns the CLI and
    returns. The cache exists so a stub or stateful adapter can persist
    in-memory state across turns of the same conversation.
    """

    config: MythosConfig
    factory: AgentFactory = default_factory
    _instances: Dict[Tuple[str, Role], Agent] = field(default_factory=dict)
    _semaphore: Optional[asyncio.Semaphore] = None

    def get(self, project_slug: str, role: Role) -> Agent:
        key = (project_slug, role)
        if key not in self._instances:
            binding = self.config.role_bindings[role]
            self._instances[key] = self.factory(role, binding)
        return self._instances[key]

    def install(self, project_slug: str, role: Role, agent: Agent) -> None:
        """Test seam: replace the agent for (slug, role) with a stub."""
        self._instances[(project_slug, role)] = agent

    async def invoke(
        self,
        project_slug: str,
        role: Role,
        request: AgentInput,
    ) -> AgentOutput:
        if self._semaphore is None:
            # Lazy init so the semaphore binds to the running loop.
            self._semaphore = asyncio.Semaphore(
                self.config.max_concurrent_specialists
            )
        async with self._semaphore:
            agent = self.get(project_slug, role)
            return await agent.invoke(request)

    def drop_project(self, project_slug: str) -> None:
        """Forget all agents associated with this project (post-archive)."""
        for key in [k for k in self._instances if k[0] == project_slug]:
            del self._instances[key]
