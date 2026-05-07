"""The Mythos orchestrator — wires Discord, projects, agents into one loop.

Public surface is ``MythosOrchestrator``. The integration tests construct
one with an ``InMemoryDiscordIO`` + stub agents and drive it via
``inject_user_message``. Production wiring constructs one with
``RealDiscordIO`` and ``CliAgent`` instances, then calls ``run_forever``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from mythos.agents import AgentInput, AgentOutput, role_tag
from mythos.config import MythosConfig
from mythos.discord_io import DiscordIO, IncomingMessage
from mythos.mediator import parse_review_verdict, parse_user_response
from mythos.project_manager import (
    BACKEND_CHANNEL,
    FRONTEND_CHANNEL,
    GENERAL_CHANNEL,
    TEST_CHANNEL,
    ProjectManager,
    ProjectRecord,
)
from mythos import prompts as P
from mythos.roles import Role
from mythos.state import ProjectPhase
from mythos.supervisor import Supervisor

logger = logging.getLogger("mythos.orchestrator")


_DECOMP_SECTION = re.compile(
    r"##\s*(Frontend|Backend|Test)\s*\([^)]*\)\s*\n(.+?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_QUESTION_RE = re.compile(r"^QUESTION:\s*(.+)$", re.MULTILINE)


@dataclass
class _DecomposedItems:
    frontend: Optional[str]
    backend: Optional[str]
    test: Optional[str]


class MythosOrchestrator:
    def __init__(
        self,
        config: MythosConfig,
        discord: DiscordIO,
        project_manager: ProjectManager,
        supervisor: Supervisor,
    ) -> None:
        self.config = config
        self.discord = discord
        self.pm = project_manager
        self.sup = supervisor
        # In-flight project tasks so we don't double-fire on user typing.
        self._inflight: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #

    def install_handlers(self) -> None:
        """Attach our message handler to the Discord IO layer."""
        self.discord.on_message(self._on_message)

    async def start(self) -> None:
        self.install_handlers()
        await self.discord.start()

    async def close(self) -> None:
        await self.discord.close()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await self.close()

    # ------------------------------------------------------------------ #
    # Inbound dispatch
    # ------------------------------------------------------------------ #

    async def _on_message(self, msg: IncomingMessage) -> None:
        """Top-level router. Three kinds of channel:

          * Main channel → start a new project.
          * A project's general channel → drive the spec/review loop.
          * A specialist channel → forward to the channel's bound agent.
        """
        if msg.is_bot:
            return

        if msg.channel_id == self.config.main_channel_id:
            await self._handle_main(msg)
            return

        rec = self.pm.project_for_channel(msg.channel_id)
        if rec is None:
            # Not a Mythos channel — silently ignore (other bots / users
            # may live in this guild).
            return

        # Question-in-own-channel discipline: messages only ever route to
        # the role bound to the channel they were posted in. The router
        # never crosses channels.
        bound_role = self.pm.role_for_channel(msg.channel_id)
        if bound_role is None:
            return

        if msg.channel_id == rec.state.channels.get(GENERAL_CHANNEL):
            await self._handle_general_channel(rec, msg)
        else:
            await self._handle_specialist_channel(rec, msg, bound_role)

    # ------------------------------------------------------------------ #
    # Main channel: kickoff
    # ------------------------------------------------------------------ #

    async def _handle_main(self, msg: IncomingMessage) -> None:
        """User dropped an idea in #main; spin up a project."""
        intake = msg.content.strip()
        if not intake:
            return
        rec = await self.pm.create_project(intake_text=intake, user_id=msg.user_id)
        slug = rec.state.slug

        await self._post(
            self.config.main_channel_id,
            Role.ATHENA,
            P.athena_intake_ack(intake, slug),
        )
        # Announce inside the project channel and kick the spec loop.
        general_id = rec.state.channels[GENERAL_CHANNEL]
        await self._post(
            general_id,
            Role.ATHENA,
            f"New project `{slug}` opened. Pinging Prometheus to draft a spec.\n\n"
            f"User request:\n> {intake}",
        )
        # Launch the draft asynchronously so the user's message handler
        # returns promptly; long subprocess work runs in the background.
        await self._spawn_phase_task(rec, self._run_draft_phase(rec))

    # ------------------------------------------------------------------ #
    # General channel: spec / review / approval
    # ------------------------------------------------------------------ #

    async def _handle_general_channel(
        self, rec: ProjectRecord, msg: IncomingMessage
    ) -> None:
        phase = rec.state.phase
        if phase == ProjectPhase.AWAITING_USER:
            verdict = parse_user_response(msg.content)
            if verdict is None:
                return
            if verdict.approved:
                self.pm.update_phase(rec.state.slug, ProjectPhase.DECOMPOSING)
                await self._post(
                    rec.state.channels[GENERAL_CHANNEL],
                    Role.ATHENA,
                    "Approval received. Decomposing the spec and opening "
                    "frontend / backend / test channels.",
                )
                await self._spawn_phase_task(rec, self._run_decompose_phase(rec))
            else:
                # Change request: revise the draft.
                if rec.state.approval_round >= self.config.max_approval_rounds:
                    self.pm.update_phase(rec.state.slug, ProjectPhase.ESCALATED)
                    await self._post(
                        rec.state.channels[GENERAL_CHANNEL],
                        Role.ATHENA,
                        "The spec has been through "
                        f"{rec.state.approval_round} change rounds — "
                        "what would you like to do? Reply 'approve' to "
                        "force-accept, or 'restart' to begin again.",
                    )
                    return
                rec.state.approval_round += 1
                await self._post(
                    rec.state.channels[GENERAL_CHANNEL],
                    Role.ATHENA,
                    "Forwarding your change request to Prometheus for revision "
                    f"(round {rec.state.approval_round}).",
                )
                await self._spawn_phase_task(
                    rec,
                    self._run_draft_phase(rec, feedback=verdict.feedback),
                )
        elif phase == ProjectPhase.DRAFTING:
            # User replied to a clarifying question — re-run the draft with
            # their reply as feedback.
            await self._spawn_phase_task(
                rec, self._run_draft_phase(rec, feedback=msg.content)
            )

    async def _run_draft_phase(
        self, rec: ProjectRecord, feedback: Optional[str] = None
    ) -> None:
        slug = rec.state.slug
        general_id = rec.state.channels[GENERAL_CHANNEL]
        self.pm.update_phase(slug, ProjectPhase.DRAFTING)

        prior = rec.workspace.read_latest_spec()
        prompt = P.prometheus_draft(rec.state.intake_text, prior, feedback)
        result = await self.sup.invoke(
            slug,
            Role.PROMETHEUS,
            AgentInput(prompt=prompt, workdir=rec.workspace.role_dir(Role.PROMETHEUS)),
        )
        if not result.ok:
            await self._post_error(general_id, Role.PROMETHEUS, result)
            return
        spec_path = rec.workspace.write_spec(result.text)
        rec.state.spec_versions.append(spec_path.name)
        await self._post_long(
            general_id,
            Role.PROMETHEUS,
            f"Draft spec ({spec_path.name}):\n\n{result.text}",
        )

        # Hand off to Argus for review.
        self.pm.update_phase(slug, ProjectPhase.REVIEWING)
        await self._post(
            general_id,
            Role.ATHENA,
            "Pinging Argus to review the draft.",
        )
        review = await self.sup.invoke(
            slug,
            Role.ARGUS,
            AgentInput(
                prompt=P.argus_review(result.text),
                workdir=rec.workspace.role_dir(Role.ARGUS),
            ),
        )
        if not review.ok:
            await self._post_error(general_id, Role.ARGUS, review)
            return
        verdict = parse_review_verdict(review.text)
        rec.state.last_review_verdict = verdict
        rec.state.last_review_text = review.text
        await self._post_long(general_id, Role.ARGUS, review.text)

        # Hand back to Athena/user for approval.
        self.pm.update_phase(slug, ProjectPhase.AWAITING_USER)
        await self._post(
            general_id,
            Role.ATHENA,
            f"Argus says **{verdict.upper()}**. Reply `approve` to proceed, "
            f"or describe what you want changed and I'll send Prometheus back "
            f"for another round.",
        )

    # ------------------------------------------------------------------ #
    # Decomposition + specialists
    # ------------------------------------------------------------------ #

    async def _run_decompose_phase(self, rec: ProjectRecord) -> None:
        slug = rec.state.slug
        general_id = rec.state.channels[GENERAL_CHANNEL]
        spec = rec.workspace.read_latest_spec() or ""

        result = await self.sup.invoke(
            slug,
            Role.ATHENA,
            AgentInput(
                prompt=P.athena_decompose(spec),
                workdir=rec.workspace.root,
            ),
        )
        if not result.ok:
            await self._post_error(general_id, Role.ATHENA, result)
            return

        items = _parse_decomposition(result.text)
        await self._post_long(
            general_id,
            Role.ATHENA,
            "Decomposition:\n\n" + result.text,
        )

        await self.pm.open_specialist_channels(slug)
        await self._post(
            general_id,
            Role.ATHENA,
            "Specialist channels opened — Apollo, Atlas, and Hephaestus are picking up their work.",
        )

        # Launch all three specialists concurrently. Each posts its own
        # status into its own channel.
        tasks = []
        if items.frontend:
            tasks.append(
                self._run_specialist(
                    rec, Role.APOLLO, FRONTEND_CHANNEL, items.frontend, spec
                )
            )
        if items.backend:
            tasks.append(
                self._run_specialist(
                    rec, Role.ATLAS, BACKEND_CHANNEL, items.backend, spec
                )
            )
        if items.test:
            tasks.append(
                self._run_specialist(
                    rec, Role.HEPHAESTUS, TEST_CHANNEL, items.test, spec
                )
            )
        self.pm.update_phase(slug, ProjectPhase.IMPLEMENTING)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pm.update_phase(slug, ProjectPhase.COMPLETE)
        await self._post(
            general_id,
            Role.ATHENA,
            "All specialists report complete. Project is implemented.",
        )

    async def _run_specialist(
        self,
        rec: ProjectRecord,
        role: Role,
        channel_slot: str,
        work_item: str,
        spec: str,
    ) -> None:
        slug = rec.state.slug
        ch_id = rec.state.channels[channel_slot]
        await self._post(
            ch_id,
            role,
            f"Picked up the work-item. Working in `{rec.workspace.role_dir(role)}`.",
        )
        result = await self.sup.invoke(
            slug,
            role,
            AgentInput(
                prompt=P.specialist_implement(role, spec, work_item),
                workdir=rec.workspace.role_dir(role),
            ),
        )
        if not result.ok:
            await self._post_error(ch_id, role, result)
            return
        await self._post_long(ch_id, role, result.text)
        # Question-in-own-channel discipline: detect QUESTION: lines and
        # re-post them as a clear ping in *this* channel only.
        for q in _QUESTION_RE.findall(result.text):
            await self._post(
                ch_id,
                role,
                f"❓ I need a clarification before I can finish: {q}",
            )

    async def _handle_specialist_channel(
        self, rec: ProjectRecord, msg: IncomingMessage, role: Role
    ) -> None:
        """User answered a question in a specialist channel — feed it back."""
        spec = rec.workspace.read_latest_spec() or ""
        await self._post(
            msg.channel_id,
            role,
            "Got it — incorporating your answer.",
        )
        result = await self.sup.invoke(
            rec.state.slug,
            role,
            AgentInput(
                prompt=P.specialist_implement(
                    role, spec, f"User clarification:\n{msg.content}"
                ),
                workdir=rec.workspace.role_dir(role),
            ),
        )
        if not result.ok:
            await self._post_error(msg.channel_id, role, result)
            return
        await self._post_long(msg.channel_id, role, result.text)

    # ------------------------------------------------------------------ #
    # Posting helpers
    # ------------------------------------------------------------------ #

    async def _post(self, channel_id: int, role: Role, body: str) -> int:
        return await self.discord.send(channel_id, f"{role_tag(role)} {body}")

    async def _post_long(self, channel_id: int, role: Role, body: str) -> None:
        # The Discord IO layer chunks at 2000 chars; we keep our role tag
        # only on the first chunk so the rest reads naturally.
        await self.discord.send(channel_id, f"{role_tag(role)}\n{body}")

    async def _post_error(self, channel_id: int, role: Role, out: AgentOutput) -> None:
        snippet = (out.stderr or out.text or "").strip().splitlines()
        tail = "\n".join(snippet[-10:]) if snippet else "(no output)"
        await self.discord.send(
            channel_id,
            f"[error] {role_tag(role)} CLI exited {out.exit_code}.\n```\n{tail}\n```",
        )

    # ------------------------------------------------------------------ #
    # Internal: schedule and track per-project background tasks.
    # ------------------------------------------------------------------ #

    async def _spawn_phase_task(
        self, rec: ProjectRecord, coro
    ) -> None:
        """Run ``coro`` as a background task keyed by project slug.

        The task is awaited inline only by tests via ``await_pending``;
        in production the user message handler returns immediately.
        """
        slug = rec.state.slug
        prior = self._inflight.get(slug)
        if prior is not None and not prior.done():
            await prior
        task = asyncio.create_task(coro)
        self._inflight[slug] = task

    async def await_pending(self, slug: Optional[str] = None) -> None:
        """Test helper: wait for a project's outstanding phase task."""
        if slug is None:
            tasks = list(self._inflight.values())
        else:
            tasks = [t for s, t in self._inflight.items() if s == slug]
        for t in tasks:
            if not t.done():
                await t


def _parse_decomposition(text: str) -> _DecomposedItems:
    sections: Dict[str, str] = {}
    for m in _DECOMP_SECTION.finditer(text or ""):
        sections[m.group(1).lower()] = m.group(2).strip()
    return _DecomposedItems(
        frontend=sections.get("frontend"),
        backend=sections.get("backend"),
        test=sections.get("test"),
    )
