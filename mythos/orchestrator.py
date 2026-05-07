"""Mythos orchestrator: routes Discord events through the project lifecycle.

The orchestrator owns the full state machine. It is a single async object
that:

1. Receives :class:`mythos.discord_client.IncomingMessage` events.
2. Decides what kind of event it is based on the channel + content.
3. Drives the right agent role via the configured :class:`Runner`.
4. Posts the agent's reply back to the right Discord channel — guarded by
   :meth:`StateStore.assert_channel_in_project` to prevent cross-project
   leaks.

The state machine, by project status:

  INTAKE       — main channel post → create project channel,
                 hand off to Prometheus.
  PLANNING     — Prometheus drafts; if it asks a question, wait for user.
                 Once a design is produced, → REVIEWING.
  REVIEWING    — Argus reviews; Athena summarises and asks the user to
                 approve. → AWAITING_APPROVAL.
  AWAITING_APPROVAL — user types ``approve`` or ``reject`` in the project
                 channel. Approval → APPROVED → DECOMPOSED → IMPLEMENTING.
  IMPLEMENTING — Apollo (frontend) and Atlas (backend) run concurrently in
                 their channels. When both finish → TESTING.
  TESTING      — Hephaestus runs in the test channel. → DONE.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from mythos.config import (
    ALL_ROLES,
    MythosConfig,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    ROLE_ATHENA,
    ROLE_PROMETHEUS,
    ROLE_ARGUS,
    ROLE_HEPHAESTUS,
    ROLE_APOLLO,
    ROLE_ATLAS,
    ROLE_PROVIDERS,
)
from mythos.discord_client import (
    DiscordClient,
    IncomingMessage,
    chunk_message,
    slugify,
)
from mythos.protocol import (
    DECISION_APPROVE,
    DECISION_BLOCK,
    DECISION_REQUEST_CHANGES,
    AgentReply,
    parse_agent_output,
)
from mythos import prompts
from mythos.runners import Runner, RunResult
from mythos.state import (
    CHANNEL_TYPE_BACKEND,
    CHANNEL_TYPE_FRONTEND,
    CHANNEL_TYPE_PROJECT,
    CHANNEL_TYPE_TEST,
    AgentTask,
    CrossProjectError,
    Project,
    ProjectStatus,
    StateStore,
)
from mythos.workspace import WorkspaceManager


logger = logging.getLogger("mythos.orchestrator")


_TITLE_RE = re.compile(r"^[\s#>]*([^\n.!?]{8,80})", re.MULTILINE)


def _derive_title_and_slug(content: str) -> tuple[str, str]:
    m = _TITLE_RE.search(content)
    title = (m.group(1).strip() if m else content[:60].strip()) or "new-project"
    slug = slugify(title)
    return title, slug


@dataclass
class _ProjectRuntime:
    """Per-project mutable runtime: pending Q&A, in-flight tasks."""

    pending_questions: List[str] = field(default_factory=list)
    pending_answers: List[str] = field(default_factory=list)
    frontend_summary: str = ""
    backend_summary: str = ""
    awaiting_user_in_project_channel: bool = False


class Orchestrator:
    def __init__(
        self,
        config: MythosConfig,
        discord: DiscordClient,
        runner: Runner,
        state: Optional[StateStore] = None,
        workspaces: Optional[WorkspaceManager] = None,
    ) -> None:
        self.config = config
        self.discord = discord
        self.runner = runner
        self.state = state or StateStore(config.state_dir)
        self.workspaces = workspaces or WorkspaceManager(config.workspace_root)
        self._runtimes: Dict[str, _ProjectRuntime] = {}
        # Self-register so the client forwards incoming messages.
        self.discord.on_message(self.on_message)

    # ------------------------------------------------------------------
    # Public message intake

    async def on_message(self, msg: IncomingMessage) -> None:
        if msg.is_bot:
            return  # never react to our own posts
        try:
            if msg.channel_id == self.discord.main_channel_id:
                await self._handle_main_channel(msg)
                return
            project = self.state.project_by_channel(msg.channel_id)
            if project is None:
                logger.debug("ignoring message in unknown channel %s", msg.channel_id)
                return
            await self._handle_project_channel(project, msg)
        except CrossProjectError as exc:
            logger.warning("cross-project block: %s", exc)
        except Exception:
            logger.exception("orchestrator failure handling message %s", msg.message_id)

    # ------------------------------------------------------------------
    # Main channel

    async def _handle_main_channel(self, msg: IncomingMessage) -> None:
        content = msg.content.strip()
        if not content:
            return
        # Idempotent — re-handling the same source message returns the existing project.
        existing = self.state.project_by_source_message(msg.message_id)
        if existing is not None:
            await self._post_main_ack(existing, msg)
            return
        title, slug = _derive_title_and_slug(content)
        async with self.state.lock(f"intake:{msg.message_id}"):
            existing = self.state.project_by_source_message(msg.message_id)
            if existing is not None:
                await self._post_main_ack(existing, msg)
                return
            workspace = self.workspaces.allocate(slug, slug)  # placeholder; redo with real id
            # Reallocate using project id once we have one.
            project = self.state.create_project(
                slug=slug,
                title=title,
                source_message_id=msg.message_id,
                owner_user_id=msg.author_id,
                workspace_path=str(workspace),  # overridden right below
            )
            real_ws = self.workspaces.allocate(project.slug, project.id)
            project.workspace_path = str(real_ws)
            self.state.append_audit(
                project.id, "workspace_allocated", {"path": str(real_ws)}
            )

            # Create the project channel.
            channel_id = await self.discord.create_project_channel(
                name=f"proj-{project.slug}",
                topic=f"Mythos project: {project.title}",
            )
            self.state.attach_project_channel(project.id, channel_id)
            self._runtimes[project.id] = _ProjectRuntime()
            self.state.set_status(project.id, ProjectStatus.PLANNING)
            self.state.save(project.id)

        await self._post_main_ack(project, msg)
        # Athena ack inside the project channel, then hand off to Prometheus.
        await self._run_athena_intake(project, content)
        await self._run_prometheus_draft(project, content)

    async def _post_main_ack(self, project: Project, msg: IncomingMessage) -> None:
        ack = (
            f"Got it, <@{msg.author_id}> — created project channel "
            f"<#{project.project_channel_id}> for **{project.title}** "
            f"(`{project.slug}`)."
        )
        await self._send(self.discord.main_channel_id, ack, project_id=None)

    # ------------------------------------------------------------------
    # Project / task channels

    async def _handle_project_channel(
        self, project: Project, msg: IncomingMessage
    ) -> None:
        # Approval gate — only the project channel accepts approve/reject.
        if msg.channel_id == project.project_channel_id:
            text = msg.content.strip().lower()
            if project.status == ProjectStatus.AWAITING_APPROVAL:
                if text in {"approve", "approved", "lgtm", "ship it"}:
                    await self._handle_user_approval(project, msg.author_id, True)
                    return
                if text in {"reject", "rejected", "changes", "needs work"}:
                    await self._handle_user_approval(project, msg.author_id, False)
                    return
            # Treat any other user text in the project channel as an answer to
            # a pending Prometheus question if there is one.
            runtime = self._runtimes.setdefault(project.id, _ProjectRuntime())
            if runtime.awaiting_user_in_project_channel and runtime.pending_questions:
                runtime.pending_answers.append(msg.content.strip())
                runtime.awaiting_user_in_project_channel = False
                await self._run_prometheus_draft(
                    project,
                    project.design_versions[0].body
                    if project.design_versions
                    else "(see channel history)",
                )
                return
        # Task channel messages: by default the implementation agents are
        # one-shot; we let the user reply but don't loop endlessly.
        # (Future: attach answers as follow-up turns. For now, just log.)
        return

    # ------------------------------------------------------------------
    # Phase 1: Athena intake (inside project channel)

    async def _run_athena_intake(self, project: Project, user_request: str) -> None:
        prompt = prompts.athena_intake_prompt(user_request, project.slug)
        result = await self._run_role(project, ROLE_ATHENA, prompt, CHANNEL_TYPE_PROJECT)
        if result is None:
            return
        await self._post_to_project_channel(project, ROLE_ATHENA, result.visible_text)

    # ------------------------------------------------------------------
    # Phase 2: Prometheus draft

    async def _run_prometheus_draft(self, project: Project, user_request: str) -> None:
        runtime = self._runtimes.setdefault(project.id, _ProjectRuntime())
        # Combine pending Q&A pairs (oldest-first) into a flat list for the prompt.
        pairs: List[str] = []
        for q, a in zip(runtime.pending_questions, runtime.pending_answers):
            pairs.append(f"Q: {q}\n  A: {a}")
        prompt = prompts.prometheus_draft_prompt(
            user_request=user_request,
            project_slug=project.slug,
            prior_questions_and_answers=pairs or None,
        )
        result = await self._run_role(
            project, ROLE_PROMETHEUS, prompt, CHANNEL_TYPE_PROJECT
        )
        if result is None:
            return
        await self._post_to_project_channel(project, ROLE_PROMETHEUS, result.visible_text)
        if result.design_body:
            self.state.add_design_version(
                project.id, result.design_body, ROLE_PROMETHEUS
            )
            self.state.set_status(project.id, ProjectStatus.REVIEWING)
            self.state.save(project.id)
            await self._run_argus_review(project)
        elif result.asks_question:
            # Capture each question line so the next turn can stitch in answers.
            qs = [
                line.strip()
                for line in result.visible_text.splitlines()
                if line.strip().endswith("?")
            ] or [result.visible_text.strip()]
            runtime.pending_questions.extend(qs)
            runtime.awaiting_user_in_project_channel = True
            self.state.append_audit(project.id, "prometheus_question", {"count": len(qs)})

    # ------------------------------------------------------------------
    # Phase 3: Argus review + Athena summary

    async def _run_argus_review(self, project: Project) -> None:
        latest = project.latest_design()
        assert latest is not None
        prompt = prompts.argus_review_prompt(latest.body, project.slug, latest.version)
        result = await self._run_role(
            project, ROLE_ARGUS, prompt, CHANNEL_TYPE_PROJECT
        )
        if result is None:
            return
        decision = result.decision or DECISION_REQUEST_CHANGES
        self.state.add_review(
            project.id, latest.version, result.visible_text, decision
        )
        self.state.save(project.id)
        await self._post_to_project_channel(project, ROLE_ARGUS, result.visible_text)
        await self._athena_summarise_for_user(project, latest.body, result.visible_text, decision)

    async def _athena_summarise_for_user(
        self,
        project: Project,
        design_body: str,
        review_comments: str,
        decision: str,
    ) -> None:
        prompt = prompts.athena_decision_prompt(
            design_body, review_comments, decision
        )
        result = await self._run_role(
            project, ROLE_ATHENA, prompt, CHANNEL_TYPE_PROJECT
        )
        if result is None:
            return
        await self._post_to_project_channel(project, ROLE_ATHENA, result.visible_text)

        if decision == DECISION_APPROVE:
            self.state.set_status(project.id, ProjectStatus.AWAITING_APPROVAL)
            if self.config.discord.approval_hint:
                await self._post_to_project_channel(
                    project,
                    ROLE_ATHENA,
                    "Type `approve` to start implementation, or `reject` to send the design back.",
                )
        elif decision == DECISION_REQUEST_CHANGES:
            # Loop back to Prometheus for revision.
            self.state.set_status(project.id, ProjectStatus.PLANNING)
            self.state.save(project.id)
            user_request = (
                project.design_versions[0].body
                if project.design_versions
                else project.title
            )
            revision_prompt = prompts.prometheus_draft_prompt(
                user_request=user_request,
                project_slug=project.slug,
                review_feedback=review_comments,
                previous_design=design_body,
            )
            rev_result = await self._run_role(
                project, ROLE_PROMETHEUS, revision_prompt, CHANNEL_TYPE_PROJECT
            )
            if rev_result and rev_result.design_body:
                self.state.add_design_version(
                    project.id, rev_result.design_body, ROLE_PROMETHEUS
                )
                self.state.set_status(project.id, ProjectStatus.REVIEWING)
                self.state.save(project.id)
                await self._post_to_project_channel(
                    project, ROLE_PROMETHEUS, rev_result.visible_text
                )
                await self._run_argus_review(project)
        else:
            # block — leave in awaiting_approval so user can intervene.
            self.state.set_status(project.id, ProjectStatus.AWAITING_APPROVAL)

        self.state.save(project.id)

    # ------------------------------------------------------------------
    # Phase 4: Approval

    async def _handle_user_approval(
        self, project: Project, user_id: int, approved: bool
    ) -> None:
        latest = project.latest_design()
        if latest is None:
            return
        decision = "approved" if approved else "rejected"
        self.state.record_approval(project.id, latest.version, user_id, decision)
        if not approved:
            self.state.set_status(project.id, ProjectStatus.PLANNING)
            self.state.save(project.id)
            await self._post_to_project_channel(
                project,
                ROLE_ATHENA,
                "Got it — sending the design back for revision.",
            )
            return
        self.state.set_status(project.id, ProjectStatus.APPROVED)
        self.state.save(project.id)
        await self._post_to_project_channel(
            project, ROLE_ATHENA, "Approved! Decomposing work into channels."
        )
        await self._decompose_and_implement(project)

    # ------------------------------------------------------------------
    # Phase 5: Decompose + run implementation agents

    async def _decompose_and_implement(self, project: Project) -> None:
        latest = project.latest_design()
        assert latest is not None

        # Create the three task channels.
        slug = project.slug
        fe_chan = await self.discord.create_task_channel(
            slug, CHANNEL_TYPE_FRONTEND, topic=f"Frontend tasks for {project.title}"
        )
        be_chan = await self.discord.create_task_channel(
            slug, CHANNEL_TYPE_BACKEND, topic=f"Backend tasks for {project.title}"
        )
        te_chan = await self.discord.create_task_channel(
            slug, CHANNEL_TYPE_TEST, topic=f"Validation for {project.title}"
        )
        self.state.attach_task_channel(
            project.id, fe_chan, CHANNEL_TYPE_FRONTEND, [ROLE_APOLLO]
        )
        self.state.attach_task_channel(
            project.id, be_chan, CHANNEL_TYPE_BACKEND, [ROLE_ATLAS]
        )
        self.state.attach_task_channel(
            project.id, te_chan, CHANNEL_TYPE_TEST, [ROLE_HEPHAESTUS]
        )
        self.state.set_status(project.id, ProjectStatus.DECOMPOSED)
        self.state.save(project.id)

        ws_root = Path(project.workspace_path)
        fe_ws = self.workspaces.subdir(ws_root, "frontend")
        be_ws = self.workspaces.subdir(ws_root, "backend")
        te_ws = self.workspaces.subdir(ws_root, "test")

        await self._post_to_project_channel(
            project,
            ROLE_ATHENA,
            f"Channels ready — frontend <#{fe_chan}>, backend <#{be_chan}>, test <#{te_chan}>.",
        )

        self.state.set_status(project.id, ProjectStatus.IMPLEMENTING)
        self.state.save(project.id)

        # Frontend + backend run concurrently, each in their own channel.
        fe_task = self.state.add_task(
            project.id,
            ROLE_APOLLO,
            ROLE_PROVIDERS[ROLE_APOLLO],
            fe_chan,
            str(fe_ws),
            latest.version,
            "Implement frontend per design",
        )
        be_task = self.state.add_task(
            project.id,
            ROLE_ATLAS,
            ROLE_PROVIDERS[ROLE_ATLAS],
            be_chan,
            str(be_ws),
            latest.version,
            "Implement backend per design",
        )
        self.state.save(project.id)

        fe_prompt = prompts.apollo_frontend_prompt(slug, latest.body, str(fe_ws))
        be_prompt = prompts.atlas_backend_prompt(slug, latest.body, str(be_ws))

        fe_coro = self._run_task(project, fe_task, fe_prompt, CHANNEL_TYPE_FRONTEND)
        be_coro = self._run_task(project, be_task, be_prompt, CHANNEL_TYPE_BACKEND)

        fe_reply, be_reply = await asyncio.gather(fe_coro, be_coro)

        runtime = self._runtimes.setdefault(project.id, _ProjectRuntime())
        runtime.frontend_summary = fe_reply.visible_text if fe_reply else ""
        runtime.backend_summary = be_reply.visible_text if be_reply else ""

        # Test phase.
        self.state.set_status(project.id, ProjectStatus.TESTING)
        self.state.save(project.id)

        te_task = self.state.add_task(
            project.id,
            ROLE_HEPHAESTUS,
            ROLE_PROVIDERS[ROLE_HEPHAESTUS],
            te_chan,
            str(te_ws),
            latest.version,
            "Validate the implementation",
        )
        self.state.save(project.id)

        te_prompt = prompts.hephaestus_test_prompt(
            slug,
            latest.body,
            str(te_ws),
            runtime.frontend_summary,
            runtime.backend_summary,
        )
        await self._run_task(project, te_task, te_prompt, CHANNEL_TYPE_TEST)

        self.state.set_status(project.id, ProjectStatus.DONE)
        self.state.save(project.id)
        await self._post_to_project_channel(
            project,
            ROLE_ATHENA,
            f"All phases complete for **{project.title}**.",
        )

    # ------------------------------------------------------------------
    # Generic role execution

    async def _run_role(
        self,
        project: Project,
        role: str,
        prompt: str,
        channel_kind: str,
    ) -> Optional[AgentReply]:
        binding = project.channels.get(channel_kind)
        if binding is None:
            logger.error("project %s missing %s channel", project.id, channel_kind)
            return None
        if role not in binding.allowed_roles and channel_kind != CHANNEL_TYPE_PROJECT:
            # Project channel is shared between athena/prometheus/argus.
            logger.error(
                "role %s not allowed in %s channel", role, channel_kind
            )
            return None
        # Confirm provider mapping holds.
        expected_provider = ROLE_PROVIDERS[role]
        actual_provider = ROLE_PROVIDERS.get(role)
        if expected_provider != actual_provider:
            raise RuntimeError(
                f"role/provider mismatch: {role} -> {actual_provider} (expected {expected_provider})"
            )
        ws = Path(project.workspace_path) / channel_kind
        if not ws.exists():
            ws = Path(project.workspace_path)
        result = await self.runner.run(role, prompt, ws)
        return parse_agent_output(result.output) if result.output else parse_agent_output(
            f"<<MYTHOS:STATUS:failed>> {result.error or 'no output'}"
        )

    async def _run_task(
        self,
        project: Project,
        task: AgentTask,
        prompt: str,
        channel_kind: str,
    ) -> Optional[AgentReply]:
        ws = Path(task.workspace_path)
        # Cross-project guard: this task's channel MUST belong to its project.
        self.state.assert_channel_in_project(project.id, task.channel_id)
        # And it must NOT belong to any other project.
        self.state.ensure_no_cross_project_routing(
            project.id, [task.channel_id]
        )
        self.state.update_task(project.id, task.task_id, status="running")
        self.state.save(project.id)
        await self._send(
            task.channel_id,
            f"**{task.role.title()}** picked up: {task.instructions}",
            project_id=project.id,
        )
        result: RunResult = await self.runner.run(task.role, prompt, ws)
        reply = parse_agent_output(result.output or "")
        if not result.output:
            reply = parse_agent_output(
                f"<<MYTHOS:STATUS:failed>> {result.error or 'no output'}"
            )
        await self._send(
            task.channel_id,
            self._format_agent_post(task.role, reply.visible_text),
            project_id=project.id,
        )
        status = "finished" if reply.finished and not reply.failed else (
            "failed" if reply.failed else "questioning" if reply.asks_question else "finished"
        )
        self.state.update_task(
            project.id,
            task.task_id,
            status=status,
            output_summary=reply.visible_text[:1000],
        )
        self.state.save(project.id)
        return reply

    # ------------------------------------------------------------------
    # Discord helpers

    async def _post_to_project_channel(
        self, project: Project, role: str, text: str
    ) -> None:
        if project.project_channel_id is None:
            return
        await self._send(
            project.project_channel_id,
            self._format_agent_post(role, text),
            project_id=project.id,
        )

    def _format_agent_post(self, role: str, text: str) -> str:
        text = text.strip()
        if not text:
            text = "(no content)"
        return f"**{role.title()}:** {text}"

    async def _send(
        self,
        channel_id: int,
        content: str,
        project_id: Optional[str],
    ) -> None:
        # Cross-project leak guard for non-main posts.
        if project_id is not None:
            try:
                self.state.assert_channel_in_project(project_id, channel_id)
            except CrossProjectError as exc:
                logger.warning("blocked cross-project post: %s", exc)
                self.state.append_audit(
                    project_id,
                    "send_blocked_cross_project",
                    {"channel_id": channel_id, "reason": str(exc)},
                )
                return
        for chunk in chunk_message(content, self.config.max_message_chars):
            await self.discord.send_message(channel_id, chunk)
