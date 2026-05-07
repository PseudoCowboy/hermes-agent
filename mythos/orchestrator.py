"""Mythos orchestrator.

Owns the project state machine, routes incoming Discord messages to the
correct agent, enforces approval gates, and dispatches specialist agents
into per-role channels post-approval.

The orchestrator is the only component allowed to:
  - Transition a project's phase
  - Create Discord channels
  - Invoke agent runners
  - Persist state changes
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from mythos.config import MythosConfig
from mythos.discord_bot import (
    DiscordTransport,
    IncomingMessage,
    OutgoingMessage,
)
from mythos.models import (
    AgentRole,
    AgentRun,
    Approval,
    Artifact,
    ArtifactType,
    AuditEvent,
    ChannelRole,
    Project,
    ProjectChannel,
    ProjectPhase,
    ROLE_CODENAME,
    Task,
    TaskStatus,
)
from mythos.runners import (
    AgentPromptPacket,
    AgentRunResult,
    PROVIDER_RUNNERS,
    FakeRunner,
)
from mythos.state_machine import (
    IllegalTransition,
    can_dispatch_implementation,
    can_transition,
)
from mythos.store import JsonStore
from mythos.workspace import WorkspaceManager


logger = logging.getLogger(__name__)


# Heuristics to detect what kind of work the design needs.
FRONTEND_KEYWORDS = (
    "frontend", "front-end", "ui", "react", "vue", "extension",
    "browser", "css", "html", "dom", "popup", "chrome", "client",
)
BACKEND_KEYWORDS = (
    "backend", "back-end", "api", "server", "database", "sql",
    "endpoint", "service", "queue", "auth", "rest", "graphql",
)
TEST_KEYWORDS = (
    "test", "tests", "qa", "verification", "verify", "coverage",
    "integration test", "unit test",
)


def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len] or "project"


def _project_channel_name(project: Project) -> str:
    return f"prj-{_slugify(project.title or project.request_text, 40)}"


@dataclass
class OrchestratorContext:
    """Shared mutable services the orchestrator needs at runtime."""
    config: MythosConfig
    store: JsonStore
    workspace: WorkspaceManager
    transport: DiscordTransport
    fake_runner: Optional[FakeRunner] = None  # only when use_fake_runners


class Orchestrator:
    """Project orchestrator. One instance per process."""

    def __init__(self, ctx: OrchestratorContext):
        self.ctx = ctx
        self.transport = ctx.transport
        self.store = ctx.store
        self.workspace = ctx.workspace
        self.config = ctx.config
        self._project_locks: Dict[str, asyncio.Lock] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Wire the transport and start it. Returns when transport stops."""
        self.transport.set_message_handler(self.handle_incoming)
        self._running = True
        await self.transport.start()

    async def stop(self) -> None:
        self._running = False
        await self.transport.stop()

    # ------------------------------------------------------------------
    # Lock per project (workspace + state mutations are serialized)
    # ------------------------------------------------------------------

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Inbound message routing
    # ------------------------------------------------------------------

    async def handle_incoming(self, msg: IncomingMessage) -> None:
        """Entry point: route a Discord message to the right project flow."""
        try:
            if msg.is_bot:
                return
            if msg.channel_id == self.config.discord.main_channel_id:
                await self._handle_main_channel_message(msg)
                return
            project = self.store.find_project_by_channel(msg.channel_id)
            if project is None:
                logger.debug("ignoring message in untracked channel %s", msg.channel_id)
                return
            channel_role = self.store.find_channel_role_for(project.id, msg.channel_id)
            if channel_role is None:
                return
            async with self._lock_for(project.id):
                if channel_role == ChannelRole.PROJECT.value:
                    await self._handle_project_channel_message(project, msg)
                else:
                    await self._handle_specialist_channel_message(
                        project, msg, channel_role
                    )
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("handle_incoming error: %s", exc)

    # ------------------------------------------------------------------
    # Phase: intake (main channel -> create project)
    # ------------------------------------------------------------------

    async def _handle_main_channel_message(self, msg: IncomingMessage) -> None:
        # Don't react to our own bot's posts
        if not msg.content.strip():
            return

        # Title heuristic: first 8 words
        title = " ".join(msg.content.strip().split()[:8])
        project = Project(
            title=title,
            request_text=msg.content,
            owner_user_id=msg.user_id,
            source_channel_id=msg.channel_id,
            source_message_id=msg.message_id,
            phase=ProjectPhase.INTAKE,
        )
        self.workspace.create(project.id)
        project.workspace_path = str(self.workspace.project_root(project.id))
        self.store.save_project(project)
        self._audit(project.id, "project.created", "user", msg.user_id, {
            "title": title, "source_message_id": msg.message_id,
        })

        # Acknowledge in main channel
        await self.transport.send(
            self.config.discord.main_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: project `{project.id}` accepted. "
            f"Spinning up channel for: _{title}_",
        )

        # Create project channel and persist
        channel_name = _project_channel_name(project)
        async with self._lock_for(project.id):
            project_channel_id = await self.transport.create_project_channel(
                project_id=project.id,
                channel_name=channel_name,
                parent_category_id=self.config.discord.project_category_id,
            )
            project.project_channel_id = project_channel_id
            project_channel = ProjectChannel(
                project_id=project.id,
                discord_channel_id=project_channel_id,
                channel_role=ChannelRole.PROJECT,
            )
            self.store.save_channel(project_channel)
            self.store.save_project(project)

            await self.transport.send(
                project_channel_id,
                f"**{ROLE_CODENAME[AgentRole.MAIN]}**: project `{project.id}` opened.\n"
                f"Original request:\n> {msg.content}\n\n"
                f"Pinging **{ROLE_CODENAME[AgentRole.DRAFT_PLAN]}** to draft a design spec.",
            )

            self._transition(project, ProjectPhase.PLANNING)
            await self._run_draft_planning(project)

    # ------------------------------------------------------------------
    # Phase: planning (Prometheus drafts spec, may ask clarifications)
    # ------------------------------------------------------------------

    async def _run_draft_planning(self, project: Project) -> None:
        prior = self._channel_history(project.project_channel_id)
        packet = self._build_packet(
            project=project,
            role=AgentRole.DRAFT_PLAN,
            target_channel_id=project.project_channel_id,
            extra_context=(
                "You are Prometheus, the draft plan agent. Analyze the user's request. "
                "If the request is ambiguous, ask up to two clarifying questions in this "
                "channel and stop. Otherwise produce a complete design spec containing: "
                "Overview, User Stories, Functional Requirements, "
                "Technical Architecture (Frontend / Backend / Test), and Open Questions. "
                "Mark spec with the heading '## Design Spec'."
            ),
            prior_messages=prior,
        )
        result = await self._invoke_agent(project, AgentRole.DRAFT_PLAN, packet)
        text = (result.stdout or "").strip()

        if self._looks_like_clarification(text):
            self._transition(project, ProjectPhase.CLARIFICATION)
            await self.transport.send(
                project.project_channel_id,
                f"**{ROLE_CODENAME[AgentRole.DRAFT_PLAN]}** (clarifying questions):\n{text}",
            )
            return

        # Treat as a draft spec
        await self._record_design_spec(project, text)
        await self._dispatch_review(project)

    async def _record_design_spec(self, project: Project, spec_text: str) -> Artifact:
        version = 1 + sum(
            1 for a in self.store.list_artifacts(project.id)
            if a.type == ArtifactType.DESIGN_SPEC
        )
        path = self.workspace.artifact_path(
            project.id, f"design-spec-v{version}.md"
        )
        path.write_text(spec_text, encoding="utf-8")
        artifact = Artifact(
            project_id=project.id, type=ArtifactType.DESIGN_SPEC,
            version=version, path=str(path),
        )
        self.store.save_artifact(artifact)
        self._transition(project, ProjectPhase.DRAFT_READY)
        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.DRAFT_PLAN]}** posted "
            f"design spec v{version}:\n\n{spec_text[:1500]}"
            + ("\n…(truncated, full spec on disk)" if len(spec_text) > 1500 else "")
            + f"\n\nPinging **{ROLE_CODENAME[AgentRole.REVIEW]}** for review.",
        )
        return artifact

    # ------------------------------------------------------------------
    # Phase: review (Argus produces review comments) -> awaiting_approval
    # ------------------------------------------------------------------

    async def _dispatch_review(self, project: Project) -> None:
        self._transition(project, ProjectPhase.REVIEW)
        spec = self._latest_artifact(project.id, ArtifactType.DESIGN_SPEC)
        if spec is None:
            await self._fail(project, "No design spec found to review.")
            return
        spec_text = Path(spec.path).read_text(encoding="utf-8")
        packet = self._build_packet(
            project=project,
            role=AgentRole.REVIEW,
            target_channel_id=project.project_channel_id,
            approved_design_artifact=spec_text,
            extra_context=(
                "You are Argus, the review agent. Review the draft design spec for "
                "completeness, contradictions, missing requirements, security gaps, "
                "and unclear scope. Output: a 'Review Verdict' (approve / "
                "request-changes), followed by 3-8 numbered review comments."
            ),
        )
        result = await self._invoke_agent(project, AgentRole.REVIEW, packet)
        review_text = (result.stdout or "").strip()
        version = 1 + sum(
            1 for a in self.store.list_artifacts(project.id)
            if a.type == ArtifactType.REVIEW_COMMENTS
        )
        path = self.workspace.artifact_path(
            project.id, f"review-v{version}.md"
        )
        path.write_text(review_text, encoding="utf-8")
        self.store.save_artifact(Artifact(
            project_id=project.id, type=ArtifactType.REVIEW_COMMENTS,
            version=version, path=str(path),
        ))

        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.REVIEW]}** review v{version}:\n\n{review_text[:1500]}"
            + ("\n…(truncated)" if len(review_text) > 1500 else ""),
        )

        # Decide: revision or ask user for approval. If review explicitly
        # requests changes AND we have rounds left, loop. Otherwise ask user.
        wants_changes = self._review_wants_changes(review_text)
        if wants_changes and project.review_round < self.config.max_review_rounds:
            project.review_round += 1
            self.store.save_project(project)
            self._transition(project, ProjectPhase.REVISION)
            await self.transport.send(
                project.project_channel_id,
                f"**{ROLE_CODENAME[AgentRole.MAIN]}**: review requested changes. "
                f"Round {project.review_round}/{self.config.max_review_rounds}. "
                f"Sending revision back to {ROLE_CODENAME[AgentRole.DRAFT_PLAN]}.",
            )
            self._transition(project, ProjectPhase.PLANNING)
            await self._run_draft_planning(project)
            return

        self._transition(project, ProjectPhase.AWAITING_APPROVAL)
        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: review complete. "
            f"Please reply **approve** to proceed with implementation, "
            f"or describe changes you want.",
        )

    # ------------------------------------------------------------------
    # Phase: project-channel inbound during AWAITING_APPROVAL or others
    # ------------------------------------------------------------------

    async def _handle_project_channel_message(
        self, project: Project, msg: IncomingMessage
    ) -> None:
        if project.phase == ProjectPhase.CLARIFICATION:
            # User answered clarification -> continue planning
            self._transition(project, ProjectPhase.PLANNING)
            await self.transport.send(
                project.project_channel_id,
                f"**{ROLE_CODENAME[AgentRole.MAIN]}**: thanks, forwarding answer "
                f"to {ROLE_CODENAME[AgentRole.DRAFT_PLAN]}.",
            )
            await self._run_draft_planning(project)
            return

        if project.phase == ProjectPhase.AWAITING_APPROVAL:
            if self._is_approval(msg.content):
                if msg.user_id != project.owner_user_id:
                    await self.transport.send(
                        project.project_channel_id,
                        f"**{ROLE_CODENAME[AgentRole.MAIN]}**: only the project "
                        f"owner can approve.",
                    )
                    return
                await self._handle_approval(project, msg)
            else:
                # Treat as feedback -> revision
                if project.review_round >= self.config.max_review_rounds:
                    await self.transport.send(
                        project.project_channel_id,
                        f"**{ROLE_CODENAME[AgentRole.MAIN]}**: max review rounds "
                        f"reached. Please reply 'approve' or 'cancel'.",
                    )
                    return
                project.review_round += 1
                self.store.save_project(project)
                self._transition(project, ProjectPhase.REVISION)
                self._transition(project, ProjectPhase.PLANNING)
                await self.transport.send(
                    project.project_channel_id,
                    f"**{ROLE_CODENAME[AgentRole.MAIN]}**: feedback noted, "
                    f"revising spec.",
                )
                await self._run_draft_planning(project)
            return

        # Other phases: just log; specialist work happens in sub-channels
        logger.debug(
            "project %s in phase %s ignored project-channel msg",
            project.id, project.phase.value,
        )

    async def _handle_specialist_channel_message(
        self, project: Project, msg: IncomingMessage, channel_role: str
    ) -> None:
        """User answered a specialist agent's question. Re-invoke that agent."""
        role_map = {
            ChannelRole.FRONTEND.value: AgentRole.FRONTEND,
            ChannelRole.BACKEND.value: AgentRole.BACKEND,
            ChannelRole.TEST.value: AgentRole.TEST,
        }
        role = role_map.get(channel_role)
        if role is None:
            return
        spec = self._latest_artifact(project.id, ArtifactType.APPROVED_DESIGN)
        if spec is None:
            return
        spec_text = Path(spec.path).read_text(encoding="utf-8")
        prior = self._channel_history(msg.channel_id)
        packet = self._build_packet(
            project=project, role=role,
            target_channel_id=msg.channel_id,
            approved_design_artifact=spec_text,
            extra_context=(
                f"User has replied in your channel. Continue your task: "
                f"answer their question, post progress, or post completion."
            ),
            prior_messages=prior,
        )
        result = await self._invoke_agent(project, role, packet)
        text = (result.stdout or "").strip()
        await self.transport.send(
            msg.channel_id,
            f"**{ROLE_CODENAME[role]}**: {text}",
        )

    # ------------------------------------------------------------------
    # Approval -> decomposition -> implementation
    # ------------------------------------------------------------------

    async def _handle_approval(self, project: Project, msg: IncomingMessage) -> None:
        latest_spec = self._latest_artifact(project.id, ArtifactType.DESIGN_SPEC)
        if latest_spec is None:
            await self._fail(project, "Approval received but no design spec exists.")
            return
        # Lock approved design as immutable artifact (architecture Decision 4)
        spec_text = Path(latest_spec.path).read_text(encoding="utf-8")
        approved_path = self.workspace.artifact_path(project.id, "approved-design.md")
        approved_path.write_text(spec_text, encoding="utf-8")
        approved = Artifact(
            project_id=project.id,
            type=ArtifactType.APPROVED_DESIGN,
            version=latest_spec.version,
            path=str(approved_path),
            immutable=True,
        )
        self.store.save_artifact(approved)
        project.approved_design_artifact_id = approved.id
        self._transition(project, ProjectPhase.APPROVED)
        self.store.save_project(project)
        self.store.save_approval(Approval(
            project_id=project.id, artifact_id=approved.id,
            approved_by_user_id=msg.user_id,
            approval_message_id=msg.message_id,
        ))
        self._audit(project.id, "design.approved", "user", msg.user_id, {
            "artifact_id": approved.id, "version": approved.version,
        })
        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: design approved. "
            f"Decomposing work and creating implementation channels.",
        )
        await self._decompose_and_dispatch(project, spec_text)

    async def _decompose_and_dispatch(self, project: Project, spec_text: str) -> None:
        if not can_dispatch_implementation(project.phase):
            await self._fail(
                project,
                f"Refusing to dispatch from phase {project.phase.value}.",
            )
            return
        self._transition(project, ProjectPhase.DECOMPOSING)
        approved = self.store.get_artifact(
            project.id, project.approved_design_artifact_id or ""
        )
        if approved is None:
            await self._fail(project, "Approved artifact missing.")
            return

        plan = _decompose_design(spec_text)
        roles_to_create: List[AgentRole] = []
        if plan["frontend"]:
            roles_to_create.append(AgentRole.FRONTEND)
        if plan["backend"]:
            roles_to_create.append(AgentRole.BACKEND)
        if plan["test"]:
            roles_to_create.append(AgentRole.TEST)
        # Always include test if any implementation work exists
        if (plan["frontend"] or plan["backend"]) and AgentRole.TEST not in roles_to_create:
            roles_to_create.append(AgentRole.TEST)

        # Create channels
        for role in roles_to_create:
            channel_role = ChannelRole(role.value)
            existing = self.store.find_channel(project.id, channel_role.value)
            if existing:
                continue
            channel_name = f"{_slugify(project.title or 'p', 30)}-{role.value}"
            cid = await self.transport.create_subchannel(
                project_id=project.id,
                parent_channel_id=project.project_channel_id,
                channel_name=channel_name,
            )
            self.store.save_channel(ProjectChannel(
                project_id=project.id, discord_channel_id=cid,
                channel_role=channel_role,
            ))

        # Create tasks
        tasks: List[Task] = []
        for role in roles_to_create:
            channel = self.store.find_channel(project.id, role.value)
            assert channel is not None
            brief = plan[role.value] or (
                f"Implement the {role.value} portion of the approved design."
            )
            task = Task(
                project_id=project.id, role=role,
                title=f"{role.value} implementation",
                description=brief,
                source_artifact_id=approved.id,
                target_channel_id=channel.discord_channel_id,
            )
            self.store.save_task(task)
            tasks.append(task)

        self._transition(project, ProjectPhase.IMPLEMENTING)

        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: created "
            f"{len(roles_to_create)} channel(s): "
            f"{', '.join(r.value for r in roles_to_create)}",
        )

        # Dispatch implementation tasks (frontend / backend) first
        for task in [t for t in tasks if t.role in (AgentRole.FRONTEND, AgentRole.BACKEND)]:
            await self._dispatch_specialist_task(project, task, spec_text)

        # Then test
        if any(t.role == AgentRole.TEST for t in tasks):
            self._transition(project, ProjectPhase.TESTING)
            for task in [t for t in tasks if t.role == AgentRole.TEST]:
                await self._dispatch_specialist_task(project, task, spec_text)

        self._transition(project, ProjectPhase.COMPLETED)
        await self.transport.send(
            project.project_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: project `{project.id}` "
            f"reached completion. See sub-channels for per-role outputs.",
        )

    async def _dispatch_specialist_task(
        self, project: Project, task: Task, spec_text: str
    ) -> None:
        task.status = TaskStatus.RUNNING
        self.store.save_task(task)
        await self.transport.send(
            task.target_channel_id,
            f"**{ROLE_CODENAME[task.role]}** task brief:\n{task.description}",
        )
        packet = self._build_packet(
            project=project, role=task.role,
            target_channel_id=task.target_channel_id,
            approved_design_artifact=spec_text,
            task_id=task.id, task_brief=task.description,
            extra_context=(
                f"You are {ROLE_CODENAME[task.role]}. Implement the above task. "
                f"If you have questions, post them in this channel only. "
                f"When done, post a 'Completion Summary' with file paths "
                f"and a brief description."
            ),
        )
        result = await self._invoke_agent(project, task.role, packet)
        summary = (result.stdout or "").strip()
        # Persist a per-task summary artifact
        art_type = (
            ArtifactType.TEST_REPORT if task.role == AgentRole.TEST
            else ArtifactType.IMPLEMENTATION_SUMMARY
        )
        path = self.workspace.task_dir(project.id, task.role.value) / "summary.md"
        path.write_text(summary, encoding="utf-8")
        self.store.save_artifact(Artifact(
            project_id=project.id, type=art_type, version=1, path=str(path),
        ))
        task.status = TaskStatus.COMPLETED if result.ok else TaskStatus.FAILED
        self.store.save_task(task)
        await self.transport.send(
            task.target_channel_id,
            f"**{ROLE_CODENAME[task.role]}** "
            f"{'completed' if result.ok else 'FAILED'}:\n{summary[:1800]}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_packet(
        self,
        project: Project,
        role: AgentRole,
        target_channel_id: str,
        approved_design_artifact: Optional[str] = None,
        task_id: Optional[str] = None,
        task_brief: Optional[str] = None,
        extra_context: str = "",
        prior_messages: Optional[List[Dict[str, str]]] = None,
    ) -> AgentPromptPacket:
        return AgentPromptPacket(
            project_id=project.id,
            project_title=project.title,
            role=role.value,
            phase=project.phase.value,
            target_channel_id=target_channel_id,
            workspace_path=project.workspace_path or str(
                self.workspace.project_root(project.id)
            ),
            allowed_questions_channel_id=target_channel_id,
            approved_design_artifact=approved_design_artifact,
            task_id=task_id, task_brief=task_brief,
            request_text=project.request_text,
            extra_context=extra_context,
            prior_messages=prior_messages or [],
        )

    async def _invoke_agent(
        self, project: Project, role: AgentRole, packet: AgentPromptPacket
    ) -> AgentRunResult:
        profile = self.config.agent(role.value)
        if self.config.use_fake_runners:
            runner = self.ctx.fake_runner or FakeRunner()
        else:
            runner = PROVIDER_RUNNERS[profile.provider]
        log_dir = self.workspace.log_dir(project.id)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: runner.run(
                packet=packet, command=profile.command, env=profile.env,
                log_dir=log_dir, timeout_seconds=profile.timeout_seconds,
            ),
        )
        run = AgentRun(
            project_id=project.id,
            task_id=packet.task_id,
            role=role,
            provider=profile.provider,
            command=result.command,
            started_at=result.started_at,
            ended_at=result.ended_at,
            exit_code=result.exit_code,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            summary=(result.stdout or "")[:500],
        )
        self.store.save_run(run)
        self._audit(project.id, "agent.run", "system", role.value, {
            "provider": profile.provider, "exit_code": result.exit_code,
            "duration": result.duration_seconds,
        })
        return result

    def _channel_history(self, channel_id: str) -> List[Dict[str, str]]:
        # Pull recent outgoing messages from the fake transport when possible;
        # for the real transport we don't reload Discord history (the agent
        # sees the prompt context instead).
        msgs: List[Dict[str, str]] = []
        chan_msgs = getattr(self.transport, "messages_by_channel", {}).get(channel_id, [])
        for m in chan_msgs[-10:]:
            msgs.append({"author": "bot", "text": m.content})
        return msgs

    def _latest_artifact(
        self, project_id: str, artifact_type: ArtifactType
    ) -> Optional[Artifact]:
        candidates = [
            a for a in self.store.list_artifacts(project_id) if a.type == artifact_type
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda a: (a.version, a.created_at))
        return candidates[-1]

    def _transition(self, project: Project, to_phase: ProjectPhase) -> None:
        if not can_transition(project.phase, to_phase):
            raise IllegalTransition(
                f"project {project.id}: {project.phase.value} -> {to_phase.value}"
            )
        old = project.phase
        project.phase = to_phase
        project.updated_at = time.time()
        self.store.save_project(project)
        self._audit(project.id, "phase.transition", "system", "orchestrator", {
            "from": old.value, "to": to_phase.value,
        })

    async def _fail(self, project: Project, reason: str) -> None:
        try:
            self._transition(project, ProjectPhase.FAILED)
        except IllegalTransition:
            project.phase = ProjectPhase.FAILED
            self.store.save_project(project)
        await self.transport.send(
            project.project_channel_id or self.config.discord.main_channel_id,
            f"**{ROLE_CODENAME[AgentRole.MAIN]}**: project `{project.id}` "
            f"failed: {reason}",
        )
        self._audit(project.id, "project.failed", "system", "orchestrator", {
            "reason": reason,
        })

    def _audit(
        self,
        project_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        payload: Dict,
    ) -> None:
        self.store.append_audit(AuditEvent(
            project_id=project_id, event_type=event_type,
            actor_type=actor_type, actor_id=actor_id, payload=payload,
        ))

    def _is_approval(self, text: str) -> bool:
        low = text.strip().lower()
        return any(low == k or low.startswith(k + " ") or low == k.lstrip("/") or k in low.split()
                   for k in self.config.discord.approval_keywords)

    def _looks_like_clarification(self, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        if "## design spec" in low or "# design spec" in low:
            return False
        # Heuristic: the agent is clarifying if it's clearly a question block.
        if "?" in text and len(text) < 1500 and "clarif" in low:
            return True
        if low.startswith("clarification") or low.startswith("questions"):
            return True
        return False

    def _review_wants_changes(self, text: str) -> bool:
        low = text.lower()
        if "request-changes" in low or "request changes" in low:
            return True
        if "approve" in low and "request" not in low:
            return False
        # Default conservative: don't auto-loop unless agent asks for it
        return False


# ---------------------------------------------------------------------------
# Decomposition heuristic (FR-022..FR-027)
# ---------------------------------------------------------------------------


def _decompose_design(spec_text: str) -> Dict[str, str]:
    """Split an approved design spec into per-role briefs.

    Strategy: look for headings whose name contains 'frontend', 'backend',
    'test'. If none found, fall back to keyword detection so we still create
    the right channels for short specs.
    """
    sections = _split_sections(spec_text)
    out = {"frontend": "", "backend": "", "test": ""}
    for heading, body in sections.items():
        h = heading.lower()
        if "frontend" in h or "front-end" in h or "ui" in h:
            out["frontend"] += (body + "\n").strip() + "\n"
        if "backend" in h or "back-end" in h or "api" in h or "server" in h:
            out["backend"] += (body + "\n").strip() + "\n"
        if "test" in h or "qa" in h or "verification" in h:
            out["test"] += (body + "\n").strip() + "\n"

    low = spec_text.lower()
    if not out["frontend"].strip() and any(k in low for k in FRONTEND_KEYWORDS):
        out["frontend"] = "Implement the frontend portion described in the design."
    if not out["backend"].strip() and any(k in low for k in BACKEND_KEYWORDS):
        out["backend"] = "Implement the backend portion described in the design."
    if not out["test"].strip() and any(k in low for k in TEST_KEYWORDS):
        out["test"] = "Verify the implementation against the design."
    return {k: v.strip() for k, v in out.items()}


def _split_sections(text: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current = "_root"
    buf: List[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if buf:
                sections[current] = "\n".join(buf).strip()
            current = line.lstrip("#").strip() or "_section"
            buf = []
        else:
            buf.append(line)
    if buf:
        sections[current] = "\n".join(buf).strip()
    return sections
