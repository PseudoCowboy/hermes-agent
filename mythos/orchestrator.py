"""Mythos orchestrator: routes Discord events through the multi-agent workflow.

Responsibilities:
  - Listen for messages in the main channel and start new projects.
  - Route messages in project channels through the drafting/review/approval loop.
  - Confine specialist agents (Apollo/Atlas/Hephaestus) to their discipline channels.
  - Maintain per-project isolation (separate channels, separate working dirs,
    separate spec/review history).

Concurrency model: per-project asyncio.Lock so we never run two workflow steps
concurrently for the same project. Different projects run in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set

from .agents import Agent, SPECIALIST_DISCIPLINE, build_roster
from .cli_runner import CLIResult, Runner, run_cli
from .config import MythosConfig
from .discord_adapter import DiscordAdapter, IncomingMessage
from .models import (
    ApprovalEvent,
    Discipline,
    DISCIPLINE_AGENT,
    Project,
    ProjectState,
    Review,
    SpecVersion,
)
from .store import Store


logger = logging.getLogger("mythos.orchestrator")


APPROVE_RE = re.compile(r"^\s*(approve|approved|lgtm|ship it|/approve)\b", re.IGNORECASE)
REJECT_RE = re.compile(r"^\s*(reject|rejected|/reject)\b", re.IGNORECASE)
REVISE_RE = re.compile(r"^\s*(revise|change|/revise|request changes)\b", re.IGNORECASE)
QUESTION_RE = re.compile(r"^\s*(QUESTION:|CLARIFY:)", re.IGNORECASE)


def _slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (s or "project")[:maxlen]


def _detect_disciplines(spec_text: str) -> List[Discipline]:
    """Heuristic: scan a draft spec and decide which disciplines are needed.

    Looks for explicit section headers first; falls back to keyword presence.
    Always includes test if either frontend or backend is present.
    """
    text = spec_text.lower()
    disciplines: List[Discipline] = []
    if any(h in text for h in ("frontend:", "## frontend", "# frontend", "frontend work", "frontend component")) or "frontend" in text:
        disciplines.append(Discipline.FRONTEND)
    if any(h in text for h in ("backend:", "## backend", "# backend", "backend work", "backend component", "api endpoint", "server")) or "backend" in text:
        disciplines.append(Discipline.BACKEND)
    # Always add test if anything else is there.
    if disciplines and Discipline.TEST not in disciplines:
        disciplines.append(Discipline.TEST)
    elif "test" in text and Discipline.TEST not in disciplines:
        disciplines.append(Discipline.TEST)
    return disciplines


class MythosOrchestrator:
    def __init__(
        self,
        config: MythosConfig,
        store: Store,
        discord: DiscordAdapter,
        runner: Runner = run_cli,
    ):
        self.config = config
        self.store = store
        self.discord = discord
        self.agents = build_roster(config.agent_cli, runner=runner)
        # Per-project locks so workflow steps don't interleave for one project.
        self._project_locks: Dict[str, asyncio.Lock] = {}
        # Project channels and discipline channels we recognize.
        self._channel_to_project: Dict[int, str] = {}
        # discipline_channel_id -> (project_id, discipline)
        self._discipline_channel: Dict[int, tuple] = {}
        self._reload_indexes()

    def _reload_indexes(self) -> None:
        for p in self.store.list_projects():
            if p.project_channel_id:
                self._channel_to_project[p.project_channel_id] = p.project_id
            for disc, cid in p.discipline_channels.items():
                self._discipline_channel[int(cid)] = (p.project_id, disc)

    def _lock(self, project_id: str) -> asyncio.Lock:
        lk = self._project_locks.get(project_id)
        if lk is None:
            lk = asyncio.Lock()
            self._project_locks[project_id] = lk
        return lk

    async def start(self) -> None:
        self.discord.on_message(self.on_discord_message)
        await self.discord.start(
            self.config.discord_bot_token,
            self.config.discord_guild_id,
            role_tokens=self.config.discord_role_bot_tokens,
        )
        logger.info("mythos orchestrator started")

    async def stop(self) -> None:
        await self.discord.stop()

    async def _send_agent(self, role: str, channel_id: int, content: str) -> int:
        """Post agent-authored text through the configured role bot if any."""
        return await self.discord.send_message_as(role, channel_id, content)

    # ------------------------------------------------------------------
    # Routing entrypoint
    # ------------------------------------------------------------------

    async def on_discord_message(self, msg: IncomingMessage) -> None:
        # Ignore the bot's own messages.
        if msg.is_bot or msg.author_id == self.discord.bot_user_id:
            return

        # Main channel: new project intake.
        if msg.channel_id == self.config.main_channel_id:
            await self._handle_main_channel(msg)
            return

        # Project channel: drafting/review/approval flow.
        project_id = self._channel_to_project.get(msg.channel_id)
        if project_id:
            await self._handle_project_channel(project_id, msg)
            return

        # Discipline sub-channel: specialist agent.
        disc_info = self._discipline_channel.get(msg.channel_id)
        if disc_info:
            project_id, discipline = disc_info
            await self._handle_discipline_channel(project_id, discipline, msg)
            return

        # Otherwise: not our channel.

    # ------------------------------------------------------------------
    # Main-channel intake (User Story 1)
    # ------------------------------------------------------------------

    async def _handle_main_channel(self, msg: IncomingMessage) -> None:
        seed = msg.content.strip()
        if not seed:
            return
        project_id = Project.new_id()
        slug = _slugify(seed)
        channel_name = f"proj-{slug}-{project_id[:6]}"

        # Working dir: per-project isolated.
        working_dir = (self.config.state_dir / "projects" / project_id).resolve()
        working_dir.mkdir(parents=True, exist_ok=True)

        # Acknowledge in main.
        await self._send_agent(
            "athena",
            self.config.main_channel_id,
            f"Athena here. Got your request — opening project channel `{channel_name}` "
            f"(id `{project_id}`).",
        )

        # Create the project channel.
        try:
            channel_id = await self.discord.create_text_channel(
                name=channel_name, topic=f"Mythos project {project_id}",
            )
        except Exception as e:
            await self._send_agent(
                "athena",
                self.config.main_channel_id,
                f"Athena: failed to create project channel ({e}). Please check bot permissions.",
            )
            return

        project = Project(
            project_id=project_id,
            owner_user_id=msg.author_id,
            seed_request=seed,
            project_channel_id=channel_id,
            state=ProjectState.CLARIFYING,
            working_dir=str(working_dir),
        )
        self.store.insert_project(project)
        self._channel_to_project[channel_id] = project_id

        # Seed the project channel.
        await self._send_agent(
            "athena",
            channel_id,
            (
                f"**Project `{project_id}`**\n"
                f"Owner: <@{msg.author_id}>\n"
                f"Seed request:\n> {seed}\n\n"
                f"@Prometheus please draft the design spec. "
                f"You may ask clarifying questions in this channel."
            ),
        )

        # Kick off the drafting loop in the background.
        asyncio.create_task(self._run_drafting_round(project_id))

    # ------------------------------------------------------------------
    # Project channel: drafting / review / approval
    # ------------------------------------------------------------------

    async def _handle_project_channel(self, project_id: str, msg: IncomingMessage) -> None:
        async with self._lock(project_id):
            project = self.store.get_project(project_id)
            if project is None:
                return

            # Approval / revise / reject signals.
            if project.state == ProjectState.AWAITING_APPROVAL:
                if APPROVE_RE.match(msg.content):
                    if msg.author_id != project.owner_user_id:
                        await self._send_agent(
                            "athena",
                            project.project_channel_id,
                            f"Athena: only the project owner (<@{project.owner_user_id}>) "
                            f"can approve.",
                        )
                        return
                    spec = self.store.latest_spec(project_id)
                    self.store.record_approval(ApprovalEvent(
                        project_id=project_id,
                        spec_version=spec.version if spec else 0,
                        user_id=msg.author_id,
                    ))
                    project.state = ProjectState.DECOMPOSING
                    self.store.update_project(project)
                    await self._send_agent(
                        "athena",
                        project.project_channel_id,
                        f"Athena: ✅ approval recorded for spec v{spec.version if spec else 0}. "
                        f"Decomposing work…",
                    )
                    asyncio.create_task(self._run_decomposition(project_id))
                    return

                if REVISE_RE.match(msg.content) or REJECT_RE.match(msg.content):
                    if project.review_iteration >= self.config.max_review_iterations:
                        await self._send_agent(
                            "athena",
                            project.project_channel_id,
                            f"Athena: max revision rounds ({self.config.max_review_iterations}) "
                            f"reached. Please clarify what to change in plain English; I'll forward "
                            f"to Prometheus.",
                        )
                        return
                    project.state = ProjectState.REVISING
                    self.store.update_project(project)
                    await self._send_agent(
                        "athena",
                        project.project_channel_id,
                        f"Athena: revision requested. Forwarding feedback to Prometheus.",
                    )
                    feedback = msg.content
                    asyncio.create_task(self._run_drafting_round(project_id, revision_note=feedback))
                    return

                # Ambiguous — confirm explicit approval (FR-015 / edge case).
                await self._send_agent(
                    "athena",
                    project.project_channel_id,
                    f"Athena: I need an explicit `approve`, `revise`, or `reject` to proceed.",
                )
                return

            # During clarifying or drafting, the user may answer Prometheus's questions.
            if project.state in (ProjectState.CLARIFYING, ProjectState.DRAFTING):
                # Treat user reply as additional context and re-run drafting.
                asyncio.create_task(self._run_drafting_round(project_id, extra_context=msg.content))
                return

            # Other states: ignore (or could log).

    async def _run_drafting_round(
        self,
        project_id: str,
        revision_note: Optional[str] = None,
        extra_context: Optional[str] = None,
    ) -> None:
        async with self._lock(project_id):
            project = self.store.get_project(project_id)
            if project is None:
                return

            project.state = ProjectState.DRAFTING
            self.store.update_project(project)

            prev_spec = self.store.latest_spec(project_id)
            prev_review = self.store.latest_review(project_id)
            wd = Path(project.working_dir)

            prompt_lines = [
                f"Project ID: {project_id}",
                f"Seed request:\n{project.seed_request}",
            ]
            if extra_context:
                prompt_lines += ["\nUser added context:", extra_context]
            if prev_spec and revision_note:
                prompt_lines += [
                    "\nPrior spec (v{}):".format(prev_spec.version),
                    prev_spec.content,
                    "\nReview comments:",
                    prev_review.content if prev_review else "(none)",
                    "\nRevision request from user:",
                    revision_note,
                    "\nProduce a revised spec.",
                ]
            else:
                prompt_lines += [
                    "\nIf the request is ambiguous, output up to 3 clarifying questions, "
                    "each prefixed with 'QUESTION:'. Otherwise produce the full design spec.",
                ]
            user_prompt = "\n".join(prompt_lines)

            result = await self.agents["prometheus"].run(user_prompt, wd)
            text = result.display()

            # If output contains questions, post them and pause.
            if not result.ok:
                await self._send_agent(
                    "prometheus",
                    project.project_channel_id,
                    f"⚠️ Prometheus failed: {text[:500]}",
                )
                project.state = ProjectState.FAILED
                self.store.update_project(project)
                return

            if QUESTION_RE.search(text or ""):
                project.state = ProjectState.CLARIFYING
                self.store.update_project(project)
                await self._send_agent(
                    "prometheus",
                    project.project_channel_id,
                    f"**Prometheus** has clarifying questions:\n{text}",
                )
                return

            # Otherwise it's a draft spec.
            new_version = (prev_spec.version + 1) if prev_spec else 1
            spec = SpecVersion(project_id=project_id, version=new_version, content=text)
            self.store.insert_spec(spec)
            project.spec_version = new_version
            project.state = ProjectState.REVIEWING
            self.store.update_project(project)

            await self._send_agent(
                "prometheus",
                project.project_channel_id,
                f"**Prometheus** posted draft spec v{new_version}:\n\n{text}\n\n"
                f"@Argus please review.",
            )

        # Run review (no lock — review is read-only of spec).
        await self._run_review(project_id)

    async def _run_review(self, project_id: str) -> None:
        async with self._lock(project_id):
            project = self.store.get_project(project_id)
            if project is None:
                return
            spec = self.store.latest_spec(project_id)
            if spec is None:
                return
            wd = Path(project.working_dir)
            prompt = (
                f"Project ID: {project_id}\nSpec version: v{spec.version}\n\nSPEC:\n{spec.content}"
            )
            result = await self.agents["argus"].run(prompt, wd)
            text = result.display()
            if not result.ok:
                await self._send_agent(
                    "argus",
                    project.project_channel_id,
                    f"⚠️ Argus failed: {text[:500]}. Athena will surface to user.",
                )
                project.state = ProjectState.AWAITING_APPROVAL
                self.store.update_project(project)
                await self._post_for_approval(project)
                return

            review = Review(
                project_id=project_id, spec_version=spec.version,
                iteration=project.review_iteration + 1, content=text,
            )
            self.store.insert_review(review)
            project.review_iteration += 1
            project.state = ProjectState.AWAITING_APPROVAL
            self.store.update_project(project)

            await self._send_agent(
                "argus",
                project.project_channel_id,
                f"**Argus** review of spec v{spec.version}:\n\n{text}\n\n"
                f"Review complete. @Athena please surface to user.",
            )
            await self._post_for_approval(project)

    async def _post_for_approval(self, project: Project) -> None:
        await self._send_agent(
            "athena",
            project.project_channel_id,
            (
                f"**Athena**: @<@{project.owner_user_id}> the spec and review are ready.\n"
                f"Reply `approve` to proceed to implementation, "
                f"`revise <feedback>` to iterate, or `reject` to stop.\n"
                f"(round {project.review_iteration} of "
                f"{self.config.max_review_iterations})"
            ),
        )

    # ------------------------------------------------------------------
    # Decomposition (User Story 5) and specialist invocation (US 6)
    # ------------------------------------------------------------------

    async def _run_decomposition(self, project_id: str) -> None:
        async with self._lock(project_id):
            project = self.store.get_project(project_id)
            if project is None:
                return
            spec = self.store.latest_spec(project_id)
            if spec is None:
                return

            disciplines = _detect_disciplines(spec.content)
            if not disciplines:
                # If we can't tell, default to backend + test (cover-all).
                disciplines = [Discipline.BACKEND, Discipline.TEST]

            # Create category for this project's sub-channels (best effort).
            try:
                category_id = await self.discord.create_category(
                    name=f"mythos-{project.project_id[:6]}",
                )
            except Exception:
                category_id = None

            for disc in disciplines:
                disc_dir = Path(project.working_dir) / disc.value
                disc_dir.mkdir(parents=True, exist_ok=True)
                ch_name = f"{disc.value}-{project.project_id[:6]}"
                ch_id = await self.discord.create_text_channel(
                    name=ch_name,
                    parent_category_id=category_id,
                    topic=f"Mythos {disc.value} for project {project.project_id}",
                )
                project.discipline_channels[disc.value] = ch_id
                self._discipline_channel[ch_id] = (project.project_id, disc.value)

                agent_name = DISCIPLINE_AGENT[disc]
                await self._send_agent(
                    "athena",
                    ch_id,
                    (
                        f"**{disc.value.title()} sub-channel for project `{project.project_id}`**\n"
                        f"Approved spec v{spec.version}:\n\n{spec.content}\n\n"
                        f"@{agent_name} please implement the {disc.value} work items in this channel."
                    ),
                )

            project.state = ProjectState.IN_PROGRESS
            self.store.update_project(project)

            await self._send_agent(
                "athena",
                project.project_channel_id,
                f"**Athena**: created {len(disciplines)} sub-channel(s): "
                + ", ".join(f"`{d.value}`" for d in disciplines),
            )

        # Kick off specialists. We do not hold the project lock — they run
        # concurrently inside their own discipline lock (per-discipline-channel).
        for disc in disciplines:
            asyncio.create_task(self._run_specialist(project_id, disc))

    async def _run_specialist(self, project_id: str, discipline: Discipline) -> None:
        project = self.store.get_project(project_id)
        if project is None:
            return
        spec = self.store.latest_spec(project_id)
        if spec is None:
            return
        agent_name = DISCIPLINE_AGENT[discipline]
        agent = self.agents[agent_name]
        ch_id = project.discipline_channels.get(discipline.value)
        if not ch_id:
            return
        wd = Path(project.working_dir) / discipline.value
        wd.mkdir(parents=True, exist_ok=True)

        prompt = (
            f"Project ID: {project_id}\n"
            f"Discipline: {discipline.value}\n"
            f"Approved spec v{spec.version}:\n\n{spec.content}\n\n"
            f"Implement the {discipline.value} work for this project. "
            f"Working directory: {wd}.\n"
            f"When finished, output the line: '{discipline.value.upper()} WORK COMPLETE'."
        )
        result = await agent.run(prompt, wd)
        text = result.display(max_chars=1500)
        await self._send_agent(
            agent_name,
            ch_id,
            f"**{agent.role_name} ({agent.name})** finished:\n\n{text}",
        )
        await self._mark_specialist_complete(project_id, discipline, text)

    async def _mark_specialist_complete(
        self, project_id: str, discipline: Discipline, output: str,
    ) -> None:
        """Record specialist completion and announce project completion once."""
        marker = f"{discipline.value.upper()} WORK COMPLETE"
        if marker not in (output or "").upper():
            return

        completion_channel_id: Optional[int] = None
        completion_text: Optional[str] = None
        async with self._lock(project_id):
            project = self.store.get_project(project_id)
            if project is None:
                return
            completed = set(project.completed_disciplines)
            if discipline.value in completed:
                return
            completed.add(discipline.value)
            ordered_completed = [
                d for d in project.discipline_channels.keys() if d in completed
            ]
            project.completed_disciplines = ordered_completed

            expected = set(project.discipline_channels.keys())
            if expected and expected.issubset(completed) and project.state != ProjectState.COMPLETE:
                project.state = ProjectState.COMPLETE
                completion_channel_id = project.project_channel_id
                completion_text = (
                    f"**Athena**: ✅ project `{project.project_id}` complete. "
                    "All specialist workstreams finished: "
                    + ", ".join(f"`{name}`" for name in ordered_completed)
                    + "."
                )
            self.store.update_project(project)

        if completion_channel_id and completion_text:
            await self._send_agent("athena", completion_channel_id, completion_text)

    # ------------------------------------------------------------------
    # Discipline channel: specialist replies + per-channel confinement
    # ------------------------------------------------------------------

    async def _handle_discipline_channel(
        self, project_id: str, discipline: str, msg: IncomingMessage
    ) -> None:
        # Per-channel agent confinement: only the specialist for THIS discipline
        # is allowed to respond here. Other specialist names mentioned in this
        # channel are ignored. (FR-010, FR-018)
        agent_name = DISCIPLINE_AGENT[Discipline(discipline)]
        agent = self.agents[agent_name]

        # If user posted a question or extra context, run the specialist again.
        project = self.store.get_project(project_id)
        if project is None:
            return
        spec = self.store.latest_spec(project_id)
        if spec is None:
            return
        wd = Path(project.working_dir) / discipline
        prompt = (
            f"Continuation in your {discipline} sub-channel.\n"
            f"User message: {msg.content}\n\n"
            f"Spec context (v{spec.version}):\n{spec.content}\n\n"
            f"Reply only in this channel. End with '{discipline.upper()} WORK COMPLETE' "
            f"if you are done."
        )
        result = await agent.run(prompt, wd)
        text = result.display(1500)
        await self._send_agent(
            agent_name, msg.channel_id, f"**{agent.role_name} ({agent.name})**: {text}"
        )
        await self._mark_specialist_complete(project_id, Discipline(discipline), text)
