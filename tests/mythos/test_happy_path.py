"""End-to-end integration tests covering the user-scenario happy path.

The full flow simulated here:

* user → main channel: project request
* Athena creates project channel, posts ack
* Prometheus drafts spec (with a clarification round)
* Argus reviews, posts request_changes the first round
* user replies `changes: …` → Prometheus revises
* Argus re-reviews → accept
* user replies `approve`
* Athena decomposes → frontend / backend / test channels
* Apollo, Atlas, Hephaestus implement and post completion in their own channel
* Project transitions to COMPLETED with artifacts persisted

Plus the spec's isolation invariant: two simultaneous projects each get
distinct channels and never cross-contaminate state.
"""

from __future__ import annotations

import asyncio

import pytest

from mythos.cli_runtime import CLIRuntime
from mythos.config import MythosConfig, load_config
from mythos.discord_bridge import InMemoryDiscordBridge
from mythos.models import AgentRole, ProjectState, WorkstreamType
from mythos.orchestrator import Orchestrator
from mythos.store import ProjectStore

from tests.mythos._responders import (
    argus_responder,
    athena_decompose_responder,
    athena_intake_responder,
    failing_responder,
    prometheus_responder,
    prometheus_sequenced_responder,
    specialist_responder,
)


def _build_runtime(cfg: MythosConfig) -> CLIRuntime:
    return CLIRuntime(agent_configs=cfg.agents)


def _wire_happy_path(
    runtime: CLIRuntime,
    *,
    needs_clarification: bool = False,
    first_review: str = "accept",
    second_review: str = "accept",
):
    runtime.set_mock(AgentRole.ATHENA, athena_intake_responder())
    if needs_clarification:
        runtime.set_mock(
            AgentRole.PROMETHEUS,
            prometheus_sequenced_responder(
                [
                    {
                        "needs_clarification": True,
                        "clarifying_questions": ["Which languages should be supported?"],
                        "channel_message": "Quick clarification first.",
                    },
                    {
                        "needs_clarification": False,
                        "spec_markdown": "## Spec v1\nTranslator extension supports en↔es.",
                        "channel_message": "Spec drafted.",
                    },
                    # second-round revision
                    {
                        "spec_markdown": "## Spec v2\nRevised per feedback.",
                        "channel_message": "Spec revised.",
                    },
                ]
            ),
        )
    else:
        runtime.set_mock(
            AgentRole.PROMETHEUS,
            prometheus_sequenced_responder(
                [
                    {
                        "needs_clarification": False,
                        "spec_markdown": "## Spec v1\nTranslator extension.",
                        "channel_message": "Spec drafted.",
                    },
                    {
                        "spec_markdown": "## Spec v2\nRevised per feedback.",
                        "channel_message": "Spec revised.",
                    },
                ]
            ),
        )
    # Argus alternates if the first review is request_changes.
    review_state = {"i": 0}
    review_seq = [first_review, second_review]
    base_argus = argus_responder()

    def argus_seq(invocation):
        rec = review_seq[min(review_state["i"], len(review_seq) - 1)]
        review_state["i"] += 1
        # Build a fresh responder per call (different recommendation).
        return argus_responder(recommendation=rec, review="Review notes.")(invocation)

    runtime.set_mock(AgentRole.ARGUS, argus_seq)
    # Athena decompose uses a different intake responder; we reset on the second call.
    intake_state = {"i": 0}

    def athena_dispatch(invocation):
        if intake_state["i"] == 0:
            intake_state["i"] += 1
            return athena_intake_responder()(invocation)
        return athena_decompose_responder()(invocation)

    runtime.set_mock(AgentRole.ATHENA, athena_dispatch)
    runtime.set_mock(
        AgentRole.APOLLO,
        specialist_responder(AgentRole.APOLLO, artifact_path="src/popup.tsx", artifact_content="// popup"),
    )
    runtime.set_mock(
        AgentRole.ATLAS,
        specialist_responder(AgentRole.ATLAS, artifact_path="src/lookup.py", artifact_content="# lookup"),
    )
    runtime.set_mock(
        AgentRole.HEPHAESTUS,
        specialist_responder(
            AgentRole.HEPHAESTUS, artifact_path="tests/test_translator.py", artifact_content="def test(): pass"
        ),
    )


@pytest.mark.asyncio
async def test_happy_path_one_round(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000, main_channel_name="general")
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents  # default agent CLI configs
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)

    store = ProjectStore()
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, store=store, workspace_root=tmp_path)

    # 1. user posts in main channel
    await bridge.inject_user_message(
        channel_id=1000, content="Build a Chrome translator extension that triggers on double-click."
    )

    # 2. project channel exists, Athena posted in main, Prometheus posted in project
    projects = store.all()
    assert len(projects) == 1
    project = projects[0]
    assert project.project_channel_id is not None
    assert project.state == ProjectState.AWAITING_APPROVAL  # spec drafted, reviewed (accept), waiting

    main_msgs = bridge.messages_in(1000)
    assert any("Translator Extension" in m["content"] for m in main_msgs)
    assert any(
        f"<#{project.project_channel_id}>" in m["content"] for m in main_msgs
    ), "Athena should link the new project channel from main"

    proj_msgs = bridge.messages_in(project.project_channel_id)
    assert any("Prometheus" in m["content"] for m in proj_msgs)
    assert any("Argus" in m["content"] for m in proj_msgs)
    assert any("approve" in m["content"].lower() for m in proj_msgs)

    # 3. user approves → decomposition runs
    await bridge.inject_user_message(channel_id=project.project_channel_id, content="approve")

    # 4. workstream channels created, specialists ran, completion posted
    project = store.get(project.id)
    assert project.state == ProjectState.COMPLETED
    types_seen = {ws.type for ws in project.workstreams.values()}
    assert types_seen == {WorkstreamType.FRONTEND, WorkstreamType.BACKEND, WorkstreamType.TEST}
    for ws in project.workstreams.values():
        assert ws.channel_id is not None
        assert ws.status == "completed"
        ws_msgs = bridge.messages_in(ws.channel_id)
        assert any("Received spec" in m["content"] for m in ws_msgs)
        assert any("complete" in m["content"].lower() for m in ws_msgs)

    # 5. Final summary appears in project channel
    proj_msgs = bridge.messages_in(project.project_channel_id)
    assert any("completed across all workstreams" in m["content"] for m in proj_msgs)


@pytest.mark.asyncio
async def test_happy_path_with_clarification_and_revision(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime, needs_clarification=True, first_review="request_changes", second_review="accept")

    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    await bridge.inject_user_message(channel_id=1000, content="Build a Chrome translator extension.")

    project = orch.store.all()[0]
    assert project.state == ProjectState.CLARIFYING

    # User answers the clarifying question
    await bridge.inject_user_message(
        channel_id=project.project_channel_id, content="English↔Spanish only for v1."
    )
    project = orch.store.get(project.id)
    # After the answer Prometheus drafts → Argus reviews (request_changes) → awaiting_approval
    assert project.state == ProjectState.AWAITING_APPROVAL
    assert len(project.specs) == 1
    assert project.specs[0].review_status == "needs_changes"

    # User asks for revisions
    await bridge.inject_user_message(
        channel_id=project.project_channel_id,
        content="changes: please add fallback when offline.",
    )
    project = orch.store.get(project.id)
    assert len(project.specs) == 2
    assert project.specs[1].review_status == "acceptable"
    assert project.state == ProjectState.AWAITING_APPROVAL

    # User approves the revised spec
    await bridge.inject_user_message(
        channel_id=project.project_channel_id, content="approve"
    )
    project = orch.store.get(project.id)
    assert project.state == ProjectState.COMPLETED
    assert len(project.approvals) == 2  # revise + approve
    assert project.approvals[-1].decision == "approved"


@pytest.mark.asyncio
async def test_two_concurrent_projects_stay_isolated(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)

    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    # Two distinct user requests
    await bridge.inject_user_message(channel_id=1000, content="Build a translator extension")
    await bridge.inject_user_message(channel_id=1000, content="Build a sticky-notes extension")

    projects = orch.store.all()
    assert len(projects) == 2
    p1, p2 = projects
    assert p1.project_channel_id != p2.project_channel_id
    assert p1.project_channel_name != p2.project_channel_name

    # Approving p1 must not affect p2
    await bridge.inject_user_message(channel_id=p1.project_channel_id, content="approve")
    p1_now = orch.store.get(p1.id)
    p2_now = orch.store.get(p2.id)
    assert p1_now.state == ProjectState.COMPLETED
    assert p2_now.state == ProjectState.AWAITING_APPROVAL
    # Workstream channels for p1 are not in p2's set
    p1_channels = {ws.channel_id for ws in p1_now.workstreams.values()}
    p2_channels = {ws.channel_id for ws in p2_now.workstreams.values()}
    assert p1_channels.isdisjoint(p2_channels)
    assert p2_channels == set(), "p2 has not decomposed yet so should have no workstream channels"


@pytest.mark.asyncio
async def test_specialist_messages_only_in_their_own_channel(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    await bridge.inject_user_message(channel_id=1000, content="Build a thing")
    project = orch.store.all()[0]
    await bridge.inject_user_message(channel_id=project.project_channel_id, content="approve")
    project = orch.store.get(project.id)

    # For each workstream, completion message lives in its own channel ONLY
    role_to_completion = {
        AgentRole.APOLLO: "apollo done",
        AgentRole.ATLAS: "atlas done",
        AgentRole.HEPHAESTUS: "Test plan ready",
    }
    for ws in project.workstreams.values():
        own = bridge.messages_in(ws.channel_id)
        own_text = " ".join(m["content"].lower() for m in own)
        assert "complete" in own_text or "ready" in own_text
        for other_ws in project.workstreams.values():
            if other_ws.channel_id == ws.channel_id:
                continue
            other_text = " ".join(m["content"].lower() for m in bridge.messages_in(other_ws.channel_id))
            # Specialist completion messages are role-specific; they should not
            # appear in other workstream channels.
            assert role_to_completion[ws.role].lower() not in other_text


@pytest.mark.asyncio
async def test_intake_failure_when_channel_creation_fails(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    bridge.set_channel_create_failure(True)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    runtime.set_mock(AgentRole.ATHENA, athena_intake_responder())
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    await bridge.inject_user_message(channel_id=1000, content="Build a thing")
    project = orch.store.all()[0]
    assert project.state == ProjectState.INTAKE_FAILED
    main_msgs = bridge.messages_in(1000)
    assert any("Failed to create project channel" in m["content"] for m in main_msgs)


@pytest.mark.asyncio
async def test_blocked_when_specialist_cli_fails(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)
    # Apollo blows up
    runtime.set_mock(AgentRole.APOLLO, failing_responder("gemini binary missing"))
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    await bridge.inject_user_message(channel_id=1000, content="Build a thing with frontend")
    project = orch.store.all()[0]
    await bridge.inject_user_message(channel_id=project.project_channel_id, content="approve")
    project = orch.store.get(project.id)
    # At least one workstream blocked → project state BLOCKED
    statuses = {ws.status for ws in project.workstreams.values()}
    assert "blocked" in statuses
    assert project.state == ProjectState.BLOCKED
    proj_msgs = bridge.messages_in(project.project_channel_id)
    assert any("blocked" in m["content"].lower() for m in proj_msgs)


@pytest.mark.asyncio
async def test_artifact_files_written_per_workstream(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    await bridge.inject_user_message(channel_id=1000, content="Build a translator")
    project = orch.store.all()[0]
    await bridge.inject_user_message(channel_id=project.project_channel_id, content="approve")
    project = orch.store.get(project.id)

    # spec artifact under project workspace
    project_ws = tmp_path / project.project_channel_name / "artifacts"
    assert project_ws.exists()
    spec_files = list(project_ws.glob("spec-v*.md"))
    assert spec_files, "spec artifact should be persisted"

    # workstream artifacts under the workstream subdir
    for ws in project.workstreams.values():
        assert ws.artifacts, f"workstream {ws.type.value} should have artifacts"
        for path in ws.artifacts:
            assert path.startswith(str(tmp_path / project.project_channel_name / ws.type.value))


@pytest.mark.asyncio
async def test_message_in_unknown_channel_is_ignored(tmp_path):
    bridge = InMemoryDiscordBridge(main_channel_id=1000)
    cfg = MythosConfig(main_channel_id=1000, workspace_root=str(tmp_path))
    cfg.agents = load_config().agents
    runtime = _build_runtime(cfg)
    _wire_happy_path(runtime)
    orch = Orchestrator(config=cfg, bridge=bridge, runtime=runtime, workspace_root=tmp_path)

    # Manually create a stray channel (not associated with a project)
    stray = await bridge.create_channel("random-chat", category=None)
    await bridge.inject_user_message(channel_id=stray, content="hello?")
    # No project should have been created
    assert orch.store.all() == []
