"""Mythos orchestrator: routes Discord messages through the project state
machine and dispatches each agent in turn.

Wiring:

  DiscordTransport ──on_message──▶ Orchestrator.handle_message
                          │
                          ▼
                  ProjectRegistry      ◀── per-channel project lookup
                          │
                          ▼
                  Project FSM          ◀── what phase are we in?
                          │
                          ▼
                  AgentRunner          ◀── spawn the right CLI

Tests inject a ``FakeDiscordTransport`` and a ``FakeAgentRunner`` and
exercise the same code path the real Discord wiring uses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from mythos.agents import AGENT_ROSTER, AgentRole
from mythos.config import MythosConfig
from mythos.prompts import (
    draft_plan_prompt,
    main_decompose_prompt,
    main_intake_prompt,
    review_prompt,
    specialist_prompt,
)
from mythos.runners import AgentExecError, AgentRunner, AgentTimeoutError
from mythos.state import (
    Project,
    ProjectRegistry,
    ProjectState,
    Workstream,
    WorkstreamKind,
)
from mythos.transport import DiscordTransport, IncomingMessage

logger = logging.getLogger(__name__)


# Workstream kind → role (agent that drives that channel).
_WORKSTREAM_TO_ROLE: Dict[WorkstreamKind, AgentRole] = {
    WorkstreamKind.FRONTEND: AgentRole.FRONTEND,
    WorkstreamKind.BACKEND: AgentRole.BACKEND,
    WorkstreamKind.TEST: AgentRole.TEST,
}


class Orchestrator:
    """Drives the multi-agent workflow.

    One instance per process. Holds a :class:`ProjectRegistry` and dispatches
    incoming messages to the project that owns the originating channel.
    """

    def __init__(
        self,
        *,
        config: MythosConfig,
        transport: DiscordTransport,
        runner: AgentRunner,
        registry: Optional[ProjectRegistry] = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.runner = runner
        self.registry = registry or ProjectRegistry()
        self._project_locks: Dict[str, asyncio.Lock] = {}
        self._tasks: List[asyncio.Task] = []

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.transport.on_message(self.handle_message)
        await self.transport.start()
        logger.info("[mythos] orchestrator started; main_channel=%s",
                    self.config.discord_main_channel_id)

    async def stop(self) -> None:
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.transport.stop()

    # -- message routing --------------------------------------------------

    async def handle_message(self, msg: IncomingMessage) -> None:
        """Top-level dispatch from the Discord transport.

        Decides which subroutine should handle the message based on which
        channel it arrived in and what state the owning project is in.
        """
        if msg.is_bot:
            return

        # Main channel → intake
        if (
            self.config.discord_main_channel_id is not None
            and msg.channel_id == self.config.discord_main_channel_id
        ):
            await self._handle_main_channel(msg)
            return

        # Project / workstream channel → look up owner
        project = self.registry.project_for_channel(msg.channel_id)
        if project is None:
            return  # not a Mythos channel

        # Workstream channel: only the specialist for this kind may act here
        # (FR-018, FR-022). Right now specialists are one-shot, so user
        # messages in workstream channels just become acknowledgements.
        ws_kind = self.registry.workstream_kind_for_channel(msg.channel_id)
        if ws_kind is not None:
            await self._handle_workstream_channel(project, ws_kind, msg)
            return

        # Project channel — depends on phase.
        await self._handle_project_channel(project, msg)

    async def _handle_main_channel(self, msg: IncomingMessage) -> None:
        request = msg.content.strip()
        if not request:
            return

        # Use the main agent to decide whether this is a project request.
        intake = await self._run(AgentRole.MAIN, main_intake_prompt(request),
                                 self.config.workspace_dir / "_intake")
        decision = intake.stdout.strip().splitlines()[0] if intake.stdout.strip() else ""
        m = re.match(r"^PROJECT:\s*(.+)$", decision, re.IGNORECASE)
        if not m:
            await self.transport.send_message(
                msg.channel_id,
                "Athena: that doesn't look like a project request — let me know "
                "if you'd like to start one.",
            )
            return

        slug = _slugify(m.group(1))
        project = Project.new(request=request, requester_id=msg.author_id)
        self.registry.register(project)
        await self._setup_project_channel(project, slug)
        # Kick off draft planning in the background so the main channel handler
        # returns quickly. Concurrent projects are served by independent tasks.
        self._spawn(self._run_draft_plan(project))

    async def _handle_project_channel(
        self, project: Project, msg: IncomingMessage
    ) -> None:
        async with self._lock_for(project):
            if project.state is ProjectState.PLANNING:
                # User answering a clarifying question — re-run the draft plan
                # with the answer appended to the request.
                project.request = (
                    project.request.rstrip()
                    + "\n\n[User clarification]\n"
                    + msg.content.strip()
                )
                self._spawn(self._run_draft_plan(project, after_clarification=True))
                return

            if project.state is ProjectState.AWAITING_APPROVAL:
                if self._is_approval(msg.content):
                    await self.transport.send_message(
                        project.project_channel_id,
                        f"Athena: approval received. Decomposing the work now.",
                    )
                    self._spawn(self._run_decompose(project))
                else:
                    await self.transport.send_message(
                        project.project_channel_id,
                        "Athena: I'll treat that as change requests. Sending "
                        "back to Prometheus for revision.",
                    )
                    project.transition_to(ProjectState.PLANNING)
                    project.request = (
                        project.request.rstrip()
                        + "\n\n[User change request]\n"
                        + msg.content.strip()
                    )
                    project.revision_count += 1
                    self._spawn(self._run_draft_plan(project, after_clarification=True))
                return

            # Other states: simply log; user messages don't drive transitions.
            logger.debug(
                "[mythos] user message in project %s while state=%s",
                project.id,
                project.state.value,
            )

    async def _handle_workstream_channel(
        self,
        project: Project,
        ws_kind: WorkstreamKind,
        msg: IncomingMessage,
    ) -> None:
        # The user is talking to the specialist — respond in-channel only,
        # never escalate to other channels (FR-018).
        await self.transport.send_message(
            msg.channel_id,
            f"{AGENT_ROSTER[_WORKSTREAM_TO_ROLE[ws_kind]].name}: noted.",
        )

    # -- project setup ----------------------------------------------------

    async def _setup_project_channel(self, project: Project, slug: str) -> None:
        if self.config.discord_guild_id is None:
            raise RuntimeError("MythosConfig.discord_guild_id is not set")
        channel_name = f"proj-{project.id[:6]}-{slug}"[:90]
        try:
            cid = await self.transport.create_text_channel(
                guild_id=self.config.discord_guild_id,
                name=channel_name,
                topic=f"Mythos project {project.id}: {project.request[:100]}",
            )
        except Exception as exc:
            logger.exception("[mythos] failed to create project channel")
            project.last_error = str(exc)
            project.transition_to(ProjectState.BLOCKED)
            await self.transport.send_message(
                self.config.discord_main_channel_id,
                f"Athena: failed to create a channel for that request — "
                f"{exc!s}. Project marked blocked.",
            )
            return

        self.registry.bind_project_channel(project, cid)
        project.project_channel_name = channel_name
        project.workdir = self.config.workspace_dir / project.id
        project.workdir.mkdir(parents=True, exist_ok=True)
        project.transition_to(ProjectState.PLANNING)

        # Handoff message in the new channel + ack in main channel.
        await self.transport.send_message(
            cid,
            f"Athena: project `{project.id}` opened from your request:\n"
            f"> {project.request[:400]}\n\n"
            f"Pinging **Prometheus** to draft a design spec.",
        )
        await self.transport.send_message(
            self.config.discord_main_channel_id,
            f"Athena: created project channel `{channel_name}` "
            f"for <@{project.requester_id}> — continuing there.",
        )

    # -- agent flows ------------------------------------------------------

    async def _run_draft_plan(
        self, project: Project, *, after_clarification: bool = False
    ) -> None:
        async with self._lock_for(project):
            if not after_clarification:
                # Initial entry — already PLANNING from setup.
                pass

            prior_review = project.latest_review() if project.revision_count else None
            try:
                result = await self._run(
                    AgentRole.DRAFT_PLAN,
                    draft_plan_prompt(project, prior_review),
                    project.workdir,
                )
            except (AgentExecError, AgentTimeoutError) as exc:
                await self._block(project, f"Prometheus failed: {exc}")
                return

            text = result.stdout.strip()
            if text.upper().startswith("CLARIFY:"):
                # Stay in PLANNING; ask the user.
                question = text.split(":", 1)[1].strip()
                await self.transport.send_message(
                    project.project_channel_id,
                    f"Prometheus: I need a clarification before drafting:\n{question}",
                )
                return

            spec = project.add_spec(text)
            await self.transport.send_message(
                project.project_channel_id,
                f"Prometheus: design spec v{spec.version} drafted. Pinging "
                f"**Argus** to review.\n\n```\n{_truncate(spec.body, 1500)}\n```",
            )
            project.transition_to(ProjectState.REVIEW)

        await self._run_review(project)

    async def _run_review(self, project: Project) -> None:
        async with self._lock_for(project):
            try:
                result = await self._run(
                    AgentRole.REVIEW,
                    review_prompt(project),
                    project.workdir,
                )
            except (AgentExecError, AgentTimeoutError) as exc:
                await self._block(project, f"Argus failed: {exc}")
                return

            review_text = result.stdout.strip()
            blocking = "STATUS: REVISE" in review_text.upper()
            review = project.add_review(review_text, blocking=blocking)

            await self.transport.send_message(
                project.project_channel_id,
                f"Argus: review of v{review.spec_version} posted "
                f"({'BLOCKING — revising' if blocking else 'no blockers'}).\n\n"
                f"```\n{_truncate(review.body, 1500)}\n```",
            )

            if blocking and project.revision_count < self.config.max_revision_rounds:
                project.revision_count += 1
                project.transition_to(ProjectState.PLANNING)
                # Re-run draft plan with the review feedback.
                self._spawn(self._run_draft_plan(project, after_clarification=True))
                return

            # Either reviewer approved, or we've used up revisions — go to
            # the user for an explicit approval.
            project.transition_to(ProjectState.AWAITING_APPROVAL)
            await self.transport.send_message(
                project.project_channel_id,
                "Athena: design spec is ready for your decision. Reply with "
                "`approve` to start implementation, or describe any changes "
                "you'd like.",
            )

    async def _run_decompose(self, project: Project) -> None:
        async with self._lock_for(project):
            project.transition_to(ProjectState.DECOMPOSING)
            try:
                result = await self._run(
                    AgentRole.MAIN,
                    main_decompose_prompt(project),
                    project.workdir,
                )
            except (AgentExecError, AgentTimeoutError) as exc:
                await self._block(project, f"Athena decomposition failed: {exc}")
                return

            kinds = _parse_workstreams(result.stdout)
            if not kinds:
                # Sensible default: every workstream.
                kinds = [WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND, WorkstreamKind.TEST]

            await self._create_workstream_channels(project, kinds)
            project.transition_to(ProjectState.IMPLEMENTING)

        # Run specialists concurrently.
        await asyncio.gather(
            *(self._run_specialist(project, kind) for kind in kinds),
            return_exceptions=False,
        )

        # After implementation, declare done. Tests/verification follow the
        # test workstream completion, which itself reports STATUS: DONE.
        if project.state is not ProjectState.BLOCKED:
            async with self._lock_for(project):
                # IMPLEMENTING -> TESTING -> COMPLETE if not already
                if project.state is ProjectState.IMPLEMENTING:
                    project.transition_to(ProjectState.TESTING)
                if project.state is ProjectState.TESTING:
                    project.transition_to(ProjectState.COMPLETE)
            await self.transport.send_message(
                project.project_channel_id,
                f"Athena: project `{project.id}` complete.",
            )

    async def _create_workstream_channels(
        self, project: Project, kinds: List[WorkstreamKind]
    ) -> None:
        for kind in kinds:
            channel_name = f"{project.project_channel_name}-{kind.value}"[:90]
            cid = await self.transport.create_text_channel(
                guild_id=self.config.discord_guild_id,
                name=channel_name,
                topic=f"{kind.value} workstream for project {project.id}",
                parent_channel_id=None,
            )
            self.registry.bind_workstream_channel(project, kind, cid)
            ws = project.workstreams[kind]
            ws.channel_name = channel_name
            ws.workdir = (project.workdir or self.config.workspace_dir / project.id) / kind.value
            ws.workdir.mkdir(parents=True, exist_ok=True)
            await self.transport.send_message(
                cid,
                f"Athena: opening the {kind.value} workstream for project "
                f"`{project.id}`. Pinging **{AGENT_ROSTER[_WORKSTREAM_TO_ROLE[kind]].name}**.",
            )

    async def _run_specialist(self, project: Project, kind: WorkstreamKind) -> None:
        role = _WORKSTREAM_TO_ROLE[kind]
        ws = project.workstreams[kind]
        ws.status = "in_progress"
        try:
            result = await self._run(
                role,
                specialist_prompt(project, ws, role),
                ws.workdir or project.workdir or self.config.workspace_dir / project.id,
            )
        except (AgentExecError, AgentTimeoutError) as exc:
            ws.status = "blocked"
            ws.last_message = str(exc)
            await self.transport.send_message(
                ws.channel_id,
                f"{AGENT_ROSTER[role].name}: blocked — {exc}.",
            )
            await self.transport.send_message(
                project.project_channel_id,
                f"Athena: {kind.value} workstream is blocked. See "
                f"<#{ws.channel_id}>.",
            )
            return

        text = result.stdout.strip()
        ws.last_message = text
        if "STATUS: BLOCKED" in text.upper():
            ws.status = "blocked"
            await self.transport.send_message(
                ws.channel_id,
                f"{AGENT_ROSTER[role].name}: blocked — see message above.",
            )
            return

        ws.status = "done"
        await self.transport.send_message(
            ws.channel_id,
            f"{AGENT_ROSTER[role].name}: workstream complete.\n\n"
            f"```\n{_truncate(text, 1500)}\n```",
        )
        await self.transport.send_message(
            project.project_channel_id,
            f"Athena: {kind.value} workstream finished — see <#{ws.channel_id}>.",
        )

    # -- helpers ---------------------------------------------------------

    async def _run(self, role: AgentRole, prompt: str, workdir: Path):
        cfg = self.config.agent(role)
        return await self.runner.run(role, prompt, workdir, cfg)

    async def _block(self, project: Project, reason: str) -> None:
        project.last_error = reason
        try:
            project.transition_to(ProjectState.BLOCKED)
        except ValueError:
            project.state = ProjectState.BLOCKED  # force, FSM dead-end
        await self.transport.send_message(
            project.project_channel_id,
            f"Athena: project blocked — {reason}",
        )

    def _is_approval(self, content: str) -> bool:
        normalized = content.strip().lower()
        return any(
            normalized == kw or normalized.startswith(kw + " ") or normalized.startswith(kw + "!")
            for kw in self.config.approval_keywords
        )

    def _lock_for(self, project: Project) -> asyncio.Lock:
        lock = self._project_locks.get(project.id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project.id] = lock
        return lock

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            pass
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "[mythos] background task crashed", exc_info=exc
            )


# ---- module-level helpers -------------------------------------------------

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = _SLUG_PATTERN.sub("-", text).strip("-")
    return text[:48] or "project"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 12] + "\n…[truncated]"


def _parse_workstreams(text: str) -> List[WorkstreamKind]:
    out: List[WorkstreamKind] = []
    seen: set = set()
    for line in text.splitlines():
        m = re.match(r"^\s*WORKSTREAM:\s*(\w+)\s*$", line, re.IGNORECASE)
        if not m:
            continue
        try:
            kind = WorkstreamKind(m.group(1).lower())
        except ValueError:
            continue
        if kind in seen:
            continue
        seen.add(kind)
        out.append(kind)
    return out
