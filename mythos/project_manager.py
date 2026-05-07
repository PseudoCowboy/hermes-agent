"""Project manager: project lifecycle + isolation invariants.

Owns the slug → ProjectState map. Creates Discord channels, allocates
workspace, persists the index. Enforces:

  * One project per slug (slug is unique).
  * Each project has its own Discord category and child channels.
  * No two projects share a workspace dir.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from mythos.config import MythosConfig
from mythos.discord_io import DiscordIO
from mythos.roles import Role
from mythos.slug import make_slug
from mythos.state import ProjectPhase, ProjectState
from mythos.workspace import Workspace


# Channels created for a project. The general channel is created on
# project init; the specialist channels are created at decomposition
# time.
GENERAL_CHANNEL = "general"
FRONTEND_CHANNEL = "frontend"
BACKEND_CHANNEL = "backend"
TEST_CHANNEL = "test"


@dataclass
class ProjectRecord:
    """A project = state + filesystem workspace."""

    state: ProjectState
    workspace: Workspace


class ProjectManager:
    def __init__(self, config: MythosConfig, discord: DiscordIO):
        self.config = config
        self.discord = discord
        self._projects: Dict[str, ProjectRecord] = {}
        self._channel_to_slug: Dict[int, str] = {}
        self._lock = asyncio.Lock()

    # --- Lookup helpers -----------------------------------------------------

    def list_projects(self) -> List[ProjectRecord]:
        return list(self._projects.values())

    def get(self, slug: str) -> Optional[ProjectRecord]:
        return self._projects.get(slug)

    def project_for_channel(self, channel_id: int) -> Optional[ProjectRecord]:
        slug = self._channel_to_slug.get(channel_id)
        return self._projects.get(slug) if slug else None

    def role_for_channel(self, channel_id: int) -> Optional[Role]:
        rec = self.project_for_channel(channel_id)
        if rec is None:
            return None
        return rec.state.channel_role.get(channel_id)

    # --- Lifecycle ----------------------------------------------------------

    async def create_project(
        self,
        intake_text: str,
        user_id: int,
    ) -> ProjectRecord:
        """Create a new project: slug + workspace + Discord channel group.

        Only the project ``general`` channel + category are created here;
        specialist channels are created later by ``open_specialist_channels``.
        """
        async with self._lock:
            slug = make_slug(intake_text)
            # Guarantee uniqueness (extremely unlikely collision; keep loud)
            n = 1
            base = slug
            while slug in self._projects:
                n += 1
                slug = f"{base}-{n}"

            workspace = Workspace.create(self.config.workspace_root, slug)
            state = ProjectState(
                slug=slug, intake_text=intake_text, user_id=user_id
            )

            # Create Discord category + general channel.
            category_name = f"{self.config.category_prefix}-{slug}"
            cat_id = await self.discord.create_category(
                self.config.discord_guild_id, category_name
            )
            general_id = await self.discord.create_text_channel(
                self.config.discord_guild_id,
                cat_id,
                f"{slug}-{self.config.general_suffix}",
            )

            state.category_id = cat_id
            state.channels[GENERAL_CHANNEL] = general_id
            # The general channel is where Athena and Prometheus operate;
            # bind it to Athena (the orchestrator) for routing decisions.
            state.channel_role[general_id] = Role.ATHENA

            rec = ProjectRecord(state=state, workspace=workspace)
            self._projects[slug] = rec
            self._channel_to_slug[general_id] = slug

            self._write_index()
            workspace.write_state(state.to_dict())
            return rec

    async def open_specialist_channels(self, slug: str) -> Dict[str, int]:
        """Create frontend / backend / test channels for an approved project."""
        rec = self._projects[slug]
        state = rec.state
        if state.category_id is None:
            raise RuntimeError(f"project {slug} has no category yet")

        bindings = [
            (FRONTEND_CHANNEL, self.config.frontend_suffix, Role.APOLLO),
            (BACKEND_CHANNEL, self.config.backend_suffix, Role.ATLAS),
            (TEST_CHANNEL, self.config.test_suffix, Role.HEPHAESTUS),
        ]
        for slot, suffix, role in bindings:
            if slot in state.channels:
                continue
            ch_id = await self.discord.create_text_channel(
                self.config.discord_guild_id,
                state.category_id,
                f"{slug}-{suffix}",
            )
            state.channels[slot] = ch_id
            state.channel_role[ch_id] = role
            self._channel_to_slug[ch_id] = slug

        rec.workspace.write_state(state.to_dict())
        self._write_index()
        return dict(state.channels)

    def update_phase(self, slug: str, phase: ProjectPhase) -> None:
        rec = self._projects[slug]
        rec.state.phase = phase
        rec.workspace.write_state(rec.state.to_dict())

    # --- Persistence --------------------------------------------------------

    def _write_index(self) -> None:
        idx_path = self.config.index_path
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            slug: {
                "category_id": rec.state.category_id,
                "workspace": str(rec.workspace.root),
                "phase": rec.state.phase.value,
                "channels": dict(rec.state.channels),
            }
            for slug, rec in self._projects.items()
        }
        idx_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
