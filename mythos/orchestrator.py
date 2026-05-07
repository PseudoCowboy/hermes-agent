"""Orchestrator: drives every project through its phases.

Designed to be sync + thread-safe. The Discord adapter pushes messages
into ``handle_message``; the orchestrator owns the rest of the workflow
(invoking agents, posting outputs to channels, mutating state).

Phases (see mythos/state.py):
  INTAKE -> CLARIFYING -> DRAFTING -> REVIEWING -> AWAITING_APPROVAL ->
  DECOMPOSING -> IMPLEMENTING -> VALIDATING -> COMPLETE

Per-channel agent confinement is enforced in ``handle_message``: a message
in a workstream channel is only handled in the context of that workstream.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mythos.config import MythosConfig
from mythos.discord_adapter import DiscordAdapter, DiscordChannel, DiscordMessage
from mythos.intake import IntakeClassifier, is_approval
from mythos.roles import AgentRole, ROLE_REGISTRY
from mythos.runners import AgentRequest, AgentResult, AgentRunner
from mythos.state import (
    ProjectPhase,
    ProjectRecord,
    ProjectStore,
    ReviewRound,
    WorkstreamKind,
    WorkstreamRecord,
    WorkstreamStatus,
    new_project_id,
)

logger = logging.getLogger("mythos.orchestrator")


@dataclass
class _PendingApproval:
    project_id: str
    design_version: int


class Orchestrator:
    """Single instance owns all projects and routes messages through them."""

    def __init__(
        self,
        *,
        config: MythosConfig,
        discord: DiscordAdapter,
        runner: AgentRunner,
        store: Optional[ProjectStore] = None,
        max_workers: int = 4,
    ):
        self.config = config
        self.discord = discord
        self.runner = runner
        self.store = store or ProjectStore(config.state_dir_path())
        self.classifier = IntakeClassifier(config.request_keywords)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mythos")
        # channel_id -> project_id
        self._channel_index: Dict[str, str] = {}
        # workstream channel_id -> (project_id, workstream_kind)
        self._workstream_index: Dict[str, Tuple[str, WorkstreamKind]] = {}
        self._lock = threading.RLock()
        self._rebuild_indexes()

        config.workspace_root_path().mkdir(parents=True, exist_ok=True)
        self.discord.register_handler(self.handle_message)

    # ── index recovery ──────────────────────────────────────────────────
    def _rebuild_indexes(self) -> None:
        with self._lock:
            self._channel_index.clear()
            self._workstream_index.clear()
            for record in self.store.all():
                if record.project_channel_id:
                    self._channel_index[record.project_channel_id] = record.project_id
                for ws in record.workstreams.values():
                    if ws.channel_id:
                        self._workstream_index[ws.channel_id] = (record.project_id, ws.kind)

    # ── inbound message routing ─────────────────────────────────────────
    def handle_message(self, message: DiscordMessage) -> None:
        if message.is_bot:
            return
        try:
            self._dispatch(message)
        except Exception:  # pragma: no cover — defensive
            logger.exception("orchestrator failed handling message %s", message.message_id)

    def _dispatch(self, message: DiscordMessage) -> None:
        # 1. Main channel intake (if configured) or any unknown channel that
        #    is the configured main channel.
        if (
            self.config.discord.main_channel_id
            and message.channel_id == self.config.discord.main_channel_id
        ):
            self._on_main_channel_message(message)
            return

        # 2. Project channel
        with self._lock:
            project_id = self._channel_index.get(message.channel_id)
        if project_id:
            self._on_project_channel_message(project_id, message)
            return

        # 3. Workstream channel
        with self._lock:
            ws = self._workstream_index.get(message.channel_id)
        if ws:
            project_id, kind = ws
            self._on_workstream_channel_message(project_id, kind, message)
            return
        # else: ignore — channel isn't ours

    # ── intake ──────────────────────────────────────────────────────────
    def _on_main_channel_message(self, message: DiscordMessage) -> None:
        decision = self.classifier.classify(message.content)
        if not decision.is_project:
            return  # silently ignore per spec

        # Idempotency on the source message id.
        idem_key = f"intake:{message.message_id}"
        if not self.store.claim_idempotency(idem_key):
            return

        project_id = new_project_id()
        record = ProjectRecord(
            project_id=project_id,
            source_message_id=message.message_id,
            owner_user_id=message.author_id,
            request_text=message.content,
            main_channel_id=message.channel_id,
        )
        record.idempotency_keys["intake"] = idem_key
        self.store.create(record)

        # Create a project channel.
        channel_name = f"{self.config.discord.project_channel_prefix}{project_id[:8]}"
        try:
            channel = self.discord.create_channel(
                channel_name,
                topic=f"Mythos project {project_id} — {message.content[:80]}",
            )
        except Exception as exc:
            with self.store.edit(project_id) as rec:
                rec.phase = ProjectPhase.ERROR
                rec.error_message = f"channel creation failed: {exc}"
            self.discord.send_message(
                message.channel_id,
                f"Athena: I couldn't create a project channel for that request — {exc}",
            )
            return

        workspace = self.config.workspace_root_path() / project_id
        workspace.mkdir(parents=True, exist_ok=True)

        with self.store.edit(project_id) as rec:
            rec.project_channel_id = channel.channel_id
            rec.project_channel_name = channel.name
            rec.workspace_path = str(workspace)
            rec.phase = ProjectPhase.DRAFTING

        with self._lock:
            self._channel_index[channel.channel_id] = project_id

        self.discord.send_message(
            message.channel_id,
            (
                f"Athena: spinning up project **{project_id}** in <#{channel.channel_id}>. "
                f"Continue the conversation there."
            ),
        )
        self.discord.send_message(
            channel.channel_id,
            (
                f"Athena: welcome to project **{project_id}**.\n"
                f"Original request from {self.discord.mention(message.author_id)}:\n"
                f"> {message.content}\n\n"
                f"I'll have Prometheus draft a plan now."
            ),
        )

        # Kick off the draft-plan agent.
        self._executor.submit(self._run_drafting, project_id)

    # ── planning loop ──────────────────────────────────────────────────
    def _on_project_channel_message(self, project_id: str, message: DiscordMessage) -> None:
        record = self.store.get(project_id)
        if record is None:
            return
        owner_only = message.author_id == record.owner_user_id

        if record.phase is ProjectPhase.CLARIFYING and owner_only:
            with self.store.edit(project_id) as rec:
                rec.clarifying_answers.append(message.content)
            # If draft is already pending answers, re-run drafting.
            self.discord.send_message(
                record.project_channel_id,
                "Athena: thanks — passing your answers to Prometheus.",
            )
            self._executor.submit(self._run_drafting, project_id)
            return

        if record.phase is ProjectPhase.AWAITING_APPROVAL and owner_only:
            if is_approval(message.content):
                self._on_approval(project_id, message)
            else:
                # Treat as revision request: bump back to drafting.
                with self.store.edit(project_id) as rec:
                    rec.clarifying_answers.append(f"[revision]: {message.content}")
                    rec.phase = ProjectPhase.DRAFTING
                self.discord.send_message(
                    record.project_channel_id,
                    "Athena: noted — sending revisions back to Prometheus.",
                )
                self._executor.submit(self._run_drafting, project_id)
            return

        # Otherwise: just leave it as transcript.

    def _run_drafting(self, project_id: str) -> None:
        record = self.store.get(project_id)
        if record is None:
            return
        with self.store.edit(project_id) as rec:
            rec.phase = ProjectPhase.DRAFTING
        prometheus = self.config.agent_for(AgentRole.DRAFT_PLAN)
        prompt = self._draft_prompt(record)
        result = self.runner.run(
            AgentRequest(
                role=AgentRole.DRAFT_PLAN,
                prompt=prompt,
                workspace=Path(record.workspace_path),
                context=self._project_context(record),
            ),
            prometheus,
        )
        full_text = result.stdout.strip()
        questions = self._extract_questions(full_text)

        with self.store.edit(project_id) as rec:
            if questions and rec.phase is ProjectPhase.DRAFTING:
                rec.clarifying_questions.extend(questions)
                rec.phase = ProjectPhase.CLARIFYING
            else:
                rec.design_versions.append(full_text)
                rec.phase = ProjectPhase.REVIEWING

        # Post to channel.
        channel_id = record.project_channel_id
        for chunk in result.messages or [full_text]:
            self.discord.send_message(channel_id, f"Prometheus: {chunk}")

        record = self.store.get(project_id)  # reload
        if record.phase is ProjectPhase.CLARIFYING:
            self.discord.send_message(
                channel_id,
                "Athena: please answer the questions above so Prometheus can keep going.",
            )
            return

        # Otherwise immediately ping review agent.
        self.discord.send_message(
            channel_id,
            "Athena: design draft is ready. Pinging Argus for review.",
        )
        self._executor.submit(self._run_review, project_id)

    def _run_review(self, project_id: str) -> None:
        record = self.store.get(project_id)
        if record is None or not record.latest_design():
            return
        argus = self.config.agent_for(AgentRole.REVIEW)
        with self.store.edit(project_id) as rec:
            rec.phase = ProjectPhase.REVIEWING
        result = self.runner.run(
            AgentRequest(
                role=AgentRole.REVIEW,
                prompt=self._review_prompt(record),
                workspace=Path(record.workspace_path),
                context=self._project_context(record),
            ),
            argus,
        )
        review_text = result.stdout.strip()
        required_changes = self._extract_required_changes(review_text)
        with self.store.edit(project_id) as rec:
            round_no = len(rec.review_rounds) + 1
            rec.review_rounds.append(
                ReviewRound(
                    round_number=round_no,
                    design_version=len(rec.design_versions),
                    review_comments=review_text,
                    requested_changes=required_changes,
                    accepted=not required_changes,
                )
            )
            rec.phase = ProjectPhase.AWAITING_APPROVAL if not required_changes else ProjectPhase.DRAFTING

        for chunk in result.messages or [review_text]:
            self.discord.send_message(record.project_channel_id, f"Argus: {chunk}")

        record = self.store.get(project_id)
        if record.phase is ProjectPhase.DRAFTING:
            # Argus requested changes — bounce back to Prometheus.
            self.discord.send_message(
                record.project_channel_id,
                "Athena: Argus requested changes. Prometheus will revise.",
            )
            self._executor.submit(self._run_drafting, project_id)
        else:
            self.discord.send_message(
                record.project_channel_id,
                (
                    "Athena: review is in. Reply **approve** in this channel to start "
                    "implementation, or describe revisions."
                ),
            )

    # ── approval + decomposition ────────────────────────────────────────
    def _on_approval(self, project_id: str, message: DiscordMessage) -> None:
        with self.store.edit(project_id) as rec:
            rec.approved_design_version = len(rec.design_versions)
            rec.phase = ProjectPhase.DECOMPOSING
        self.discord.send_message(
            self.store.get(project_id).project_channel_id,
            "Athena: approval recorded. Decomposing the work.",
        )
        self._executor.submit(self._decompose_and_launch, project_id)

    def _decompose_and_launch(self, project_id: str) -> None:
        record = self.store.get(project_id)
        if record is None or record.approved_design_version is None:
            return
        design = record.design_versions[record.approved_design_version - 1]
        kinds = self._needed_workstreams(design)

        with self.store.edit(project_id) as rec:
            for kind in kinds:
                if kind.value in rec.workstreams:
                    continue
                channel_name = self.config.discord.workstream_channel_template.format(
                    project=record.project_id[:8], workstream=kind.value
                )
                idem = f"workstream-channel:{record.project_id}:{kind.value}"
                if not self.store.claim_idempotency(idem):
                    continue
                channel = self.discord.create_channel(
                    channel_name,
                    topic=f"Mythos {kind.value} workstream for project {record.project_id}",
                )
                workspace = (
                    Path(rec.workspace_path) / kind.value
                ) if rec.workspace_path else None
                if workspace:
                    workspace.mkdir(parents=True, exist_ok=True)
                rec.workstreams[kind.value] = WorkstreamRecord(
                    kind=kind,
                    channel_id=channel.channel_id,
                    channel_name=channel.name,
                    workspace_path=str(workspace) if workspace else None,
                )
                rec.idempotency_keys[idem] = idem

            rec.phase = ProjectPhase.IMPLEMENTING

        # Reload + index workstream channels.
        record = self.store.get(project_id)
        with self._lock:
            for ws in record.workstreams.values():
                if ws.channel_id:
                    self._workstream_index[ws.channel_id] = (project_id, ws.kind)

        # Announce + launch each implementation/test workstream.
        impl_kinds = [WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND]
        validation_kinds = [WorkstreamKind.TEST, WorkstreamKind.REVIEW]

        announced = []
        for kind in kinds:
            ws = record.workstreams[kind.value]
            announced.append(f"- {kind.value}: <#{ws.channel_id}>")
        self.discord.send_message(
            record.project_channel_id,
            "Athena: created workstream channels:\n" + "\n".join(announced),
        )

        for kind in kinds:
            if kind in impl_kinds:
                self._executor.submit(self._run_implementation_workstream, project_id, kind)

        # Validation workstreams (test) launch after implementation completes
        # via _on_workstream_complete(); review workstream during planning is
        # already handled.

    def _run_implementation_workstream(self, project_id: str, kind: WorkstreamKind) -> None:
        record = self.store.get(project_id)
        ws = record.workstreams.get(kind.value)
        if ws is None:
            return
        role = AgentRole.FRONTEND if kind is WorkstreamKind.FRONTEND else AgentRole.BACKEND
        agent = self.config.agent_for(role)

        # Handoff message — published to the workstream channel BEFORE the
        # agent runs, so the channel transcript shows the same packet that
        # the agent received.
        handoff = self._handoff_message(record, kind)
        with self.store.edit(project_id) as rec:
            rec.workstreams[kind.value].handoff_message = handoff
            rec.workstreams[kind.value].status = WorkstreamStatus.RUNNING
        self.discord.send_message(
            ws.channel_id,
            f"Athena: handoff for {kind.value} workstream:\n{handoff}",
        )
        self.discord.send_message(
            ws.channel_id,
            f"{agent.name}: starting work on {kind.value} task.",
        )
        result = self.runner.run(
            AgentRequest(
                role=role,
                prompt=handoff,
                workspace=Path(ws.workspace_path) if ws.workspace_path else self.config.workspace_root_path() / project_id,
                context=record.design_versions[record.approved_design_version - 1] if record.approved_design_version else "",
            ),
            agent,
        )
        text = result.stdout.strip()
        for chunk in result.messages or [text]:
            self.discord.send_message(ws.channel_id, f"{agent.name}: {chunk}")
        completion_msg = (
            f"{agent.name}: ✅ completed {kind.value} workstream "
            f"(exit={result.exit_code}, {result.duration_s:.2f}s)."
        )
        self.discord.send_message(ws.channel_id, completion_msg)

        with self.store.edit(project_id) as rec:
            target = rec.workstreams[kind.value]
            target.last_output = text
            target.run_ids.append(result.request_id)
            target.status = WorkstreamStatus.COMPLETE if result.exit_code == 0 else WorkstreamStatus.FAILED

        self._on_workstream_complete(project_id, kind)

    def _on_workstream_complete(self, project_id: str, kind: WorkstreamKind) -> None:
        record = self.store.get(project_id)
        if record is None:
            return
        # Aggregate status to the project channel.
        summary_lines = []
        for ws in record.workstreams.values():
            summary_lines.append(f"- {ws.kind.value}: {ws.status.value}")
        self.discord.send_message(
            record.project_channel_id,
            f"Athena: status update — {kind.value} just changed.\n" + "\n".join(summary_lines),
        )

        # If all impl workstreams are done, run validation (test) once.
        impl_done = all(
            record.workstreams[k.value].status
            in (WorkstreamStatus.COMPLETE, WorkstreamStatus.FAILED)
            for k in (WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND)
            if k.value in record.workstreams
        )
        if impl_done and WorkstreamKind.TEST.value in record.workstreams:
            test_ws = record.workstreams[WorkstreamKind.TEST.value]
            if test_ws.status is WorkstreamStatus.PENDING:
                self._executor.submit(self._run_validation, project_id, WorkstreamKind.TEST)
                return

        if kind is WorkstreamKind.TEST:
            self._finalize(project_id)

    def _run_validation(self, project_id: str, kind: WorkstreamKind) -> None:
        record = self.store.get(project_id)
        ws = record.workstreams.get(kind.value)
        if ws is None:
            return
        with self.store.edit(project_id) as rec:
            rec.phase = ProjectPhase.VALIDATING
            rec.workstreams[kind.value].status = WorkstreamStatus.RUNNING
        agent = self.config.agent_for(AgentRole.TEST)
        prompt = self._validation_prompt(record)
        self.discord.send_message(
            ws.channel_id,
            f"{agent.name}: running validation against the implemented workstreams.",
        )
        result = self.runner.run(
            AgentRequest(
                role=AgentRole.TEST,
                prompt=prompt,
                workspace=Path(ws.workspace_path) if ws.workspace_path else self.config.workspace_root_path() / project_id,
                context=record.design_versions[record.approved_design_version - 1] if record.approved_design_version else "",
            ),
            agent,
        )
        text = result.stdout.strip()
        defects = self._extract_defects(text)
        for chunk in result.messages or [text]:
            self.discord.send_message(ws.channel_id, f"{agent.name}: {chunk}")
        with self.store.edit(project_id) as rec:
            ws_rec = rec.workstreams[kind.value]
            ws_rec.last_output = text
            ws_rec.defects = defects
            ws_rec.run_ids.append(result.request_id)
            ws_rec.status = WorkstreamStatus.COMPLETE if not defects else WorkstreamStatus.BLOCKED
        if defects:
            # Route defects back to responsible workstreams.
            for defect in defects:
                # Default routing: test channel — operator can refine.
                target_kind = WorkstreamKind.BACKEND if "backend" in defect.lower() else WorkstreamKind.FRONTEND
                target = record.workstreams.get(target_kind.value)
                if target:
                    self.discord.send_message(
                        target.channel_id,
                        f"Athena: defect routed from {kind.value} — {defect}",
                    )
            self.discord.send_message(
                record.project_channel_id,
                f"Athena: validation found defects: {len(defects)}. Project remains open.",
            )
        else:
            self._on_workstream_complete(project_id, kind)

    def _finalize(self, project_id: str) -> None:
        record = self.store.get(project_id)
        if record is None:
            return
        with self.store.edit(project_id) as rec:
            rec.phase = ProjectPhase.COMPLETE
        lines = ["**Project complete.**", "", "Workstream summary:"]
        for ws in record.workstreams.values():
            lines.append(f"- {ws.kind.value}: {ws.status.value}")
        self.discord.send_message(record.project_channel_id, "Athena: " + "\n".join(lines))

    # ── workstream channel messages ──────────────────────────────────
    def _on_workstream_channel_message(
        self, project_id: str, kind: WorkstreamKind, message: DiscordMessage
    ) -> None:
        # Implementation agents may ask questions only here. If the user is
        # asked something inside the workstream channel and replies, just
        # log it. If a question requires user decision (escalation), the
        # main agent (Athena) is responsible — handled by _escalate_question.
        record = self.store.get(project_id)
        if record is None:
            return
        # If a workstream agent asks a question that explicitly tags the
        # owner and scope changes, escalate.
        if message.author_id == record.owner_user_id and "?" in message.content:
            return  # user clarification stays in the workstream channel

    def escalate_question(self, project_id: str, kind: WorkstreamKind, question: str) -> None:
        """Public hook for an agent runner to push a user-impacting question
        back to the project channel via Athena."""
        record = self.store.get(project_id)
        if record is None:
            return
        self.discord.send_message(
            record.project_channel_id,
            (
                f"Athena: question from {kind.value} workstream needs your input — "
                f"{question}\n(originating channel: <#{record.workstreams[kind.value].channel_id}>)"
            ),
        )

    # ── prompt builders ────────────────────────────────────────────────
    def _project_context(self, record: ProjectRecord) -> str:
        lines = [
            f"Project ID: {record.project_id}",
            f"Owner: {record.owner_user_id}",
            f"Original request: {record.request_text}",
        ]
        if record.clarifying_questions:
            lines.append("Clarifying questions asked so far:")
            for q in record.clarifying_questions:
                lines.append(f"  - {q}")
        if record.clarifying_answers:
            lines.append("User answers / revisions:")
            for a in record.clarifying_answers:
                lines.append(f"  - {a}")
        return "\n".join(lines)

    def _draft_prompt(self, record: ProjectRecord) -> str:
        if record.clarifying_questions and len(record.clarifying_answers) < len(record.clarifying_questions):
            return (
                "Ask the user only the minimum clarifying questions you still need. "
                "Phrase them as a numbered list and prefix the message with QUESTIONS:."
            )
        return (
            "Draft a design spec for the user's request. The spec MUST contain these sections:\n"
            "  1. Scope\n  2. User-facing behavior\n  3. Technical approach\n"
            "  4. Assumptions\n  5. Risks\n  6. Workstream candidates (one or more of: frontend, backend, test).\n"
            "If you still need critical information, instead reply with QUESTIONS: and a numbered list."
        )

    def _review_prompt(self, record: ProjectRecord) -> str:
        latest = record.latest_design() or ""
        return (
            "You are the review agent. Review this design spec against the original "
            "request. List required changes as `- REQUIRED CHANGE: ...` lines. If it is "
            "ready to ship, end your reply with `APPROVED`.\n\n"
            f"DESIGN SPEC:\n{latest}\n\n"
            f"ORIGINAL REQUEST:\n{record.request_text}"
        )

    def _handoff_message(self, record: ProjectRecord, kind: WorkstreamKind) -> str:
        design = record.design_versions[record.approved_design_version - 1] if record.approved_design_version else ""
        return (
            f"Workstream: {kind.value}\n"
            f"Project: {record.project_id}\n"
            f"Workspace: {record.workspace_path}\n"
            f"Approved design (version {record.approved_design_version}):\n"
            f"---\n{design}\n---\n"
            "Implement only the parts of this design that fall in your workstream. "
            "Post a completion message to your channel when done; ask any "
            "clarifying questions in your channel only."
        )

    def _validation_prompt(self, record: ProjectRecord) -> str:
        impl_outputs = []
        for kind in (WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND):
            ws = record.workstreams.get(kind.value)
            if ws and ws.last_output:
                impl_outputs.append(f"### {kind.value} output\n{ws.last_output}")
        return (
            "Run validation tests against the implemented workstreams. Report any "
            "defects as `- DEFECT: ...` lines. If everything passes, reply with `PASS`."
            "\n\n" + ("\n\n".join(impl_outputs) or "(no implementation outputs available)")
        )

    # ── parsers ─────────────────────────────────────────────────────────
    @staticmethod
    def _extract_questions(text: str) -> List[str]:
        if "QUESTIONS:" not in text.upper():
            return []
        lines = text.splitlines()
        out = []
        in_block = False
        for line in lines:
            if "QUESTIONS:" in line.upper():
                in_block = True
                continue
            if in_block:
                stripped = line.strip()
                if not stripped:
                    if out:
                        break
                    continue
                # Tolerate "1. foo", "- foo", "* foo".
                m = re.match(r"^[\d\-\*\.\)]+\s*(.*)$", stripped)
                if m and m.group(1):
                    out.append(m.group(1).strip())
                else:
                    out.append(stripped)
        return out

    @staticmethod
    def _extract_required_changes(text: str) -> List[str]:
        if "APPROVED" in text.upper():
            return []
        out = []
        for line in text.splitlines():
            m = re.match(r"^\s*[-*]\s*REQUIRED CHANGE\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                out.append(m.group(1).strip())
        return out

    @staticmethod
    def _extract_defects(text: str) -> List[str]:
        if re.search(r"\bPASS\b", text):
            return []
        out = []
        for line in text.splitlines():
            m = re.match(r"^\s*[-*]\s*DEFECT\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                out.append(m.group(1).strip())
        return out

    @staticmethod
    def _needed_workstreams(design: str) -> List[WorkstreamKind]:
        text = design.lower()
        kinds: List[WorkstreamKind] = []
        # Frontend if the design mentions any UI/extension surface.
        if any(k in text for k in ("frontend", "ui", "extension", "popup", "ux", "interface", "html", "css", "react", "browser")):
            kinds.append(WorkstreamKind.FRONTEND)
        if any(k in text for k in ("backend", "server", "api", "service", "database", "db", "endpoint", "queue")):
            kinds.append(WorkstreamKind.BACKEND)
        # Test workstream is created if the design mentions tests OR if any
        # implementation workstream exists (so completion can be validated).
        if any(k in text for k in ("test", "validation", "verify", "qa")) or kinds:
            kinds.append(WorkstreamKind.TEST)
        if not kinds:
            # Default fallback: backend + test.
            kinds = [WorkstreamKind.BACKEND, WorkstreamKind.TEST]
        return kinds

    # ── shutdown ────────────────────────────────────────────────────────
    def shutdown(self, wait: bool = True) -> None:
        # Detach our handler so a fresh orchestrator can take over the same
        # discord adapter without us trying to submit work post-shutdown.
        try:
            handlers = getattr(self.discord, "handlers", None)
            if isinstance(handlers, list) and self.handle_message in handlers:
                handlers.remove(self.handle_message)
        except Exception:  # pragma: no cover — defensive
            pass
        self._executor.shutdown(wait=wait)

    # ── debugging ───────────────────────────────────────────────────────
    def snapshot(self) -> Dict:
        return {
            "channel_index": dict(self._channel_index),
            "workstream_index": {k: (pid, kind.value) for k, (pid, kind) in self._workstream_index.items()},
            "projects": [r.to_json() for r in self.store.all()],
        }
