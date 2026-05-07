"""Mythos orchestrator: project lifecycle state machine.

This module wires together state, workspace, agent runner, and Discord
operations. It's transport-agnostic — Discord I/O is provided via the
`DiscordOps` protocol (so tests can use `FakeDiscordClient`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from . import prompts
from .agent_runner import AgentRunner, RunRequest, RunResult
from .config import MythosConfig
from .discord_ops import DiscordOps, MessageHandler, PostedMessage
from .roles import (CHANNEL_OWNERSHIP, ChannelKind, ProjectStatus, Role,
                    ROLE_TO_CLI)
from .state import (ApprovalEvent, ChannelRef, DesignSpec, Project,
                    ProjectStore, ReviewComment, TaskRecord,
                    new_project_id, slugify)
from .workspace import WorkspaceLayout, WorkspaceManager


log = logging.getLogger("mythos.orchestrator")


_INTAKE_HINT_RE = re.compile(r"\b(build|create|make|need|want|implement)\b", re.I)


def looks_like_intake(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if text.startswith("/"):
        return False  # ignore slash commands here
    return bool(_INTAKE_HINT_RE.search(text)) or len(text) > 40


def short_title_from_request(text: str) -> str:
    first = text.strip().splitlines()[0]
    if len(first) > 60:
        first = first[:60].rstrip() + "…"
    return first


# ---------------------------------------------------------------------------


@dataclass
class _PendingClarification:
    """A project waiting for the user to answer Prometheus's questions."""
    asked_at: float


class Orchestrator:
    def __init__(
        self,
        config: MythosConfig,
        store: ProjectStore,
        discord: DiscordOps,
        agent_runner: AgentRunner,
        workspace_manager: WorkspaceManager,
        guild_id: Optional[int] = None,
    ):
        self.config = config
        self.store = store
        self.discord = discord
        self.agent_runner = agent_runner
        self.workspace_manager = workspace_manager
        self.guild_id = int(guild_id or config.discord_guild_id or 0)

        # Per-project transient context: most recent N user messages in plan
        # channel. Persisted via project.audit but kept here for prompt
        # building convenience.
        self._channel_history: Dict[int, List[str]] = {}
        self._project_locks: Dict[str, asyncio.Lock] = {}

        # Wire up Discord message handler
        self.discord.register_handler(self._on_message)

    # ---- Locking --------------------------------------------------------

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

    # ---- Discord event ingress -----------------------------------------

    async def _on_message(self, msg: PostedMessage) -> None:
        if msg.is_bot:
            return  # ignore bot echoes
        try:
            if msg.channel_id == self.config.main_channel_id:
                await self._handle_intake(msg)
                return
            project = self.store.find_by_channel(msg.channel_id)
            if not project:
                return
            await self._handle_project_message(project, msg)
        except Exception as exc:
            log.exception("on_message failure: %s", exc)
            try:
                await self.discord.send(msg.channel_id,
                                        f":warning: mythos error: {exc}")
            except Exception:
                pass

    # ---- Intake ---------------------------------------------------------

    async def _handle_intake(self, msg: PostedMessage) -> Project:
        if not looks_like_intake(msg.content):
            return  # type: ignore[return-value]

        project_id, short_id = new_project_id()
        slug = slugify(short_title_from_request(msg.content))
        layout = self.workspace_manager.create(project_id)

        project = Project(
            project_id=project_id,
            short_id=short_id,
            slug=slug,
            status=ProjectStatus.INTAKE_RECEIVED,
            created_by_discord_user_id=msg.author_id,
            main_channel_id=msg.channel_id,
            original_request=msg.content,
            workspace_path=str(layout.root),
        )
        project.append_audit("intake_received", {"author_id": msg.author_id})
        self.store.create(project)

        # Acknowledge in main channel before doing anything heavy.
        await self.discord.send(
            msg.channel_id,
            f":hourglass_flowing_sand: **Athena**: accepted project "
            f"`{short_id}` — `{slug}`. Creating a project channel now.",
        )

        await self._setup_project_channels(project)
        await self._run_athena_intake(project)
        await self._run_prometheus_clarify(project)
        return project

    # ---- Channel setup --------------------------------------------------

    async def _setup_project_channels(self, project: Project) -> None:
        category_name = f"project-{project.short_id}-{project.slug}"
        if self.guild_id:
            project.category_id = await self.discord.create_category(
                self.guild_id, category_name)
        # Plan channel always exists from the start.
        await self._ensure_channel(project, ChannelKind.PLAN)
        project.status = ProjectStatus.PROJECT_CHANNEL_CREATED
        project.append_audit("project_channels_created",
                             {"category_id": project.category_id})
        self.store.save(project)

        plan = project.channel_for(ChannelKind.PLAN)
        if plan:
            await self.discord.send(
                project.main_channel_id,
                f":file_folder: Project `{project.short_id}` channel: <#{plan.discord_channel_id}>",
            )

    async def _ensure_channel(self, project: Project, kind: ChannelKind) -> ChannelRef:
        existing = project.channel_for(kind)
        if existing:
            return existing
        name = f"{project.short_id}-{kind.value}"
        cid = await self.discord.create_text_channel(
            self.guild_id, name, project.category_id,
        )
        ref = ChannelRef(kind=kind, discord_channel_id=cid, name=name)
        project.channels[kind.value] = ref
        project.append_audit("channel_created",
                             {"kind": kind.value, "channel_id": cid, "name": name})
        self.store.save(project)
        return ref

    # ---- Athena intake message in plan ---------------------------------

    async def _run_athena_intake(self, project: Project) -> None:
        plan = await self._ensure_channel(project, ChannelKind.PLAN)
        prompt = prompts.athena_intake(project.original_request, project.short_id)
        result = await self._run_agent(project, Role.ATHENA, ChannelKind.PLAN, prompt)
        if result.summary:
            await self._post(project, ChannelKind.PLAN,
                             f"**Athena**: {result.summary}")

    # ---- Prometheus clarification --------------------------------------

    async def _run_prometheus_clarify(self, project: Project) -> None:
        history = self._plan_history(project)
        prompt = prompts.prometheus_clarify(project.original_request, history)
        result = await self._run_agent(project, Role.PROMETHEUS, ChannelKind.PLAN, prompt)
        await self._post(project, ChannelKind.PLAN,
                         f"**Prometheus** (Draft Plan): {result.summary}")
        project.status = ProjectStatus.CLARIFYING_QUESTIONS
        project.append_audit("prometheus_clarify_posted")
        self.store.save(project)

    # ---- Project channel message handler -------------------------------

    async def _handle_project_message(self, project: Project, msg: PostedMessage) -> None:
        kind = project.channel_kind_for(msg.channel_id)
        if kind is None:
            return
        async with self._lock_for(project.project_id):
            self._record_history(msg.channel_id, msg.content)

            if kind != ChannelKind.PLAN:
                # Specialist channels: user can only chat with the resident
                # specialist. Re-run the specialist with the message as new
                # context.
                role = CHANNEL_OWNERSHIP[kind][0]
                await self._continue_specialist(project, kind, role, msg)
                return

            # Plan channel: depends on lifecycle phase
            if project.status == ProjectStatus.CLARIFYING_QUESTIONS:
                await self._handle_clarification_answer(project, msg)
            elif project.status == ProjectStatus.AWAITING_USER_APPROVAL:
                await self._handle_approval_or_revision(project, msg)
            elif project.status == ProjectStatus.REVISION_REQUESTED:
                # User added more context after revision request; let
                # Prometheus draft a new spec.
                await self._draft_spec(project, revision_feedback=msg.content)
            else:
                # Just record; nothing to do yet.
                pass

    # ---- Lifecycle steps -----------------------------------------------

    async def _handle_clarification_answer(self, project: Project,
                                           msg: PostedMessage) -> None:
        await self._draft_spec(project)

    async def _draft_spec(self, project: Project,
                          revision_feedback: Optional[str] = None) -> None:
        project.status = ProjectStatus.DESIGN_DRAFTING
        self.store.save(project)

        history = self._plan_history(project)
        previous_spec = project.latest_spec().content if project.latest_spec() else None
        prompt = prompts.prometheus_draft_spec(
            project.original_request, history,
            revision_feedback=revision_feedback, previous_spec=previous_spec,
        )
        result = await self._run_agent(project, Role.PROMETHEUS, ChannelKind.PLAN, prompt)
        version = (project.latest_spec().version + 1) if project.latest_spec() else 1
        spec = DesignSpec(version=version, content=result.summary, created_at=time.time())
        project.design_specs.append(spec)
        project.append_audit("design_spec_drafted", {"version": version})
        self.store.save(project)

        # Persist artifact
        layout = self.workspace_manager.get(project.project_id)
        (layout.design / f"v{version:02d}.md").write_text(result.summary)

        await self._post(project, ChannelKind.PLAN,
                         f"**Prometheus** posted design spec v{version}:\n\n{result.summary}")

        await self._review_spec(project, spec)

    async def _review_spec(self, project: Project, spec: DesignSpec) -> None:
        project.status = ProjectStatus.DESIGN_REVIEW
        self.store.save(project)

        prompt = prompts.argus_review(spec.content)
        result = await self._run_agent(project, Role.ARGUS, ChannelKind.PLAN, prompt)
        review = ReviewComment(spec_version=spec.version, content=result.summary,
                               created_at=time.time())
        project.review_comments.append(review)
        project.append_audit("review_comments_posted", {"spec_version": spec.version})
        self.store.save(project)

        layout = self.workspace_manager.get(project.project_id)
        (layout.reviews / f"v{spec.version:02d}.md").write_text(result.summary)

        await self._post(project, ChannelKind.PLAN,
                         f"**Argus** review of v{spec.version}:\n\n{result.summary}")

        await self._present_for_approval(project, spec, review)

    async def _present_for_approval(self, project: Project, spec: DesignSpec,
                                    review: ReviewComment) -> None:
        prompt = prompts.athena_present_for_approval(
            spec.content, review.content, project.short_id,
        )
        result = await self._run_agent(project, Role.ATHENA, ChannelKind.PLAN, prompt)
        await self._post(project, ChannelKind.PLAN,
                         f"**Athena** (please approve):\n\n{result.summary}")
        project.status = ProjectStatus.AWAITING_USER_APPROVAL
        project.append_audit("awaiting_approval", {"spec_version": spec.version})
        self.store.save(project)

    async def _handle_approval_or_revision(self, project: Project,
                                           msg: PostedMessage) -> None:
        text = msg.content.strip().lower()
        # Approval first (more specific keywords)
        if any(kw in text for kw in self.config.approval_keywords):
            await self._record_approval(project, msg)
            await self._decompose_and_dispatch(project)
            return
        if any(kw in text for kw in self.config.revision_keywords):
            project.status = ProjectStatus.REVISION_REQUESTED
            project.append_audit("revision_requested",
                                 {"feedback": msg.content})
            self.store.save(project)
            await self._post(project, ChannelKind.PLAN,
                             "**Athena**: noted, asking Prometheus to revise.")
            await self._draft_spec(project, revision_feedback=msg.content)
            return
        # Otherwise: ambiguous; nudge.
        await self._post(project, ChannelKind.PLAN,
                         "**Athena**: please reply `approve` to ship the spec, "
                         "or `revise: <what to change>` to request changes.")

    async def _record_approval(self, project: Project, msg: PostedMessage) -> None:
        spec = project.latest_spec()
        if spec is None:
            await self._post(project, ChannelKind.PLAN,
                             "**Athena**: no design spec yet, nothing to approve.")
            return
        approval = ApprovalEvent(
            spec_version=spec.version,
            approver_user_id=msg.author_id,
            approved_at=time.time(),
            message_id=msg.message_id,
        )
        project.approvals.append(approval)
        project.status = ProjectStatus.APPROVED_FOR_IMPLEMENTATION
        project.append_audit("approval_recorded",
                             {"spec_version": spec.version,
                              "approver_user_id": msg.author_id})
        self.store.save(project)
        await self._post(project, ChannelKind.PLAN,
                         f"**Athena**: approval recorded for spec v{spec.version}. "
                         "Decomposing work now.")

    # ---- Decomposition --------------------------------------------------

    async def _decompose_and_dispatch(self, project: Project) -> None:
        if not project.is_approved():
            await self._post(project, ChannelKind.PLAN,
                             "**Athena**: cannot decompose — current spec is not approved.")
            return

        spec = project.latest_spec()
        assert spec is not None
        prompt = prompts.athena_decompose(spec.content, project.short_id)
        result = await self._run_agent(project, Role.ATHENA, ChannelKind.PLAN, prompt)
        plan = self._parse_decomposition(result.summary)

        kinds: List[tuple[ChannelKind, Role, str]] = []
        if plan.get("frontend"):
            kinds.append((ChannelKind.FRONTEND, Role.APOLLO, plan.get("frontend_brief", "")))
        if plan.get("backend"):
            kinds.append((ChannelKind.BACKEND, Role.ATLAS, plan.get("backend_brief", "")))
        if plan.get("test", True):  # default true
            kinds.append((ChannelKind.TEST, Role.HEPHAESTUS, plan.get("test_brief", "")))

        project.status = ProjectStatus.WORK_DECOMPOSED
        project.append_audit("work_decomposed",
                             {"plan": plan, "tracks": [k.value for k, _, _ in kinds]})
        self.store.save(project)

        # Spin up channels and dispatch agents (in parallel, but each posts in
        # only its own channel).
        tasks = []
        for kind, role, brief in kinds:
            tasks.append(self._spawn_specialist(project, kind, role, brief, spec.content))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

        project.status = ProjectStatus.IMPLEMENTATION_IN_PROGRESS
        self.store.save(project)
        await self._post(project, ChannelKind.PLAN,
                         "**Athena**: implementation underway. Each specialist "
                         "will post status in their own channel.")

    def _parse_decomposition(self, text: str) -> Dict[str, object]:
        # Pull the first {...} block out of the text and JSON-parse it.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {"frontend": True, "backend": True, "test": True,
                    "frontend_brief": "Implement the user-facing parts.",
                    "backend_brief": "Implement the server-side parts.",
                    "test_brief": "Validate end-to-end."}
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return {"frontend": True, "backend": True, "test": True}

    async def _spawn_specialist(self, project: Project, kind: ChannelKind,
                                role: Role, brief: str, spec_text: str) -> None:
        ref = await self._ensure_channel(project, kind)
        await self.discord.send(
            ref.discord_channel_id,
            f":hammer: **Athena**: assigning **{role.display_name}** to this "
            f"`{kind.value}` channel. Brief: {brief or '(see plan channel)'}",
        )
        layout = self.workspace_manager.get(project.project_id)
        prompt = prompts.specialist_implement(
            role=role, channel_kind=kind, spec_text=spec_text, brief=brief,
            workspace_path=str(getattr(layout, kind.value, layout.source)),
        )
        result = await self._run_agent(project, role, kind, prompt)
        await self._post(project, kind,
                         f"**{role.display_name}**:\n\n{result.summary}")
        # Save artifact summary
        target_dir = getattr(layout, kind.value, layout.source)
        (target_dir / "intro.md").write_text(result.summary)

    async def _continue_specialist(self, project: Project, kind: ChannelKind,
                                   role: Role, msg: PostedMessage) -> None:
        # Continued conversation in a specialist channel — re-prompt the
        # resident agent with the user's new message as additional context.
        spec = project.latest_spec()
        layout = self.workspace_manager.get(project.project_id)
        brief = (
            f"Continue your work. The user just sent: \"{msg.content}\". "
            "Respond in this channel only."
        )
        prompt = prompts.specialist_implement(
            role=role, channel_kind=kind,
            spec_text=spec.content if spec else "(no spec)",
            brief=brief,
            workspace_path=str(getattr(layout, kind.value, layout.source)),
        )
        result = await self._run_agent(project, role, kind, prompt)
        await self._post(project, kind,
                         f"**{role.display_name}**:\n\n{result.summary}")

    # ---- Helpers --------------------------------------------------------

    async def _run_agent(self, project: Project, role: Role,
                         kind: ChannelKind, prompt: str) -> RunResult:
        run_id = f"{role.value}-{secrets.token_hex(3)}-{int(time.time())}"
        layout = self.workspace_manager.get(project.project_id)
        request = RunRequest(
            project_id=project.project_id,
            run_id=run_id,
            role=role,
            channel_kind=kind,
            prompt=prompt,
            workspace_dir=layout.source,
            log_dir=layout.logs,
        )
        task = TaskRecord(task_id=run_id, role=role, channel_kind=kind,
                          status="running")
        project.tasks.append(task)
        self.store.save(project)
        try:
            result = await self.agent_runner.run(request)
        except Exception as exc:
            task.status = "failed"
            task.summary = str(exc)
            task.finished_at = time.time()
            project.append_audit("agent_failed",
                                 {"role": role.value, "task_id": run_id,
                                  "error": str(exc)})
            self.store.save(project)
            raise
        task.status = result.status
        task.summary = (result.summary or "")[:1000]
        task.finished_at = time.time()
        project.append_audit("agent_completed",
                             {"role": role.value, "task_id": run_id,
                              "status": result.status})
        self.store.save(project)
        return result

    async def _post(self, project: Project, kind: ChannelKind, content: str) -> None:
        ref = project.channel_for(kind)
        if ref is None:
            log.warning("post: no channel for %s in project %s", kind, project.project_id)
            return
        # Discord allows up to 2000 chars per message; chunk.
        for chunk in _chunk_message(content):
            await self.discord.send(ref.discord_channel_id, chunk)

    def _record_history(self, channel_id: int, content: str) -> None:
        bucket = self._channel_history.setdefault(channel_id, [])
        bucket.append(content)
        if len(bucket) > 20:
            del bucket[: len(bucket) - 20]

    def _plan_history(self, project: Project) -> List[str]:
        plan = project.channel_for(ChannelKind.PLAN)
        if not plan:
            return []
        return list(self._channel_history.get(plan.discord_channel_id, []))


# ---------------------------------------------------------------------------


def _chunk_message(content: str, limit: int = 1900) -> List[str]:
    if len(content) <= limit:
        return [content]
    out: List[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            out.append(remaining)
            break
        # Try to break on newline near the limit.
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return out
