"""End-to-end happy path through the orchestrator with fake transport and
fake CLI runners. Mirrors the Chrome translator extension scenario from
the user-scenario.md and spec.md.

The test exercises every state transition required by the spec:

  intake → planning (clarify) → planning → review → awaiting_approval
   → decomposing → implementing → testing → complete

Plus an isolation test (two concurrent projects don't bleed into each
other) covering SC-002 / FR-022.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mythos.agents import AgentRole
from mythos.config import load_config
from mythos.orchestrator import Orchestrator
from mythos.runners import FakeAgentRunner
from mythos.state import ProjectState, WorkstreamKind
from mythos.transport import FakeDiscordTransport


MAIN_CHANNEL_ID = 1
GUILD_ID = 999


def _build_orchestrator(responder=None, responses=None):
    cfg = load_config(path=Path("/nonexistent.yaml"))
    cfg.discord_main_channel_id = MAIN_CHANNEL_ID
    cfg.discord_guild_id = GUILD_ID
    transport = FakeDiscordTransport()
    transport.register_existing_channel(MAIN_CHANNEL_ID, "main")
    runner = FakeAgentRunner(responses=responses, responder=responder)
    orch = Orchestrator(config=cfg, transport=transport, runner=runner)
    return cfg, transport, runner, orch


async def _drain(orch: Orchestrator, *, max_loops: int = 20) -> None:
    """Wait for every background task the orchestrator spawned to complete."""
    for _ in range(max_loops):
        if not orch._tasks:
            return
        await asyncio.gather(*list(orch._tasks), return_exceptions=True)
    assert not orch._tasks, f"tasks still pending: {orch._tasks!r}"


# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_chrome_translator_extension():
    """Chrome translator extension request flows through the entire pipeline."""

    # canned responses keyed by role; we move through phases by mutating
    # them between user-message injections.
    responses = {
        AgentRole.MAIN: "PROJECT: chrome translator extension",
        AgentRole.DRAFT_PLAN: (
            "Goal: Chrome extension that translates selected text on double-click.\n"
            "User Stories: ...\n"
            "Functional Requirements: ...\n"
            "Frontend Work: popup + content script\n"
            "Backend Work: dictionary API\n"
            "Testing Plan: unit + integration"
        ),
        AgentRole.REVIEW: (
            "Summary: looks good.\nBlocking Issues: NONE\nSuggestions: none\nSTATUS: APPROVE"
        ),
        AgentRole.FRONTEND: "Built popup + content script. STATUS: DONE",
        AgentRole.BACKEND: "Built dictionary lookup endpoint. STATUS: DONE",
        AgentRole.TEST: "All tests pass. STATUS: DONE",
    }
    cfg, transport, runner, orch = _build_orchestrator(responses=responses)
    await orch.start()

    # Step 1 — user posts the project request in the main channel.
    await transport.inject_user_message(
        channel_id=MAIN_CHANNEL_ID,
        author_id=42,
        content=(
            "I want to build a Chrome extension translator: when I double-click "
            "a word it looks it up in a built-in dictionary."
        ),
    )
    await _drain(orch)

    # A project was created.
    projects = orch.registry.all()
    assert len(projects) == 1
    project = projects[0]
    assert project.project_channel_id is not None
    assert project.project_channel_name.startswith("proj-")
    # Project channel got the handoff to Prometheus.
    proj_msgs = transport.messages_in(project.project_channel_id)
    assert any("Prometheus" in m["content"] for m in proj_msgs)
    # Main-channel ack went out.
    main_msgs = transport.messages_in(MAIN_CHANNEL_ID)
    assert any("project channel" in m["content"] for m in main_msgs)
    # Spec was drafted and review came back.
    assert project.latest_spec() is not None
    assert project.latest_review() is not None
    # We're now awaiting user approval.
    assert project.state is ProjectState.AWAITING_APPROVAL

    # Step 2 — user approves in the project channel.
    await transport.inject_user_message(
        channel_id=project.project_channel_id,
        author_id=42,
        content="approve",
    )
    await _drain(orch)

    # Workstreams were created and specialists ran.
    assert project.state is ProjectState.COMPLETE
    assert {WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND, WorkstreamKind.TEST}.issubset(
        project.workstreams
    )
    for kind in (WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND, WorkstreamKind.TEST):
        ws = project.workstreams[kind]
        assert ws.status == "done", f"{kind} not done: {ws.status} {ws.last_message!r}"
        assert ws.channel_id is not None
        ws_msgs = transport.messages_in(ws.channel_id)
        assert ws_msgs, f"no messages in {kind} channel"
        joined = "\n".join(m["content"] for m in ws_msgs)
        assert "workstream complete" in joined.lower()

    # Each specialist saw the spec — assert by checking the runner trace
    # included the workstream channel name in the prompt sent to it.
    for kind, role in (
        (WorkstreamKind.FRONTEND, AgentRole.FRONTEND),
        (WorkstreamKind.BACKEND, AgentRole.BACKEND),
        (WorkstreamKind.TEST, AgentRole.TEST),
    ):
        prompts = [p for r, p, _ in runner.calls if r is role]
        assert prompts, f"no prompt sent to {role}"
        ws = project.workstreams[kind]
        assert ws.channel_name in prompts[0]

    await orch.stop()


@pytest.mark.asyncio
async def test_clarifying_question_round():
    """Vague request triggers CLARIFY then a follow-up answer produces a spec."""

    state = {"draft_call": 0}

    async def responder(role, prompt, workdir, cfg):
        if role is AgentRole.MAIN:
            return "PROJECT: vague thing"
        if role is AgentRole.DRAFT_PLAN:
            state["draft_call"] += 1
            if state["draft_call"] == 1:
                return "CLARIFY:\nWhat platform should this run on?"
            return (
                "Goal: ...\nUser Stories: ...\nFunctional Requirements: ...\n"
                "Frontend Work: ...\nBackend Work: ...\nTesting Plan: ..."
            )
        if role is AgentRole.REVIEW:
            return "Summary: ok\nBlocking Issues: NONE\nSuggestions: none\nSTATUS: APPROVE"
        return "STATUS: DONE"

    cfg, transport, runner, orch = _build_orchestrator(responder=responder)
    await orch.start()

    await transport.inject_user_message(MAIN_CHANNEL_ID, 7, "build me a thing")
    await _drain(orch)
    project = orch.registry.all()[0]
    # First spin: clarify, no spec yet.
    assert project.latest_spec() is None
    assert project.state is ProjectState.PLANNING
    proj_msgs = transport.messages_in(project.project_channel_id)
    assert any("clarification" in m["content"].lower() for m in proj_msgs)

    # User answers — second spin produces a spec, review approves, awaiting.
    await transport.inject_user_message(
        project.project_channel_id, 7, "Web app, runs in the browser."
    )
    await _drain(orch)
    assert project.latest_spec() is not None
    assert project.state is ProjectState.AWAITING_APPROVAL
    await orch.stop()


@pytest.mark.asyncio
async def test_review_blocking_triggers_revision_round():
    """Review with blocking issues sends the spec back to the draft agent."""

    state = {"draft_call": 0, "review_call": 0}

    async def responder(role, prompt, workdir, cfg):
        if role is AgentRole.MAIN:
            return "PROJECT: revising thing"
        if role is AgentRole.DRAFT_PLAN:
            state["draft_call"] += 1
            return f"Goal: v{state['draft_call']}\nFrontend Work: x\nBackend Work: y\nTesting Plan: z"
        if role is AgentRole.REVIEW:
            state["review_call"] += 1
            if state["review_call"] == 1:
                return "Summary: meh\nBlocking Issues: missing API\nSuggestions: ...\nSTATUS: REVISE"
            return "Summary: ok\nBlocking Issues: NONE\nSuggestions: none\nSTATUS: APPROVE"
        return "STATUS: DONE"

    cfg, transport, runner, orch = _build_orchestrator(responder=responder)
    await orch.start()
    await transport.inject_user_message(MAIN_CHANNEL_ID, 1, "make a tool that helps")
    await _drain(orch)
    project = orch.registry.all()[0]
    # Two specs drafted, second one approved, now awaiting user.
    assert state["draft_call"] == 2
    assert state["review_call"] == 2
    assert project.revision_count >= 1
    assert project.state is ProjectState.AWAITING_APPROVAL
    await orch.stop()


@pytest.mark.asyncio
async def test_isolation_two_concurrent_projects():
    """SC-002 / FR-022: two simultaneous projects keep separate channels and
    artifacts and don't see each other's runner calls.
    """

    async def responder(role, prompt, workdir, cfg):
        if role is AgentRole.MAIN:
            # use the prompt content to differentiate projects
            if "alpha" in prompt.lower():
                return "PROJECT: alpha service"
            return "PROJECT: beta service"
        if role is AgentRole.DRAFT_PLAN:
            tag = "alpha" if "alpha" in prompt.lower() else "beta"
            return (
                f"Goal: {tag} service\nFrontend Work: {tag} ui\n"
                f"Backend Work: {tag} api\nTesting Plan: {tag} tests"
            )
        if role is AgentRole.REVIEW:
            return "Summary: ok\nBlocking Issues: NONE\nSTATUS: APPROVE"
        # specialists must only see their own project's spec
        return f"OK ({prompt[:30]}). STATUS: DONE"

    cfg, transport, runner, orch = _build_orchestrator(responder=responder)
    await orch.start()
    await asyncio.gather(
        transport.inject_user_message(MAIN_CHANNEL_ID, 1, "build the alpha service"),
        transport.inject_user_message(MAIN_CHANNEL_ID, 2, "build the beta service"),
    )
    await _drain(orch)

    assert len(orch.registry.all()) == 2
    p_alpha = next(p for p in orch.registry.all() if "alpha" in p.request.lower())
    p_beta = next(p for p in orch.registry.all() if "beta" in p.request.lower())

    assert p_alpha.project_channel_id != p_beta.project_channel_id
    assert p_alpha.workdir != p_beta.workdir
    assert p_alpha.id != p_beta.id

    # Approve both, in parallel.
    await asyncio.gather(
        transport.inject_user_message(p_alpha.project_channel_id, 1, "approve"),
        transport.inject_user_message(p_beta.project_channel_id, 2, "approve"),
    )
    await _drain(orch)

    # Each project has its own workstream channels with its own slug.
    for project, label in ((p_alpha, "alpha"), (p_beta, "beta")):
        assert project.state is ProjectState.COMPLETE
        for kind in (WorkstreamKind.FRONTEND, WorkstreamKind.BACKEND, WorkstreamKind.TEST):
            ws = project.workstreams[kind]
            assert ws.channel_name.startswith(project.project_channel_name)
            # Cross-channel leak check: the other project's slug isn't in the name.
            other = "beta" if label == "alpha" else "alpha"
            assert other not in ws.channel_name

    # Verify the registry's per-channel routing isolates messages.
    for project in (p_alpha, p_beta):
        assert orch.registry.project_for_channel(project.project_channel_id) is project
        for ws in project.workstreams.values():
            assert orch.registry.project_for_channel(ws.channel_id) is project

    await orch.stop()


@pytest.mark.asyncio
async def test_specialist_only_responds_in_assigned_channel():
    """FR-018: a user message in a workstream channel produces a response
    in THAT channel only — no echo into the project channel or others.
    """
    responses = {
        AgentRole.MAIN: "PROJECT: little tool",
        AgentRole.DRAFT_PLAN: "Goal: x\nFrontend Work: x\nBackend Work: x\nTesting Plan: x",
        AgentRole.REVIEW: "Summary: ok\nBlocking Issues: NONE\nSTATUS: APPROVE",
        AgentRole.FRONTEND: "STATUS: DONE",
        AgentRole.BACKEND: "STATUS: DONE",
        AgentRole.TEST: "STATUS: DONE",
    }
    cfg, transport, runner, orch = _build_orchestrator(responses=responses)
    await orch.start()
    await transport.inject_user_message(MAIN_CHANNEL_ID, 5, "build a tool that does stuff")
    await _drain(orch)
    project = orch.registry.all()[0]
    await transport.inject_user_message(project.project_channel_id, 5, "approve")
    await _drain(orch)

    fe = project.workstreams[WorkstreamKind.FRONTEND]
    proj_msgs_before = len(transport.messages_in(project.project_channel_id))
    other_ws_before = len(transport.messages_in(project.workstreams[WorkstreamKind.BACKEND].channel_id))

    await transport.inject_user_message(fe.channel_id, 5, "looking good!")
    await _drain(orch)

    # Project channel + the other workstream channel did not gain any new
    # messages — the response stayed in the FE channel only.
    assert len(transport.messages_in(project.project_channel_id)) == proj_msgs_before
    assert len(transport.messages_in(project.workstreams[WorkstreamKind.BACKEND].channel_id)) == other_ws_before
    assert any(
        "Apollo:" in m["content"]
        for m in transport.messages_in(fe.channel_id)
    )
    await orch.stop()


@pytest.mark.asyncio
async def test_main_channel_non_request_is_ignored():
    """Edge case: casual main-channel chatter does not create a project."""
    responses = {AgentRole.MAIN: "IGNORE: just a hello"}
    cfg, transport, runner, orch = _build_orchestrator(responses=responses)
    await orch.start()
    await transport.inject_user_message(MAIN_CHANNEL_ID, 1, "hi everyone")
    await _drain(orch)
    assert orch.registry.all() == []
    main_msgs = transport.messages_in(MAIN_CHANNEL_ID)
    assert any("doesn't look like a project" in m["content"] for m in main_msgs)
    await orch.stop()


@pytest.mark.asyncio
async def test_agent_failure_marks_project_blocked():
    """Edge case: an agent CLI failure transitions the project to BLOCKED
    and posts a recoverable status message in the project channel.
    """
    from mythos.runners import AgentExecError

    async def responder(role, prompt, workdir, cfg):
        if role is AgentRole.MAIN:
            return "PROJECT: doomed thing"
        if role is AgentRole.DRAFT_PLAN:
            raise AgentExecError("simulated CLI failure")
        return ""

    cfg, transport, runner, orch = _build_orchestrator(responder=responder)
    await orch.start()
    await transport.inject_user_message(MAIN_CHANNEL_ID, 1, "build a doomed thing")
    await _drain(orch)
    project = orch.registry.all()[0]
    assert project.state is ProjectState.BLOCKED
    proj_msgs = transport.messages_in(project.project_channel_id)
    assert any("blocked" in m["content"].lower() for m in proj_msgs)
    await orch.stop()
