"""Message router: turns Discord messages into agent invocations.

Responsibilities:

* Detect ``@AgentName`` mentions of registered agents.
* Look up the project owning the channel via ``ProjectStore``.
* Refuse cross-project dispatch (``project-isolation`` ``Channel-to-project guard``).
* Refuse out-of-channel specialist invocations (``implementation-agents``
  ``Channel-scoped activation``).
* Build the per-channel context bundle the agent should see.
* Track the per-project handoff streak and pause when it exceeds
  ``MythosConfig.max_handoff_streak``.
* Hand off to the orchestrator for state transitions; the orchestrator
  in turn calls the registry to dispatch and posts the runner's output
  back into the channel.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from mythos.agents.registry import AgentRegistry
from mythos.config import AgentSpec, MythosConfig
from mythos.discord_adapter import DiscordAdapter, DiscordMessage
from mythos.projects import Project, ProjectStatus, ProjectStore

logger = logging.getLogger(__name__)


@dataclass
class RoutedMention:
    project: Project
    channel_role: str
    agent: AgentSpec
    triggering_message: DiscordMessage


class MessageRouter:
    def __init__(
        self,
        *,
        config: MythosConfig,
        store: ProjectStore,
        registry: AgentRegistry,
        discord: DiscordAdapter,
    ):
        self._cfg = config
        self._store = store
        self._registry = registry
        self._discord = discord
        self._lock = threading.RLock()
        # display-name (lowercased) -> AgentSpec
        self._mention_index: Dict[str, AgentSpec] = {}
        for agent in config.agents.values():
            self._mention_index[agent.display_name.lower()] = agent
            self._mention_index[agent.name.lower()] = agent

    # ------------------------------------------------------------------ public

    def find_mentioned_agents(self, message: DiscordMessage) -> List[AgentSpec]:
        seen: List[AgentSpec] = []
        for mention in message.mentions:
            spec = self._mention_index.get(mention.lower())
            if spec and spec not in seen:
                seen.append(spec)
        return seen

    def is_user_mentioned(self, message: DiscordMessage) -> bool:
        return any(m.lower() == "user" for m in message.mentions)

    def channel_role_for(self, project: Project, channel_id: str) -> Optional[str]:
        for role, cid in project.sub_channel_ids.items():
            if cid == channel_id:
                return role
        return None

    def resolve_dispatch(
        self, message: DiscordMessage, agent: AgentSpec
    ) -> Optional[RoutedMention]:
        """Validate routing constraints; return a RoutedMention or None.

        Returns None when the dispatch should be silently dropped (e.g.
        agent mentioned outside its assigned channel).
        """
        if message.channel_id == self._cfg.main_channel_id:
            # Main channel mentions of specialists are ignored; specialists
            # are channel-scoped. The orchestrator handles main-channel
            # intake separately.
            if agent.role != "main":
                logger.info(
                    "router: ignoring main-channel mention of role=%s agent=%s",
                    agent.role,
                    agent.name,
                )
                return None

        project = self._store.get_by_channel(message.channel_id)
        if not project:
            logger.info(
                "router: no project owns channel_id=%s; ignoring mention of %s",
                message.channel_id,
                agent.name,
            )
            return None

        if project.status == ProjectStatus.ARCHIVED:
            logger.info("router: project %s is archived; ignoring", project.id)
            return None

        channel_role = self.channel_role_for(project, message.channel_id)
        if not channel_role:
            logger.warning(
                "router: project=%s has no role mapping for channel_id=%s",
                project.id,
                message.channel_id,
            )
            return None

        # Channel-scoped activation: implementation agents only on their channel.
        if agent.channel != "main" and agent.channel != channel_role:
            logger.info(
                "router: agent=%s scoped to channel=%s but mention was on channel=%s; dropping",
                agent.name,
                agent.channel,
                channel_role,
            )
            return None

        # Handoff loop protection.
        if message.is_agent and project.handoff_streak >= self._cfg.max_handoff_streak:
            logger.warning(
                "router: project=%s exceeded handoff streak %s; pausing dispatch",
                project.id,
                self._cfg.max_handoff_streak,
            )
            return None

        return RoutedMention(
            project=project,
            channel_role=channel_role,
            agent=agent,
            triggering_message=message,
        )

    def build_context_bundle(self, project: Project, channel_id: str, *, limit: int = 30) -> str:
        history = self._discord.channel_history(channel_id=channel_id, limit=limit)
        lines: List[str] = []
        for msg in history:
            speaker = msg.author_display_name or ("agent" if msg.is_agent else "user")
            lines.append(f"[{speaker}] {msg.content}")
        return "\n".join(lines)

    def note_handoff(self, project: Project, *, by_agent: bool) -> None:
        if by_agent:
            project.handoff_streak += 1
        else:
            project.handoff_streak = 0
        self._store.update(project)

    def reset_handoff_streak(self, project: Project) -> None:
        project.handoff_streak = 0
        self._store.update(project)
