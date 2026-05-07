# Mythos Integration Test Report

This report summarises what the bundled `tests/mythos/` suite verifies,
which spec requirements each test exercises, and what is intentionally
out of scope (i.e. what still requires the manual checklist in
`HAPPY_PATH_CHECKLIST.md` to confirm).

## Run

```
$ python -m pytest tests/mythos/ -q
30 passed, 4 warnings in ~2 seconds
```

The two test files are:

| File | Purpose |
|---|---|
| `tests/mythos/test_orchestrator_happy_path.py` | End-to-end workflow tests using the in-memory Discord double + scripted CLI runners. |
| `tests/mythos/test_units.py` | Unit tests for config, project store, slugifier, FakeRunner, SubprocessRunner. |

## What is simulated

The integration tests bypass real Discord and real CLI subprocesses by
substituting:

* `mythos.fake_discord.InMemoryDiscord` for the production Discord
  adapter. It implements the same `DiscordAdapter` Protocol as the
  real `DiscordPyAdapter`, so the orchestrator code is exercised
  exactly as it would be in production.
* `mythos.runners.fake.FakeRunner` keyed by agent role for each
  CLI runner. Tests pre-script per-role outputs that resemble what a
  real Claude Code / Codex / Gemini CLI would emit (including
  `@AgentName` handoff pings and ` ```decomposition.json``` ` fenced
  blocks).
* A `tmp_path`-rooted `ProjectStore` so workspace and state files do
  not leak between tests.

## Spec coverage

| Spec requirement | Test |
|---|---|
| `discord-orchestration` → Main channel intake | `test_happy_path_chrome_extension` (intake), `test_two_projects_isolated` |
| `discord-orchestration` → Project channel category creation | `test_happy_path_chrome_extension`, `test_two_projects_isolated` |
| `discord-orchestration` → Lazy creation of discipline sub-channels | `test_happy_path_chrome_extension`, `test_two_projects_isolated` |
| `discord-orchestration` → Long output chunking | `test_long_output_is_chunked` |
| `discord-orchestration` → Mention-based handoff routing | `test_happy_path_chrome_extension` (Prometheus→Argus chain), `test_clarification_round_then_approval` (user @-mention) |
| `discord-orchestration` → Status feedback to main channel | `test_happy_path_chrome_extension` (DONE message), `test_two_projects_isolated` |
| `agent-runtime` → Role-to-CLI binding (defaults match brief) | `test_default_role_to_runner_binding`, `test_default_runner_commands_match_brief` |
| `agent-runtime` → Override via configuration | `test_runner_binding_override_via_env` |
| `agent-runtime` → Subprocess lifecycle (cwd, timeout, missing binary) | `test_subprocess_runner_passes_prompt_via_stdin`, `test_subprocess_runner_passes_prompt_via_argv`, `test_subprocess_runner_timeout_marks_timed_out`, `test_subprocess_runner_missing_binary_raises` |
| `agent-runtime` → Per-role system prompts | (Implicit; orchestrator dispatches always include the per-role system prompt block — checked indirectly by `test_happy_path_chrome_extension` because Prometheus' fake responses match the Prometheus role pull.) |
| `agent-runtime` → Credential handling (no credential values in logs / snapshots) | `test_to_dict_redacts_secrets_and_lists_env_keys_only` |
| `project-isolation` → Unique project identifier | `test_project_id_generator_format`, `test_two_ids_distinct_in_same_second` |
| `project-isolation` → Per-project working directory | `test_store_workspace_isolation` |
| `project-isolation` → Per-project metadata persistence + reopen | `test_store_create_persist_and_reopen`, `test_state_persists_across_store_reopen` |
| `project-isolation` → Conversation context isolation | `test_two_projects_isolated` (channel/file isolation; context bundles are scoped per-channel by construction). |
| `project-isolation` → Concurrent project parallelism | `test_two_projects_isolated` |
| `project-isolation` → Project teardown | `test_store_archive` |
| `planning-and-review-workflow` → Initial drafting handoff | `test_happy_path_chrome_extension` |
| `planning-and-review-workflow` → Clarification before drafting | `test_clarification_round_then_approval` |
| `planning-and-review-workflow` → Design spec artifact | (Tested indirectly: the `_persist_design_spec_if_present` hook + workspace existence checks; the real Claude Code CLI is responsible for writing `design-spec.md`, so the production run uses the actual file.) |
| `planning-and-review-workflow` → Review handoff | `test_happy_path_chrome_extension` |
| `planning-and-review-workflow` → User approval gate | `test_happy_path_chrome_extension`, `test_approval_keywords` |
| `planning-and-review-workflow` → Approved spec is immutable for downstream work | (Implicit: orchestrator records `approved_spec_hash` after approval; spec hash is part of the dispatched metadata for downstream agents.) |
| `work-decomposition` → Decomposition is triggered by approval | `test_happy_path_chrome_extension`, `test_approval_keywords` |
| `work-decomposition` → Decomposition manifest format | `test_happy_path_chrome_extension` (writes `decomposition.json`) |
| `work-decomposition` → Sub-channel creation matches manifest | `test_two_projects_isolated` (P1 frontend-only, P2 backend-only) |
| `work-decomposition` → Specialist briefing | `test_happy_path_chrome_extension` |
| `work-decomposition` → Dependency-aware ordering (test waits for impl) | `test_happy_path_chrome_extension` (test scope depends on frontend+backend) |
| `implementation-agents` → Channel-scoped activation | `test_specialist_mention_outside_their_channel_is_ignored` |
| `implementation-agents` → Approved design spec in prompt | (Implicit: dispatch metadata includes `approved_spec_hash` and the workspace cwd contains `design-spec.md`.) |
| `implementation-agents` → Channel-only clarification | (Implicit by routing: the channel-scope guard refuses cross-channel dispatch.) |
| `implementation-agents` → Code is written into project workspace | (Test substitutes scripted runner output; in production every runner is invoked with `cwd=workspace`.) |
| `implementation-agents` → Completion message contract | `test_happy_path_chrome_extension` |

## What is intentionally NOT covered by the integration tests

These require a live Discord guild and the real CLIs and are documented
in `HAPPY_PATH_CHECKLIST.md` for manual validation:

* Discord application + bot creation, intent enabling, channel-permission
  configuration.
* The actual webhook-based per-agent identity (each agent posting under
  a distinct username + avatar via the channel webhook). The in-memory
  fake records the `agent_name` / `agent_display_name` it would have
  used, but only the real adapter creates Discord webhooks.
* Real subprocess launch of `claude`, `codex`, `gemini` against the
  proxy. `test_units.py` verifies the SubprocessRunner mechanics
  (stdin payload, argv-mode prompt, timeout, missing binary) using
  small POSIX commands (`/bin/cat`, `/usr/bin/printf`, `/bin/sh`); it
  does not require any of the real LLM CLIs to be installed.
* Discord rate-limit handling (deferred to discord.py defaults).
* Reaction-based approval (`✅` / `👍`). The orchestrator's approval
  detection accepts both reactions and keywords, but the in-memory fake
  Discord doesn't simulate reactions; the keyword path is fully tested.

## Known gaps

* `_persist_design_spec_if_present` only writes `design-spec.md` from
  the runner's output if the runner emitted a fenced ` ```markdown ` /
  ` ```md ` block whose content begins with a `#` heading. The real
  Claude Code CLI normally writes the file directly via its file-edit
  tool, so this fallback rarely fires; for fully scripted-output
  scenarios you should ensure your draft-plan stub emits such a block
  if you want the file to exist.
* The orchestrator currently treats the Main Agent (Athena) as
  workflow-driven rather than mention-driven inside implementation
  channels (i.e. an `@Athena` ping in `#frontend` is interpreted as a
  completion signal, not a re-dispatch). If you need real mention-based
  Athena dispatch from arbitrary channels, lift the `if next_agent.role
  == "main": continue` guard in `orchestrator._post_dispatch_followups`.
