"""Orchestrator: drives the full Mythos workflow.

State machine, in plain English:

* User posts in main channel.
   -> Athena (Main Agent) creates a category and a #planning sub-channel,
      welcomes the user, then pings @Prometheus in #planning.

* Prometheus runs in #planning. Its output either:
   - Asks 1-3 numbered questions ending with @user, OR
   - Writes design-spec.md and posts a summary ending with @Argus.

* On @user: orchestrator simply waits for a real user reply (no dispatch
  fired until then).
* On @Argus: orchestrator dispatches Argus, who posts review comments
  ending with @Athena.

* Athena (mediator) summarises, asks user for approval ending with @user.
   - User reply containing "approve" / "lgtm" / reaction ✅ -> APPROVED.
   - Anything else -> revision loop, capped at config.max_planning_rounds.

* On approval: Athena emits a ```decomposition.json``` block. Orchestrator
  parses it, creates the matching sub-channels, and pings each
  specialist in their channel.

* Each specialist runs in its own channel. When it ends with @Athena,
  the orchestrator records completion. Once all non-test scopes that the
  test scope depends on have completed, the orchestrator pings
  @Hephaestus.

* When Hephaestus completes, project status -> DONE and Athena posts
  a one-line status update in the main channel.

This file is intentionally synchronous (call -> dispatch -> post) so the
unit tests can run deterministically without an event loop. The
production Discord adapter posts asynchronously, but each
``handle_message`` call is itself synchronous from the orchestrator's
point of view.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from mythos.agents.registry import AgentInvocation, AgentRegistry
from mythos.config import AgentSpec, MythosConfig
from mythos.discord_adapter import DiscordAdapter, DiscordMessage
from mythos.projects import Project, ProjectStatus, ProjectStore, slugify
from mythos.router import MessageRouter

logger = logging.getLogger(__name__)


_DECOMP_FENCE = re.compile(
    r"```decomposition\.json\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_GENERIC_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(\s*\[.*?\]|\s*\{.*?\})\s*\n```", re.DOTALL)
_APPROVAL_KEYWORDS = ("approve", "approved", "lgtm", "ship it")
_APPROVAL_REACTIONS = ("white_check_mark", "thumbs_up", "✅", "👍")
_DISCORD_MESSAGE_LIMIT = 2000


class Orchestrator:
    def __init__(
        self,
        *,
        config: MythosConfig,
        store: ProjectStore,
        registry: AgentRegistry,
        discord: DiscordAdapter,
        router: Optional[MessageRouter] = None,
    ):
        self._cfg = config
        self._store = store
        self._registry = registry
        self._discord = discord
        self._router = router or MessageRouter(
            config=config, store=store, registry=registry, discord=discord
        )
        self._lock = threading.RLock()

    @property
    def router(self) -> MessageRouter:
        return self._router

    # ------------------------------------------------------------------ entry

    def handle_message(self, message: DiscordMessage) -> None:
        """Top-level dispatch from the Discord listener."""

        # Ignore our own bot/agent messages for intake purposes; we only
        # process them for mention-based routing below.
        if message.channel_id == self._cfg.main_channel_id and not message.is_bot:
            self._intake_main_channel(message)
            return

        # Per-project channels: handle approval signals + agent mentions.
        project = self._store.get_by_channel(message.channel_id)
        if not project:
            return
        if project.status == ProjectStatus.ARCHIVED:
            return

        # Handle approval gate
        if (
            project.status == ProjectStatus.AWAITING_APPROVAL
            and not message.is_bot
            and self._router.channel_role_for(project, message.channel_id) == "planning"
        ):
            if self._is_approval(message):
                self._on_approval(project)
                return
            else:
                # Treat as revision request.
                self._on_revision_request(project, message)
                return

        # Mention-based dispatch
        self._dispatch_mentions(message)

    # ------------------------------------------------------------------ intake

    def _intake_main_channel(self, message: DiscordMessage) -> None:
        if not message.content.strip():
            return
        if self._store.active_count() >= self._cfg.max_concurrent_projects:
            self._post(
                self._cfg.main_channel_id,
                content=(
                    f"<@{message.author_id}> Sorry — at the configured "
                    f"max_concurrent_projects ({self._cfg.max_concurrent_projects}). "
                    "Please archive an existing project first."
                ),
                agent_role="main",
                mention_user_ids=[message.author_id],
            )
            return

        # Build a short project name from the first sentence.
        first_sentence = re.split(r"[.\n!?]", message.content.strip())[0][:48].strip()
        name = first_sentence or "untitled-project"

        project = self._store.create(
            name=name,
            requester_user_id=message.author_id,
            intake_text=message.content.strip(),
        )

        # Create the channel category + #planning channel.
        category_id = self._discord.create_category(
            guild_id=self._cfg.discord_guild_id,
            name=f"proj-{project.slug}",
        )
        planning_id = self._discord.create_sub_channel(
            guild_id=self._cfg.discord_guild_id,
            category_id=category_id,
            name="planning",
        )
        project.channel_category_id = category_id
        project.sub_channel_ids["planning"] = planning_id
        project.status = ProjectStatus.PLANNING_DRAFTING
        self._store.update(project)

        # Acknowledge in the main channel.
        self._post(
            self._cfg.main_channel_id,
            content=(
                f"<@{message.author_id}> Got it — I created **{project.name}** "
                f"({project.id}). Continuing in the project's #planning channel."
            ),
            agent_role="main",
            mention_user_ids=[message.author_id],
        )

        # Welcome message + ping Prometheus.
        intake_summary = self._truncate(project.intake_text, 1500)
        self._post(
            planning_id,
            content=(
                f"**Project:** {project.name} ({project.id})\n"
                f"**Requested by:** <@{message.author_id}>\n\n"
                f"**Intake:**\n{intake_summary}\n\n"
                f"@Prometheus please draft a design spec."
            ),
            agent_role="main",
            mention_user_ids=[message.author_id],
        )

        # Dispatch Prometheus immediately.
        self._dispatch_agent_in_channel(
            project=project,
            channel_id=planning_id,
            channel_role="planning",
            agent=self._cfg.agent_for_role("draft_plan"),
            extra_directives="Initial draft. Decide whether you need clarification.",
        )

    # ------------------------------------------------------------------ approval

    def _is_approval(self, message: DiscordMessage) -> bool:
        if any(r in _APPROVAL_REACTIONS for r in message.reactions):
            return True
        body = message.content.lower()
        return any(kw in body for kw in _APPROVAL_KEYWORDS)

    def _on_approval(self, project: Project) -> None:
        # Freeze design spec hash.
        spec_path = self._store.workspace_for(project) / "design-spec.md"
        if spec_path.exists():
            project.approved_spec_hash = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        project.status = ProjectStatus.APPROVED
        self._store.update(project)

        planning_id = project.sub_channel_ids.get("planning", "")
        self._post(
            planning_id,
            content=(
                f"Approved (spec hash `{project.approved_spec_hash[:12]}…`). "
                "I'll now decompose the work."
            ),
            agent_role="main",
        )
        self._post(
            self._cfg.main_channel_id,
            content=(
                f"**{project.name}** ({project.id}): design approved. Moving to decomposition."
            ),
            agent_role="main",
        )

        # Run the Main Agent again with a 'decompose now' directive.
        self._dispatch_agent_in_channel(
            project=project,
            channel_id=planning_id,
            channel_role="planning",
            agent=self._cfg.agent_for_role("main"),
            extra_directives=(
                "Decompose the approved design. Output a fenced block "
                "```decomposition.json``` containing an array of "
                '{"scope","summary","dependencies"}. End with @user.'
            ),
        )

    def _on_revision_request(self, project: Project, message: DiscordMessage) -> None:
        if project.planning_rounds >= self._cfg.max_planning_rounds:
            self._post(
                project.sub_channel_ids["planning"],
                content=(
                    f"We have hit the planning round cap "
                    f"({self._cfg.max_planning_rounds}). Reply with `approve`, "
                    f"`reject`, or `more rounds` to continue."
                ),
                agent_role="main",
            )
            return
        project.planning_rounds += 1
        project.status = ProjectStatus.PLANNING_DRAFTING
        self._store.update(project)

        # Re-ping Prometheus.
        planning_id = project.sub_channel_ids["planning"]
        self._dispatch_agent_in_channel(
            project=project,
            channel_id=planning_id,
            channel_role="planning",
            agent=self._cfg.agent_for_role("draft_plan"),
            extra_directives=f"Revision round {project.planning_rounds}. User feedback: {message.content!r}",
        )

    # ------------------------------------------------------------------ mention dispatch

    def _dispatch_mentions(self, message: DiscordMessage) -> None:
        agents = self._router.find_mentioned_agents(message)
        if not agents:
            return

        # Note: handoff streak only counts agent->agent pings.
        project = self._store.get_by_channel(message.channel_id)
        if not project:
            return
        if message.is_agent:
            project.handoff_streak += 1
        else:
            project.handoff_streak = 0
        self._store.update(project)

        if project.handoff_streak > self._cfg.max_handoff_streak:
            self._post(
                project.sub_channel_ids.get("planning", message.channel_id),
                content=(
                    f"Handoff streak exceeded ({self._cfg.max_handoff_streak}). "
                    f"Pausing agent-to-agent dispatch for project {project.id}; "
                    "please reply to resume."
                ),
                agent_role="main",
            )
            project.status = ProjectStatus.PAUSED
            self._store.update(project)
            return

        for agent in agents:
            routed = self._router.resolve_dispatch(message, agent)
            if not routed:
                continue
            self._dispatch_agent_in_channel(
                project=routed.project,
                channel_id=message.channel_id,
                channel_role=routed.channel_role,
                agent=agent,
            )

    # ------------------------------------------------------------------ central dispatch

    def _dispatch_agent_in_channel(
        self,
        *,
        project: Project,
        channel_id: str,
        channel_role: str,
        agent: AgentSpec,
        extra_directives: str = "",
    ) -> None:
        invocation = AgentInvocation(
            project_id=project.id,
            channel_id=channel_id,
            channel_role=channel_role,
            agent=agent,
            workspace_path=project.workspace_path,
            context_bundle=self._router.build_context_bundle(project, channel_id),
            extra_directives=extra_directives,
            metadata={
                "approved_spec_hash": project.approved_spec_hash,
                "planning_round": project.planning_rounds,
            },
        )
        try:
            result = self._registry.dispatch(invocation, timeout_seconds=self._cfg.timeout_seconds)
        except Exception as exc:
            logger.exception("dispatch failed: %s", exc)
            self._post(
                channel_id,
                content=(
                    f"{agent.display_name} (project `{project.id}`) errored: {exc}"
                ),
                agent_role=agent.role,
            )
            return

        if not result.run.ok:
            failure = "timed out" if result.run.timed_out else f"exited rc={result.run.returncode}"
            self._post(
                channel_id,
                content=(
                    f"{agent.display_name} (project `{project.id}`) {failure}."
                ),
                agent_role=agent.role,
            )
            return

        # Post the runner output, chunked to satisfy Discord's 2000-char limit.
        for chunk in self._chunk_for_discord(result.run.stdout):
            self._post(
                channel_id,
                content=chunk,
                agent_role=agent.role,
            )

        # Then progress the workflow based on what the agent did.
        self._post_dispatch_followups(project, channel_id, channel_role, agent, result.run.stdout)

    # ------------------------------------------------------------------ workflow rules

    def _post_dispatch_followups(
        self,
        project: Project,
        channel_id: str,
        channel_role: str,
        agent: AgentSpec,
        output: str,
    ) -> None:
        # Find any agent-mentions in the output and dispatch them inline so
        # the workflow advances synchronously.
        next_agents = self._extract_agent_mentions(output)

        # Draft Plan agent finished
        if agent.role == "draft_plan":
            # If it pinged the user, just wait.
            if "@user" in output and "@Argus" not in output and "@argus" not in output:
                project.status = ProjectStatus.PLANNING_CLARIFY
                self._store.update(project)
                return
            project.status = ProjectStatus.PLANNING_REVIEW
            self._store.update(project)
            self._persist_design_spec_if_present(project, output)

        # Review agent finished -> Athena summarises and asks the user.
        if agent.role == "review":
            project.status = ProjectStatus.AWAITING_APPROVAL
            self._store.update(project)
            # Drive Athena to summarise + request approval.
            self._dispatch_agent_in_channel(
                project=project,
                channel_id=channel_id,
                channel_role=channel_role,
                agent=self._cfg.agent_for_role("main"),
                extra_directives=(
                    "Summarise the design and Argus's review in 5-8 lines. "
                    "End with: 'Reply `approve` / `lgtm` to proceed, or describe changes.' "
                    "and ping @user."
                ),
            )
            return  # main agent dispatch already happened

        # Main agent: detect decomposition manifest in its output.
        if agent.role == "main" and project.status == ProjectStatus.APPROVED:
            manifest = self._extract_decomposition(output)
            if manifest is not None:
                self._apply_decomposition(project, manifest)
                return

        # Implementation agent finished -> record completion + maybe trigger test.
        if agent.role in ("frontend", "backend"):
            if "Athena" in output or "@athena" in output.lower():
                project.completion_reports[agent.role] = output[-2000:]
                self._store.update(project)
                self._maybe_trigger_test_agent(project)

        if agent.role == "test":
            if "Athena" in output or "@athena" in output.lower():
                project.completion_reports[agent.role] = output[-2000:]
                project.status = ProjectStatus.DONE
                self._store.update(project)
                self._post(
                    self._cfg.main_channel_id,
                    content=f"**{project.name}** ({project.id}): all scopes complete. ✅",
                    agent_role="main",
                )
                return

        # Generic next-agent chaining for non-special-cased roles. (E.g.
        # draft_plan -> Argus.) We dispatch each mentioned agent that is
        # *not* the user and not the agent that just spoke. Athena (main)
        # is a workflow brain and is invoked explicitly by the orchestrator,
        # so a casual "@Athena" mention in an implementation channel is a
        # completion signal, not a dispatch request.
        for next_agent in next_agents:
            if next_agent.name == agent.name:
                continue
            if next_agent.role == "main":
                continue
            # Routing constraints (channel scope etc.) still apply.
            synthetic = DiscordMessage(
                id="synthetic",
                channel_id=channel_id,
                author_id=agent.name,
                author_display_name=agent.display_name,
                content=output,
                is_bot=True,
                is_agent=True,
                agent_name=agent.name,
                mentions=[next_agent.display_name],
            )
            routed = self._router.resolve_dispatch(synthetic, next_agent)
            if not routed:
                continue
            self._dispatch_agent_in_channel(
                project=project,
                channel_id=channel_id,
                channel_role=channel_role,
                agent=next_agent,
            )

    def _extract_agent_mentions(self, output: str) -> List[AgentSpec]:
        seen: List[AgentSpec] = []
        # Reuse the router's mention index by parsing.
        import re as _re
        for token in _re.findall(r"@([A-Za-z0-9_]+)", output):
            spec = self._router._mention_index.get(token.lower())  # noqa: SLF001
            if spec is None:
                continue
            if spec in seen:
                continue
            seen.append(spec)
        return seen


    def _persist_design_spec_if_present(self, project: Project, output: str) -> None:
        # Look for a fenced block whose info string is `markdown` and whose
        # content starts with a heading matching the design-spec convention.
        match = re.search(r"```(?:markdown|md)\s*\n(# .*?)```", output, re.DOTALL)
        if match:
            (Path(project.workspace_path) / "design-spec.md").write_text(match.group(1).strip() + "\n")

    def _extract_decomposition(self, output: str) -> Optional[List[Dict[str, Any]]]:
        match = _DECOMP_FENCE.search(output)
        if match:
            blob = match.group(1)
        else:
            match = _GENERIC_JSON_FENCE.search(output)
            if not match:
                return None
            blob = match.group(1)
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        cleaned: List[Dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("scope")
            summary = entry.get("summary", "")
            deps = entry.get("dependencies", []) or []
            if scope not in ("frontend", "backend", "test"):
                continue
            cleaned.append({"scope": scope, "summary": summary, "dependencies": list(deps)})
        return cleaned or None

    def _apply_decomposition(self, project: Project, manifest: Sequence[Dict[str, Any]]) -> None:
        # Persist manifest.
        manifest_path = Path(project.workspace_path) / "decomposition.json"
        manifest_path.write_text(json.dumps(list(manifest), indent=2))
        project.decomposition_manifest_path = str(manifest_path)
        project.status = ProjectStatus.DECOMPOSED
        self._store.update(project)

        # Create sub-channels for distinct scopes.
        scopes_seen: List[str] = []
        for entry in manifest:
            scope = entry["scope"]
            if scope in scopes_seen:
                continue
            scopes_seen.append(scope)
            chid = self._discord.create_sub_channel(
                guild_id=self._cfg.discord_guild_id,
                category_id=project.channel_category_id,
                name=scope,
            )
            project.sub_channel_ids[scope] = chid
        self._store.update(project)

        # Post a human-readable summary in #planning and main channel.
        scope_lines = [
            f"- **{e['scope']}**: {e['summary']}"
            + (f" (deps: {', '.join(e['dependencies'])})" if e.get("dependencies") else "")
            for e in manifest
        ]
        summary = "Decomposition manifest:\n" + "\n".join(scope_lines)
        self._post(project.sub_channel_ids["planning"], content=summary, agent_role="main")
        self._post(self._cfg.main_channel_id, content=f"**{project.name}** decomposed. " + summary, agent_role="main")

        # Brief each non-test specialist immediately. Test waits for deps.
        project.status = ProjectStatus.IMPLEMENTING
        self._store.update(project)
        scope_to_role = {"frontend": "frontend", "backend": "backend", "test": "test"}
        for entry in manifest:
            scope = entry["scope"]
            if scope == "test":
                continue
            role = scope_to_role[scope]
            agent = self._cfg.agent_for_role(role)
            chid = project.sub_channel_ids[scope]
            briefing = (
                f"**Scope:** {scope}\n**Summary:** {entry['summary']}\n"
                f"Approved spec is at `design-spec.md` in your workspace.\n"
                f"@{agent.display_name} please implement and end with `@Athena` when done."
            )
            self._post(chid, content=briefing, agent_role="main")
            self._dispatch_agent_in_channel(
                project=project,
                channel_id=chid,
                channel_role=scope,
                agent=agent,
                extra_directives=f"Implement the {scope} scope per the brief above.",
            )

    def _maybe_trigger_test_agent(self, project: Project) -> None:
        manifest_path = Path(project.workspace_path) / "decomposition.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            return
        test_entries = [e for e in manifest if e.get("scope") == "test"]
        if not test_entries:
            return
        if "test" not in project.sub_channel_ids:
            return
        if "test" in project.completion_reports:
            return
        # Verify all dependencies are completed.
        for entry in test_entries:
            deps = entry.get("dependencies", []) or []
            if not all(dep in project.completion_reports for dep in deps):
                return

        project.status = ProjectStatus.AWAITING_TESTS
        self._store.update(project)
        agent = self._cfg.agent_for_role("test")
        chid = project.sub_channel_ids["test"]
        self._dispatch_agent_in_channel(
            project=project,
            channel_id=chid,
            channel_role="test",
            agent=agent,
            extra_directives="All implementation deps reported complete. Implement tests now.",
        )

    # ------------------------------------------------------------------ posting

    def _post(
        self,
        channel_id: str,
        *,
        content: str,
        agent_role: str = "main",
        mention_user_ids: Optional[Sequence[str]] = None,
    ) -> None:
        spec = self._cfg.agent_for_role(agent_role)
        for chunk in self._chunk_for_discord(content):
            self._discord.send_message(
                channel_id=channel_id,
                content=chunk,
                agent_name=spec.name,
                agent_display_name=spec.display_name,
                mention_user_ids=mention_user_ids,
            )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _chunk_for_discord(content: str) -> List[str]:
        if len(content) <= _DISCORD_MESSAGE_LIMIT:
            return [content]
        chunks: List[str] = []
        remaining = content
        while remaining:
            if len(remaining) <= _DISCORD_MESSAGE_LIMIT:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, _DISCORD_MESSAGE_LIMIT)
            if cut < 256:
                cut = _DISCORD_MESSAGE_LIMIT
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        return chunks

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    # ------------------------------------------------------------------ teardown

    def archive_project(self, project_id: str) -> None:
        self._store.archive(project_id)
        project = self._store.get(project_id)
        if not project:
            return
        self._post(
            self._cfg.main_channel_id,
            content=f"**{project.name}** ({project_id}) archived.",
            agent_role="main",
        )
