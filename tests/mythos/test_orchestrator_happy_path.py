"""End-to-end happy-path tests for the Mythos orchestrator.

These tests use ``InMemoryDiscord`` and ``FakeRunner`` so the full
state machine can be exercised without real Discord, real CLIs, or any
network I/O. Every requirement marked in the spec dir is asserted
either directly or via behaviour observable in channel history.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List

import pytest

from mythos.agents.registry import AgentRegistry
from mythos.config import MythosConfig, load_config
from mythos.discord_adapter import DiscordMessage
from mythos.fake_discord import InMemoryDiscord
from mythos.orchestrator import Orchestrator
from mythos.projects import ProjectStatus, ProjectStore
from mythos.runners.fake import FakeRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_environment(tmp_path: Path, scripted_responses: Dict[str, List[str]]):
    """Build a fully wired Mythos system with in-memory Discord + fake runners."""

    cfg = load_config(env={
        "MYTHOS_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "MYTHOS_STATE_PATH": str(tmp_path / "state.json"),
        "MYTHOS_TIMEOUT_SECONDS": "10",
        "MYTHOS_MAX_PLANNING_ROUNDS": "2",
        "DISCORD_GUILD_ID": "guild-test",
    })

    discord = InMemoryDiscord()
    main_channel_id = discord.add_loose_channel(guild_id=cfg.discord_guild_id, name="main")
    cfg.main_channel_id = main_channel_id

    # Each runner can be backed by per-role scripts.
    fake = FakeRunner("fake-multi", responses=scripted_responses)
    runners = {
        "claude_code": fake,
        "codex": fake,
        "gemini": fake,
    }

    store = ProjectStore(cfg.state_db_path, cfg.workspace_root)
    registry = AgentRegistry(cfg, runners)
    orchestrator = Orchestrator(config=cfg, store=store, registry=registry, discord=discord)
    discord.add_listener(orchestrator.handle_message)
    return cfg, discord, store, registry, orchestrator, main_channel_id


def _user_message(channel_id: str, content: str, *, user_id: str = "user-42") -> DiscordMessage:
    from mythos.fake_discord import _parse_mentions
    return DiscordMessage(
        id=f"u-{time.time_ns()}",
        channel_id=channel_id,
        author_id=user_id,
        author_display_name="testuser",
        content=content,
        is_bot=False,
        is_agent=False,
        mentions=_parse_mentions(content),
    )


def _channel_text(discord: InMemoryDiscord, channel_id: str) -> str:
    return "\n".join(m.content for m in discord.channel_history(channel_id=channel_id, limit=200))


def _channel_id_by_name(discord: InMemoryDiscord, name: str) -> str:
    for cid, meta in discord.all_channels().items():
        if meta["name"] == name:
            return cid
    raise AssertionError(f"channel not found: {name}")


def _planning_chat_history(discord: InMemoryDiscord, project) -> List[DiscordMessage]:
    return discord.channel_history(channel_id=project.sub_channel_ids["planning"], limit=200)


# ---------------------------------------------------------------------------
# Happy-path scenario fixtures
# ---------------------------------------------------------------------------


def _full_happy_path_responses() -> Dict[str, List[str]]:
    """Scripted CLI outputs for the canonical Chrome-extension scenario."""

    decomposition_block = (
        "```decomposition.json\n"
        + json.dumps(
            [
                {
                    "scope": "frontend",
                    "summary": "Chrome extension UI: content script intercepts double-click, popup shows translation.",
                    "dependencies": [],
                },
                {
                    "scope": "backend",
                    "summary": "Bundled JSON dictionary lookup helper exposed to the content script.",
                    "dependencies": [],
                },
                {
                    "scope": "test",
                    "summary": "Headless integration test that double-clicks 'hola' and asserts 'hello' tooltip.",
                    "dependencies": ["frontend", "backend"],
                },
            ],
            indent=2,
        )
        + "\n```\n@user please confirm any further details."
    )

    return {
        # Athena (Main) is invoked by the runner after Argus's review (to
        # summarise + ask for approval) and again after approval (to emit
        # the decomposition manifest). Intake itself uses a hard-coded
        # welcome message and does not call Athena's runner.
        "main": [
            (
                "Here is the consolidated design + review summary:\n"
                "- Chrome extension intercepts double-click events.\n"
                "- Bundled dictionary maps source -> target.\n"
                "- Tests cover content-script + lookup helper.\n"
                "Reply `approve` / `lgtm` to proceed, or describe changes. @user"
            ),
            decomposition_block,
        ],
        # Prometheus (draft_plan) — direct draft, no clarification needed.
        "draft_plan": [
            (
                "I have written `design-spec.md` to the workspace.\n"
                "- Manifest v3 Chrome extension.\n"
                "- Content script registers dblclick, sends selection to background.\n"
                "- Background looks up translation in bundled dictionary.\n"
                "- Popup shows translation tooltip.\n"
                "@Argus please review."
            ),
        ],
        # Argus (review)
        "review": [
            (
                "**Must fix:** - (none)\n"
                "**Should fix:** - Specify dictionary update mechanism.\n"
                "**Nit:** - Mention permissions in manifest.\n"
                "@Athena"
            ),
        ],
        # Apollo (frontend)
        "frontend": [
            "Wrote `content_script.js`, `popup.html`, `popup.css`. @Athena",
        ],
        # Atlas (backend)
        "backend": [
            "Wrote `dictionary.json`, `lookup.js`. @Athena",
        ],
        # Hephaestus (test)
        "test": [
            "Wrote `tests/integration_dblclick.spec.js`. @Athena",
        ],
    }


# ---------------------------------------------------------------------------
# Scenario 1: full happy path
# ---------------------------------------------------------------------------


def test_happy_path_chrome_extension(tmp_path):
    cfg, discord, store, registry, orchestrator, main_channel_id = _build_environment(
        tmp_path, _full_happy_path_responses()
    )

    # 1) User posts intake in the main channel.
    user_msg = _user_message(
        main_channel_id,
        "I want to build a Chrome extension translator: when I double-click on a website it should "
        "search a built-in dictionary and show the translation.",
    )
    discord.emit_user_message(user_msg)

    projects = store.list()
    assert len(projects) == 1
    project = projects[0]
    assert project.id.startswith("proj_")
    assert project.requester_user_id == "user-42"
    assert project.channel_category_id  # category created
    assert "planning" in project.sub_channel_ids  # planning channel created
    # Main channel got an acknowledgement to the user.
    assert any("Got it" in m.content for m in discord.channel_history(channel_id=main_channel_id))

    # Prometheus has been dispatched and posted its draft summary in #planning.
    planning_id = project.sub_channel_ids["planning"]
    planning_text = _channel_text(discord, planning_id)
    assert "design-spec.md" in planning_text
    # Argus reply followed (chained from @Argus mention) — review headings present.
    assert "Must fix" in planning_text
    # Athena has summarised + asked for approval
    assert "approve" in planning_text.lower() and "@user" in planning_text
    assert project.status == ProjectStatus.AWAITING_APPROVAL

    # 2) User approves.
    approve_msg = _user_message(planning_id, "approve")
    discord.emit_user_message(approve_msg)

    # Reload the project — status should now have moved past approval and the
    # decomposition manifest should exist.
    project = store.get(project.id)
    assert project.status in (ProjectStatus.IMPLEMENTING, ProjectStatus.AWAITING_TESTS, ProjectStatus.DONE)
    assert project.approved_spec_hash == "" or len(project.approved_spec_hash) == 64

    manifest_path = Path(project.workspace_path) / "decomposition.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert {e["scope"] for e in manifest} == {"frontend", "backend", "test"}

    # Sub-channels were created for each scope.
    assert "frontend" in project.sub_channel_ids
    assert "backend" in project.sub_channel_ids
    assert "test" in project.sub_channel_ids

    # Apollo / Atlas posted completion in their channels.
    assert "content_script" in _channel_text(discord, project.sub_channel_ids["frontend"])
    assert "dictionary.json" in _channel_text(discord, project.sub_channel_ids["backend"])

    # Hephaestus posted in #test (only after deps).
    assert "integration_dblclick" in _channel_text(discord, project.sub_channel_ids["test"])

    # Project ends in DONE and Athena announced completion in main.
    project = store.get(project.id)
    assert project.status == ProjectStatus.DONE
    assert any("complete" in m.content for m in discord.channel_history(channel_id=main_channel_id))


# ---------------------------------------------------------------------------
# Scenario 2: Draft Plan asks clarifying questions
# ---------------------------------------------------------------------------


def test_clarification_round_then_approval(tmp_path):
    responses = {
        "main": [
            "Summary + review. Reply `approve` to proceed. @user",
            "```decomposition.json\n[{\"scope\": \"frontend\", \"summary\": \"x\", \"dependencies\": []}]\n```",
        ],
        "draft_plan": [
            "1. What target languages?\n2. Online or offline?\n3. Tooltip or popup?\n@user",
            (
                "Got it. Wrote `design-spec.md`.\n"
                "- Offline EN/ES dictionary, tooltip presentation.\n"
                "@Argus please review."
            ),
        ],
        "review": ["**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena"],
        "frontend": ["Done. @Athena"],
    }

    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)

    discord.emit_user_message(_user_message(main_id, "Build a translator extension."))
    project = store.list()[0]
    planning_id = project.sub_channel_ids["planning"]

    # First Prometheus output should be the question — and orchestrator should
    # park in PLANNING_CLARIFY without dispatching Argus.
    planning_msgs = _planning_chat_history(discord, project)
    prom_outputs = [m for m in planning_msgs if m.agent_name == "prometheus"]
    assert prom_outputs, "Prometheus should have posted at least one message"
    assert "1." in prom_outputs[0].content and "@user" in prom_outputs[0].content
    assert project.status == ProjectStatus.PLANNING_CLARIFY

    # User replies with answers in #planning -> the listener simply records
    # the message; Prometheus needs to be re-pinged manually by the user (or
    # by Athena). For this test we re-trigger Prometheus by user @-mention.
    discord.emit_user_message(
        _user_message(planning_id, "@Prometheus offline EN/ES, tooltip", user_id="user-42")
    )

    project = store.get(project.id)
    assert project.status in (
        ProjectStatus.AWAITING_APPROVAL,
        ProjectStatus.PLANNING_REVIEW,
        ProjectStatus.PLANNING_CLARIFY,
    )

    # Once draft_plan flows to Argus then Athena, we can approve and finish.
    discord.emit_user_message(_user_message(planning_id, "lgtm"))

    project = store.get(project.id)
    # In this minimal scenario manifest only includes frontend, so no test
    # phase. We expect implementation to have begun.
    assert "frontend" in project.sub_channel_ids


# ---------------------------------------------------------------------------
# Scenario 3: Project isolation — two concurrent projects
# ---------------------------------------------------------------------------


def test_two_projects_isolated(tmp_path):
    responses = {
        "main": [
            "Approval ask. @user",       # P1 after Argus review
            "Approval ask. @user",       # P2 after Argus review
            "```decomposition.json\n[{\"scope\": \"frontend\", \"summary\": \"P1\", \"dependencies\": []}]\n```",  # P1 decomp
            "```decomposition.json\n[{\"scope\": \"backend\", \"summary\": \"P2\", \"dependencies\": []}]\n```",   # P2 decomp
        ],
        "draft_plan": [
            "Drafted P1 spec. @Argus",
            "Drafted P2 spec. @Argus",
        ],
        "review": [
            "**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena",
            "**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena",
        ],
        "frontend": ["P1 frontend done. @Athena"],
        "backend": ["P2 backend done. @Athena"],
    }
    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)

    discord.emit_user_message(_user_message(main_id, "Project one: a calculator extension."))
    discord.emit_user_message(_user_message(main_id, "Project two: a server health dashboard."))

    projects = store.list()
    assert len(projects) == 2
    p1, p2 = projects[0], projects[1]
    assert p1.id != p2.id
    assert p1.workspace_path != p2.workspace_path
    assert p1.channel_category_id != p2.channel_category_id
    # Sub-channels disjoint:
    assert set(p1.sub_channel_ids.values()).isdisjoint(set(p2.sub_channel_ids.values()))

    # Approve both.
    discord.emit_user_message(_user_message(p1.sub_channel_ids["planning"], "approve"))
    discord.emit_user_message(_user_message(p2.sub_channel_ids["planning"], "approve"))

    # Each project's manifest matches its own scope.
    m1 = json.loads((Path(p1.workspace_path) / "decomposition.json").read_text())
    m2 = json.loads((Path(p2.workspace_path) / "decomposition.json").read_text())
    assert m1[0]["scope"] == "frontend"
    assert m2[0]["scope"] == "backend"

    # Frontend channel exists on P1 but not P2; backend channel exists on P2 but not P1.
    assert "frontend" in store.get(p1.id).sub_channel_ids
    assert "backend" in store.get(p2.id).sub_channel_ids
    assert "backend" not in store.get(p1.id).sub_channel_ids
    assert "frontend" not in store.get(p2.id).sub_channel_ids


# ---------------------------------------------------------------------------
# Scenario 4: Channel-scoped activation - mention outside scope is ignored
# ---------------------------------------------------------------------------


def test_specialist_mention_outside_their_channel_is_ignored(tmp_path):
    responses = {
        "main": [
            "Approval ask. @user",
            "```decomposition.json\n[{\"scope\":\"frontend\",\"summary\":\"x\",\"dependencies\":[]}]\n```",
        ],
        "draft_plan": ["Drafted. @Argus"],
        "review": ["**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena"],
        "frontend": ["Done. @Athena"],
    }
    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)

    discord.emit_user_message(_user_message(main_id, "Build a thing."))
    project = store.list()[0]
    discord.emit_user_message(_user_message(project.sub_channel_ids["planning"], "approve"))

    frontend_id = project.sub_channel_ids["frontend"]
    # Capture how many calls the runners have already made.
    fake = next(iter(registry._runners.values()))  # noqa: SLF001
    before = len(fake.calls)

    # User pings @Apollo from #planning — should be a no-op (Apollo is
    # scoped to #frontend).
    discord.emit_user_message(_user_message(project.sub_channel_ids["planning"], "@Apollo any update?"))
    after_planning = len(fake.calls)
    assert after_planning == before, "Apollo must NOT be dispatched from #planning"

    # User pings @Apollo in #frontend — should be a dispatch.
    fake._responses.setdefault("frontend", []).append("Tweaked. @Athena")
    discord.emit_user_message(_user_message(frontend_id, "@Apollo please tweak the icon"))
    assert len(fake.calls) == after_planning + 1


# ---------------------------------------------------------------------------
# Scenario 5: Long output is chunked under 2000 chars
# ---------------------------------------------------------------------------


def test_long_output_is_chunked(tmp_path):
    long_body = "A" * 4500 + "\n@Argus"
    responses = {
        "main": ["Approval ask. @user"],
        "draft_plan": [long_body],
        "review": ["**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena"],
    }
    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)
    discord.emit_user_message(_user_message(main_id, "Build a thing."))

    project = store.list()[0]
    msgs = discord.channel_history(channel_id=project.sub_channel_ids["planning"], limit=200)
    prom_msgs = [m for m in msgs if m.agent_name == "prometheus"]
    # Output >2000 chars should be split across multiple messages.
    assert len(prom_msgs) >= 2
    assert all(len(m.content) <= 2000 for m in prom_msgs)


# ---------------------------------------------------------------------------
# Scenario 6: Approval keyword variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", ["approve", "Approve", "lgtm", "LGTM", "approved!"])
def test_approval_keywords(tmp_path, keyword):
    responses = {
        "main": [
            "Approval ask. @user",
            "```decomposition.json\n[{\"scope\":\"frontend\",\"summary\":\"x\",\"dependencies\":[]}]\n```",
        ],
        "draft_plan": ["Drafted. @Argus"],
        "review": ["**Must fix:** - (none)\n**Should fix:** - (none)\n**Nit:** - (none)\n@Athena"],
        "frontend": ["Done. @Athena"],
    }
    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)
    discord.emit_user_message(_user_message(main_id, "Build a thing."))
    project = store.list()[0]
    discord.emit_user_message(_user_message(project.sub_channel_ids["planning"], keyword))
    project = store.get(project.id)
    assert "frontend" in project.sub_channel_ids


# ---------------------------------------------------------------------------
# Scenario 7: Persistence + recovery
# ---------------------------------------------------------------------------


def test_state_persists_across_store_reopen(tmp_path):
    responses = {
        "main": [],
        "draft_plan": ["1. anything?\n@user"],
    }
    cfg, discord, store, registry, _orch, main_id = _build_environment(tmp_path, responses)
    discord.emit_user_message(_user_message(main_id, "X"))
    pid = store.list()[0].id

    # Reopen
    store2 = ProjectStore(cfg.state_db_path, cfg.workspace_root)
    p = store2.get(pid)
    assert p is not None
    assert p.id == pid
    assert p.requester_user_id == "user-42"
    assert p.channel_category_id  # persisted
    # Channel index also rebuilt:
    assert store2.get_by_channel(p.sub_channel_ids["planning"]) is not None
