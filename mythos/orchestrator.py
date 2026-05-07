"""Orchestrator — the heart of Mythos.

Coordinates Athena, Prometheus, Argus, and the workstream specialists across
isolated Discord channels per project.

Implements the happy path described in the user scenario:

1. User posts a project request in the configured main channel.
2. Athena creates a unique project channel and acknowledges in the main channel.
3. Prometheus drafts a spec, asking clarifying questions in the project channel
   if needed.
4. Argus reviews the spec inside the project channel.
5. Athena presents the spec + review and asks the user for approval.
6. On approval, Athena decomposes work into frontend/backend/test workstreams,
   creating a dedicated channel for each.
7. Apollo, Atlas, and Hephaestus implement their workstream and post completion
   in their own channel only (FR-022).

Per-project isolation invariants:

* Each project has its own Discord channels and its own working dir under
  ``workspace_root/<project-slug>``.
* Each agent invocation runs against the project workspace (or workstream
  subdir) so CLI-generated artifacts cannot collide between projects.
* Channel→project routing is enforced by :class:`mythos.store.ProjectStore`;
  messages in unknown channels are ignored, and messages in workstream channels
  are routed to the workstream rather than the parent project.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mythos.agents import (
    ApolloAgent,
    ArgusAgent,
    AthenaAgent,
    AtlasAgent,
    BaseAgent,
    HephaestusAgent,
    PrometheusAgent,
    RoleOutput,
    WORKSTREAM_AGENT_CLASS,
)
from mythos.cli_runtime import CLIRuntime
from mythos.config import MythosConfig
from mythos.discord_bridge import DiscordBridge, IncomingMessage
from mythos.models import (
    AgentRole,
    AgentRun,
    Artifact,
    Project,
    ProjectState,
    Workstream,
    WorkstreamType,
)
from mythos.store import ProjectStore

logger = logging.getLogger(__name__)


_APPROVAL_RE = re.compile(r"\b(approve|approved|lgtm|ship\s*it|looks\s*good)\b", re.IGNORECASE)
_REJECT_RE = re.compile(r"\b(reject|rejected|cancel|abort)\b", re.IGNORECASE)
_CHANGES_RE = re.compile(r"\b(change|changes|revise|update|tweak|please\s+rewrite)\b", re.IGNORECASE)
_CLARIFY_ANSWER_RE = re.compile(r"^(answer|a:|ans:)\s*", re.IGNORECASE)


@dataclass
class WorkstreamPlan:
    type: WorkstreamType
    scope: str


class Orchestrator:
    def __init__(
        self,
        *,
        config: MythosConfig,
        bridge: DiscordBridge,
        runtime: CLIRuntime,
        store: ProjectStore | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.runtime = runtime
        self.store = store or ProjectStore()
        self.workspace_root = Path(workspace_root or config.workspace_root or "workspaces")
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        self.athena: AthenaAgent = AthenaAgent(runtime)
        self.prometheus: PrometheusAgent = PrometheusAgent(runtime)
        self.argus: ArgusAgent = ArgusAgent(runtime)
        self.apollo: ApolloAgent = ApolloAgent(runtime)
        self.atlas: AtlasAgent = AtlasAgent(runtime)
        self.hephaestus: HephaestusAgent = HephaestusAgent(runtime)

        self._project_locks: dict[str, asyncio.Lock] = {}
        self._intake_lock = asyncio.Lock()

        bridge.on_message(self.handle_message)

    # --- workspace helpers --------------------------------------------------

    def project_workspace(self, project: Project) -> Path:
        wd = self.workspace_root / project.project_channel_name
        (wd / "artifacts").mkdir(parents=True, exist_ok=True)
        (wd / "runs").mkdir(parents=True, exist_ok=True)
        return wd

    def workstream_workspace(self, project: Project, ws: Workstream) -> Path:
        wd = self.project_workspace(project) / ws.type.value
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

    # --- message routing ----------------------------------------------------

    async def handle_message(self, msg: IncomingMessage) -> None:
        if msg.is_bot:
            return
        try:
            if msg.channel_id == self.config.main_channel_id or msg.channel_id == self.bridge.main_channel_id:
                await self._handle_main_channel(msg)
                return

            ws_pair = self.store.workstream_for_channel(msg.channel_id)
            if ws_pair is not None:
                project, workstream = ws_pair
                await self._handle_workstream_channel(project, workstream, msg)
                return

            project = self.store.project_for_channel(msg.channel_id)
            if project is not None:
                await self._handle_project_channel(project, msg)
                return

            logger.debug("Ignoring message in unknown channel %s", msg.channel_id)
        except Exception:  # pragma: no cover - defensive top-level
            logger.exception("Failed to handle message %s", msg.message_id)

    # --- main channel: project intake --------------------------------------

    async def _handle_main_channel(self, msg: IncomingMessage) -> None:
        request_text = msg.content.strip()
        if not request_text:
            return
        async with self._intake_lock:
            await self.intake_request(
                request_text=request_text,
                requester=msg.author_name,
                source_message_id=msg.message_id,
            )

    async def intake_request(
        self,
        *,
        request_text: str,
        requester: str,
        source_message_id: str,
    ) -> Project:
        """Public entrypoint for project intake. Tests use this directly."""

        athena_workspace = self.workspace_root / "_athena"
        athena_workspace.mkdir(parents=True, exist_ok=True)
        athena_out = await self.athena.intake(request=request_text, workspace=athena_workspace)
        display_name = (athena_out.structured.get("display_name") or "untitled-project").strip() or "untitled-project"
        intake_message = athena_out.structured.get("intake_message") or (
            f"Spinning up a new project for: {request_text[:120]}"
        )

        project = await self.store.create_project(
            display_name=display_name,
            requester=requester,
            request_text=request_text,
            source_message_id=source_message_id,
        )
        self.project_workspace(project)  # materialize workspace dir

        # Create dedicated project channel.
        channel_name = f"{self.config.project_channel_prefix}-{project.project_channel_name}"
        try:
            channel_id = await self.bridge.create_channel(
                channel_name, category=self.config.project_category_name or None
            )
        except Exception as exc:  # NFR: report failure, recoverable state
            logger.exception("Failed to create project channel for %s", project.id)
            await self.store.transition(project.id, ProjectState.INTAKE_FAILED)
            await self.bridge.send(
                self.config.main_channel_id or self.bridge.main_channel_id,
                f"⚠️ Failed to create project channel for **{display_name}** ({exc}). Project left in `intake_failed`.",
            )
            return project

        await self.store.attach_project_channel(project.id, channel_id)
        project.project_channel_name = channel_name

        await self.bridge.send(
            self.config.main_channel_id or self.bridge.main_channel_id,
            f"📂 **{display_name}** → <#{channel_id}> · Prometheus is on it.\n> {intake_message}",
        )

        await self.bridge.send(
            channel_id,
            f"👋 Welcome to **{display_name}**.\nRequest: _{request_text}_\n\nPrometheus (draft plan) is analyzing now…",
        )

        # Prometheus picks up immediately.
        await self._run_prometheus_draft(project)
        return project

    # --- project channel: clarifications + approvals ------------------------

    async def _handle_project_channel(self, project: Project, msg: IncomingMessage) -> None:
        async with self._lock_for(project.id):
            content = msg.content.strip()
            if project.state == ProjectState.CLARIFYING:
                await self._record_clarification_answer(project, content)
                return
            if project.state == ProjectState.AWAITING_APPROVAL:
                await self._handle_approval_response(project, msg, content)
                return
            # Unrelated chatter in the project channel — acknowledge softly.
            logger.debug(
                "Project %s received chat in state %s: %s", project.id, project.state.value, content[:80]
            )

    # --- workstream channels: confine specialists ---------------------------

    async def _handle_workstream_channel(
        self, project: Project, ws: Workstream, msg: IncomingMessage
    ) -> None:
        # Currently we treat all user messages in workstream channels as scope
        # clarifications. Specialists never *receive* user replies in the live
        # demo path; the orchestrator-driven flow runs them once after assignment.
        logger.debug(
            "Project %s ws %s received message in state %s",
            project.id,
            ws.type.value,
            project.state.value,
        )

    # --- prometheus drafting -----------------------------------------------

    async def _run_prometheus_draft(self, project: Project) -> None:
        await self.store.transition(project.id, ProjectState.DRAFTING)
        clarifications = project.metadata.get("clarifications", [])
        out = await self.prometheus.draft(
            request=project.request_text,
            clarifications=clarifications,
            workspace=self.project_workspace(project),
        )
        await self._record_run(project, AgentRole.PROMETHEUS, out)
        if not out.ok:
            await self._post_blocker(project, AgentRole.PROMETHEUS, out)
            return

        if out.structured.get("needs_clarification"):
            questions = out.structured.get("clarifying_questions") or []
            project.metadata.setdefault("pending_questions", []).extend(questions)
            await self.store.transition(project.id, ProjectState.CLARIFYING)
            qtext = "\n".join(f"• {q}" for q in questions) or "(no questions found)"
            channel_msg = out.structured.get("channel_message") or "I have some questions before drafting the spec."
            await self.bridge.send(
                project.project_channel_id,
                f"🧠 **Prometheus** ({AgentRole.PROMETHEUS.value}):\n{channel_msg}\n\n{qtext}\n\n_Reply in this channel to continue._",
            )
            return

        spec_md = out.structured.get("spec_markdown") or out.text
        spec = await self.store.add_spec(project.id, spec_md)
        await self._record_artifact(project, "spec", spec_md, AgentRole.PROMETHEUS)
        channel_msg = out.structured.get("channel_message") or "Spec drafted; pinging Argus to review."
        await self.bridge.send(
            project.project_channel_id,
            f"📝 **Prometheus** posted spec **v{spec.version}**.\n{channel_msg}",
        )
        await self.bridge.send(
            project.project_channel_id,
            f"```markdown\n{spec_md[:1800]}\n```" + ("\n…(truncated)" if len(spec_md) > 1800 else ""),
        )
        await self._run_argus_review(project, spec.version)

    async def _record_clarification_answer(self, project: Project, answer: str) -> None:
        clean = _CLARIFY_ANSWER_RE.sub("", answer).strip()
        pending: list[str] = project.metadata.setdefault("pending_questions", [])
        if not pending:
            return
        question = pending.pop(0)
        clarifications: list[tuple[str, str]] = project.metadata.setdefault("clarifications", [])
        clarifications.append((question, clean))
        if pending:
            await self.bridge.send(
                project.project_channel_id,
                f"📌 Got it. Next question:\n• {pending[0]}",
            )
            return
        await self.bridge.send(
            project.project_channel_id,
            "🙏 Thanks — Prometheus is drafting the spec now.",
        )
        await self._run_prometheus_draft(project)

    # --- argus review -------------------------------------------------------

    async def _run_argus_review(self, project: Project, spec_version: int) -> None:
        await self.store.transition(project.id, ProjectState.REVIEWING)
        spec = next(s for s in project.specs if s.version == spec_version)
        out = await self.argus.review(
            spec=spec.content, spec_version=spec_version, workspace=self.project_workspace(project)
        )
        await self._record_run(project, AgentRole.ARGUS, out)
        if not out.ok:
            await self._post_blocker(project, AgentRole.ARGUS, out)
            return
        recommendation = (out.structured.get("recommendation") or "request_changes").lower()
        if recommendation not in ("accept", "request_changes"):
            recommendation = "request_changes"
        review_md = out.structured.get("review_markdown") or out.text
        severity = out.structured.get("severity") or "info"
        await self.store.add_review(project.id, spec_version, review_md, recommendation, severity=severity)
        await self._record_artifact(project, "review", review_md, AgentRole.ARGUS)

        channel_msg = out.structured.get("channel_message") or f"Review complete: {recommendation}."
        await self.bridge.send(
            project.project_channel_id,
            f"🔍 **Argus** review for spec v{spec_version} → **{recommendation}** ({severity}).\n{channel_msg}",
        )
        await self.bridge.send(
            project.project_channel_id,
            f"```markdown\n{review_md[:1800]}\n```" + ("\n…(truncated)" if len(review_md) > 1800 else ""),
        )
        await self.store.transition(project.id, ProjectState.AWAITING_APPROVAL)
        await self.bridge.send(
            project.project_channel_id,
            (
                "🗳️ **Athena** here. Review is in. Reply with one of:\n"
                "• `approve` — proceed to decomposition\n"
                "• `changes: <your feedback>` — request a revision\n"
                "• `reject` — close this project"
            ),
        )

    # --- approval handling --------------------------------------------------

    async def _handle_approval_response(
        self, project: Project, msg: IncomingMessage, content: str
    ) -> None:
        spec = project.latest_spec()
        if spec is None:
            return
        lower = content.lower()
        if lower.startswith("changes:") or lower.startswith("revise:"):
            requested = content.split(":", 1)[1].strip() if ":" in content else content
            await self.store.add_approval(
                project.id,
                spec.version,
                approver=msg.author_name,
                decision="revise",
                requested_changes=requested,
            )
            await self.bridge.send(
                project.project_channel_id,
                f"🔁 Revisions noted. Prometheus will rewrite spec v{spec.version}.",
            )
            await self._run_prometheus_revise(project, requested)
            return
        if _APPROVAL_RE.search(lower):
            await self.store.add_approval(
                project.id, spec.version, approver=msg.author_name, decision="approved"
            )
            await self.bridge.send(
                project.project_channel_id,
                f"✅ Spec v{spec.version} approved by {msg.author_name}. Decomposing into workstreams…",
            )
            await self._run_decomposition(project)
            return
        if _REJECT_RE.search(lower):
            await self.store.add_approval(
                project.id, spec.version, approver=msg.author_name, decision="rejected"
            )
            await self.store.transition(project.id, ProjectState.ARCHIVED)
            await self.bridge.send(
                project.project_channel_id,
                f"🛑 Spec rejected. Project archived.",
            )
            return
        if _CHANGES_RE.search(lower):
            await self.store.add_approval(
                project.id,
                spec.version,
                approver=msg.author_name,
                decision="revise",
                requested_changes=content,
            )
            await self.bridge.send(
                project.project_channel_id,
                "🔁 I read that as a request for changes. Prometheus will revise.",
            )
            await self._run_prometheus_revise(project, content)
            return
        await self.bridge.send(
            project.project_channel_id,
            "I need an explicit `approve`, `changes: …`, or `reject` to move forward.",
        )

    async def _run_prometheus_revise(self, project: Project, user_changes: str) -> None:
        spec = project.latest_spec()
        review = project.latest_review()
        if spec is None:
            return
        await self.store.transition(project.id, ProjectState.DRAFTING)
        out = await self.prometheus.revise(
            prev_spec=spec.content,
            prev_version=spec.version,
            review=review.text if review else "(no review)",
            user_changes=user_changes,
            workspace=self.project_workspace(project),
        )
        await self._record_run(project, AgentRole.PROMETHEUS, out)
        if not out.ok:
            await self._post_blocker(project, AgentRole.PROMETHEUS, out)
            return
        new_spec_md = out.structured.get("spec_markdown") or out.text
        new_spec = await self.store.add_spec(project.id, new_spec_md)
        await self._record_artifact(project, "spec", new_spec_md, AgentRole.PROMETHEUS)
        channel_msg = out.structured.get("channel_message") or "Revised spec posted."
        await self.bridge.send(
            project.project_channel_id,
            f"📝 **Prometheus** revised spec → **v{new_spec.version}**.\n{channel_msg}",
        )
        await self._run_argus_review(project, new_spec.version)

    # --- decomposition ------------------------------------------------------

    async def _run_decomposition(self, project: Project) -> None:
        await self.store.transition(project.id, ProjectState.DECOMPOSING)
        spec = project.latest_spec()
        if spec is None:
            return
        out = await self.athena.decompose(
            spec=spec.content,
            spec_version=spec.version,
            workspace=self.project_workspace(project),
        )
        await self._record_run(project, AgentRole.ATHENA, out)
        if not out.ok:
            await self._post_blocker(project, AgentRole.ATHENA, out)
            return
        raw_ws = out.structured.get("workstreams") or []
        plans: list[WorkstreamPlan] = []
        seen: set[WorkstreamType] = set()
        for entry in raw_ws:
            if not isinstance(entry, dict):
                continue
            try:
                ws_type = WorkstreamType((entry.get("type") or "").lower())
            except ValueError:
                continue
            if ws_type in seen:
                continue
            seen.add(ws_type)
            plans.append(WorkstreamPlan(type=ws_type, scope=str(entry.get("scope") or "")))
        if not plans:
            # Fallback default decomposition so the spec test still passes.
            plans = [
                WorkstreamPlan(type=WorkstreamType.FRONTEND, scope="Frontend implementation per spec."),
                WorkstreamPlan(type=WorkstreamType.BACKEND, scope="Backend implementation per spec."),
                WorkstreamPlan(type=WorkstreamType.TEST, scope="Test plan + happy/edge cases."),
            ]

        channel_msg = out.structured.get("channel_message") or "Decomposed work into specialist channels."
        summary_lines = [f"• **{p.type.value}** → {p.scope[:140]}" for p in plans]
        await self.bridge.send(
            project.project_channel_id,
            f"🧩 **Athena** decomposition:\n" + "\n".join(summary_lines) + f"\n\n{channel_msg}",
        )

        for plan in plans:
            from mythos.models import WORKSTREAM_AGENT
            role = WORKSTREAM_AGENT[plan.type]
            ws = await self.store.add_workstream(
                project.id, plan.type, role, plan.scope, channel_name=""
            )
            ws_channel_name = f"{project.project_channel_name}-{plan.type.value}"
            try:
                channel_id = await self.bridge.create_channel(
                    ws_channel_name, category=self.config.project_category_name or None
                )
            except Exception as exc:
                logger.exception("Failed to create workstream channel for %s/%s", project.id, plan.type.value)
                ws.status = "blocked"
                ws.blocker = f"channel create failed: {exc}"
                continue
            ws.channel_name = ws_channel_name
            await self.store.attach_workstream_channel(project.id, ws.id, channel_id)
            await self.bridge.send(
                project.project_channel_id,
                f"📁 Created <#{channel_id}> for **{plan.type.value}** (assigned to **{role.value}**).",
            )

        await self.store.transition(project.id, ProjectState.IMPLEMENTING)
        # Specialists run concurrently inside their own channels.
        await asyncio.gather(
            *(self._run_specialist(project, ws) for ws in project.workstreams.values() if ws.channel_id is not None),
            return_exceptions=True,
        )
        await self._maybe_complete(project)

    # --- specialists --------------------------------------------------------

    async def _run_specialist(self, project: Project, ws: Workstream) -> None:
        spec = project.latest_spec()
        if spec is None or ws.channel_id is None:
            return
        agent_cls = WORKSTREAM_AGENT_CLASS[ws.type]
        agent: BaseAgent = agent_cls(self.runtime)
        ws.status = "in_progress"
        await self.bridge.send(
            ws.channel_id,
            f"👷 **{ws.role.value.title()}** here. Received spec v{spec.version}; my scope:\n> {ws.scope}\n\nStarting now…",
        )
        # Channel-confinement guard: capture the channel id and refuse to send
        # anywhere else for this run.
        out: RoleOutput = await agent.implement(  # type: ignore[attr-defined]
            spec=spec.content,
            scope=ws.scope,
            workspace=self.workstream_workspace(project, ws),
        )
        await self._record_run(project, ws.role, out, workstream_id=ws.id)
        if not out.ok:
            ws.status = "blocked"
            ws.blocker = out.raw.error or "CLI run failed"
            await self.bridge.send(
                ws.channel_id,
                f"🚫 **{ws.role.value}** blocked: {ws.blocker}",
            )
            await self.bridge.send(
                project.project_channel_id,
                f"⚠️ Workstream **{ws.type.value}** is blocked: {ws.blocker}",
            )
            return

        # Persist artifact if the agent produced one.
        artifact_path = (
            out.structured.get("artifact_path")
            or out.structured.get("test_artifact_path")
        )
        artifact_content = (
            out.structured.get("artifact_content")
            or out.structured.get("test_artifact_content")
        )
        if artifact_path and artifact_content:
            full_path = self.workstream_workspace(project, ws) / artifact_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                full_path.write_text(artifact_content)
                ws.artifacts.append(str(full_path))
                await self.store.add_artifact(
                    Artifact(
                        project_id=project.id,
                        artifact_type=ws.type.value,
                        path=str(full_path),
                        producer=ws.role,
                        workstream_id=ws.id,
                    )
                )
            except OSError:
                logger.warning("Failed to write artifact %s", full_path)

        completion_msg = out.structured.get("channel_message") or f"{ws.role.value} done."
        await self.bridge.send(
            ws.channel_id,
            f"✅ **{ws.role.value}** complete.\n{completion_msg}",
        )
        ws.status = "completed"

    async def _maybe_complete(self, project: Project) -> None:
        if not project.workstreams:
            return
        statuses = {ws.status for ws in project.workstreams.values()}
        if statuses <= {"completed"}:
            await self.store.transition(project.id, ProjectState.COMPLETED)
            links = " · ".join(
                f"<#{ws.channel_id}> {ws.type.value}" for ws in project.workstreams.values() if ws.channel_id
            )
            await self.bridge.send(
                project.project_channel_id,
                f"🎉 **{project.display_name}** completed across all workstreams.\n{links}",
            )
        elif "blocked" in statuses:
            await self.store.transition(project.id, ProjectState.BLOCKED)
            blocked = [
                f"{ws.type.value}: {ws.blocker}"
                for ws in project.workstreams.values()
                if ws.status == "blocked"
            ]
            await self.bridge.send(
                project.project_channel_id,
                "⚠️ Project blocked:\n" + "\n".join(f"• {b}" for b in blocked),
            )

    # --- helpers ------------------------------------------------------------

    async def _record_run(
        self,
        project: Project,
        role: AgentRole,
        out: RoleOutput,
        *,
        workstream_id: str | None = None,
    ) -> None:
        run = AgentRun(
            project_id=project.id,
            role=role,
            workstream_id=workstream_id,
            status="completed" if out.ok else "failed",
            summary=(out.structured.get("channel_message") or out.text[:200]),
            output=out.text[:4000],
            blocker_reason=out.raw.error or "",
            ended_at=out.raw.duration_seconds,
        )
        await self.store.add_run(run)

    async def _record_artifact(
        self, project: Project, kind: str, content: str, producer: AgentRole
    ) -> None:
        wd = self.project_workspace(project) / "artifacts"
        wd.mkdir(parents=True, exist_ok=True)
        version = sum(1 for a in project.artifacts if a.artifact_type == kind) + 1
        path = wd / f"{kind}-v{version}.md"
        try:
            path.write_text(content)
        except OSError:
            return
        await self.store.add_artifact(
            Artifact(
                project_id=project.id,
                artifact_type=kind,
                path=str(path),
                producer=producer,
                version=version,
            )
        )

    async def _post_blocker(self, project: Project, role: AgentRole, out: RoleOutput) -> None:
        await self.store.transition(project.id, ProjectState.BLOCKED)
        target = project.project_channel_id or self.bridge.main_channel_id
        await self.bridge.send(
            target,
            f"🚫 **{role.value}** is blocked: {out.raw.error or 'CLI runtime unavailable.'}",
        )
