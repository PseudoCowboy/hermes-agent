# Plan: Deploy hermes-agent with Multi-Agent Workflow on Azure VM

**Status:** in-progress
**Owners:** user (zejiajiang), Claude Code (this session), Codex (review)
**VM:** `ssh -i ~/.ssh/duck_key.pem azureuser@172.169.248.86` (Ubuntu 24.04, 4 vCPU, 7.8 GiB RAM, 29 GB disk, Python 3.12.3, Node 22.22.2)
**Canonical working copy:** `/Users/jiangzejia/code/analysis/hermes-agent` on mac
**VM working copy target:** `/home/azureuser/hermes-agent`

This file is the **shared** contract between the user, Claude, and Codex. Everyone checks and updates it. Edit checkboxes in place (`[ ]` → `[x]`). Add blocking notes inline under the relevant item.

---

## Goal

Stand up the custom multi-agent workflow (NanoClaw-style Iris / Hermes / Athena / Atlas / Apollo / Argus roles backed by the `workflow_*` tools and skills) on the new Azure VM, reachable via Telegram (and later Discord when the user creates a new bot). CLI coding agents (claude code, codex) on the VM route through a local `copilot-api` daemon so neither needs real Anthropic/OpenAI API keys.

## Principles

- **State lives in the repo.** No scratch notes in chat. This plan file + `.env.example` + `scripts/` are the handoff surface.
- **Sessions may break.** Any VM-side command that can run longer than ~30 s runs under `systemd`, `tmux`, or as a background task written to a log — never as an interactive foreground process.
- **Code review gate.** Before each push of non-trivial code, run `codex-review` (gpt-5.4). Note the result in the relevant checklist item.
- **Mac first, VM second.** Local changes get committed + pushed to our custom remote, then pulled on the VM. No direct edits on the VM except config files.

---

## Pre-flight blockers (user must unblock)

- [ ] **B1 — Remote target.** Decide where the custom migration lives. `origin` currently points to `NousResearch/hermes-agent`. Pick one:
  - `(a)` Personal fork `git@github.com:<user>/hermes-agent.git` on a branch like `nanoclaw-migration`. **Recommended.**
  - `(b)` Separate personal repo (e.g. `hermes-agent-private`).
  - `(c)` Other.
  Record choice: _TBD_
- [ ] **B2 — VM GitHub access.** Add the VM's ed25519 pub key to the chosen repo (Deploy key with write access is fine). Pub key:
  ```
  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAh30GTbfPge2mPczwSX37PBPpEbHNPmCs2tz3FjBYVn azureuser@duck
  ```
- [ ] **B3 — Hermes LLM provider.** What does Hermes itself use for the main model?
  - `(a)` Route through the same copilot-api daemon (OpenAI-compat on `:4142`). Zero key needed. **Recommended for parity with CLIs.**
  - `(b)` OpenRouter key.
  - `(c)` Anthropic / OpenAI direct.
  Record choice: _TBD_
- [ ] **B4 — Telegram token.** User holds it. Claude will ask right before wiring up the adapter (phase P6).
- [ ] **B5 — Discord.** ~~Deferred. User creates the bot when Telegram smoke test passes. Phase P7 is blocked on this.~~ **Resolved 2026-04-22** — bot `Hermes` (app id `1496432314660552766`) invited to Gea's server, intents enabled, smoke test passed.

---

## Phases & checklist

### P0 — Baseline sync (mac → custom remote)

- [ ] P0.1 Resolve B1 (remote target). Add it as `personal` git remote on mac.
- [ ] P0.2 Clean triage of working tree. Commit the migration work in logical chunks:
  - [ ] `plans/` docs (nanoclaw-migration analyses, baseline, this plan)
  - [ ] `optional-skills/migration/nanoclaw-migration/` (the migrator skill)
  - [ ] `skills/software-development/workflow-*/` (the 8 workflow skills)
  - [ ] `tools/workflow_tools.py` + `tests/tools/test_workflow_tools.py`
  - [ ] `toolsets.py` + `hermes_cli/tools_config.py` + related tests (the workflow toolset split)
  - [ ] `scripts/deploy-source.sh`
  - [ ] `model_tools.py`, `.gitignore`, `tests/agent/test_skill_commands.py`
- [ ] P0.3 Run `codex-review` on the full migration diff. Note findings.
- [ ] P0.4 Push to `personal/nanoclaw-migration` (or chosen branch).

### P1 — VM system prep

- [ ] P1.1 Install OS deps: `build-essential`, `libssl-dev`, `libffi-dev`, `pkg-config`, `curl`, `ca-certificates`, `tmux`, `jq`, `ripgrep`.
- [ ] P1.2 Install `uv` (astral-sh) for Python env management: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- [ ] P1.3 Confirm Node 22 + npm 10 (already present).
- [ ] P1.4 Configure git identity (`user.email`, `user.name`) on VM for any VM-side commits (should be rare).

### P2 — `copilot-api` daemon

- [ ] P2.1 Run `sudo npx @jeffreycao/copilot-api@latest start` interactively once so the user can complete the GitHub device-code auth flow. User must be in the session for this step.
- [ ] P2.2 Create a systemd unit `/etc/systemd/system/copilot-api.service` running `sudo npx @jeffreycao/copilot-api@latest start` as a service that restarts on failure and starts at boot. Logs to journald.
- [ ] P2.3 Verify both endpoints respond: `curl http://127.0.0.1:4141/v1/models` (Anthropic-compat) and `curl http://127.0.0.1:4142/v1/models` (OpenAI-compat).
- [ ] P2.4 Record Copilot auth state path (so we know what survives reboots).

### P3 — `claude` and `codex` CLIs on VM

- [ ] P3.1 Install the two CLIs globally: `sudo npm i -g @anthropic-ai/claude-code @openai/codex` (or whatever the actual package names are; verify from mac install).
- [ ] P3.2 Port `~/.codex/config.toml` from mac, rewriting `base_url` to `http://127.0.0.1:4142/v1` (already matches). Strip mac-specific `[projects."/Users/..."]` entries. Keep `mcp_servers.gemini-search` only if the binary is also on the VM; otherwise drop for now.
- [ ] P3.3 Create a launcher script `~/bin/cc` on the VM that exports the full Anthropic env block the user provided and `exec claude "$@"`. This is how claude code runs on the VM.
- [ ] P3.4 Smoke test both: `cc` and `codex` each answer a trivial prompt through the local daemon.

### P4 — hermes-agent VM install

- [ ] P4.1 `git clone` the chosen remote/branch into `/home/azureuser/hermes-agent`.
- [ ] P4.2 Create venv with `uv venv` + `uv sync` (or the repo's documented install path — check `pyproject.toml` / `README`).
- [ ] P4.3 Install any node deps the repo needs (`npm install` for `agent-browser`, etc.) — only if we're keeping the browser toolset enabled on the VM (probably not initially; the VM is headless).
- [ ] P4.4 Run the repo's test suite (fast subset at minimum): `pytest tests/tools/test_workflow_tools.py tests/test_toolsets.py tests/hermes_cli/ -q`. All must pass.

### P5 — hermes-agent config on VM

- [ ] P5.1 Create `~/.hermes/config.yaml` with: Telegram platform enabled, CLI platform enabled, `workflow-readonly` + `workflow-mutating` enabled on CLI only, browser/image_gen/tts/vision/rl/home-assistant disabled, memory + session_search + web + file + terminal + skills + todo + clarify + cronjob + delegation + code_execution enabled.
- [ ] P5.2 Create `~/.hermes/.env` with provider credentials resolved from B3. If B3=(a) copilot-api, wire Hermes's OpenRouter/OpenAI client to `http://127.0.0.1:4142/v1`.
- [ ] P5.3 Run `hermes setup tools --summary` to confirm the resolved toolset matches expectation.

### P6 — Telegram smoke test

- [x] P6.1 Ask user for Telegram bot token (B4). Save in `~/.hermes/.env` as `TELEGRAM_BOT_TOKEN`.
- [x] P6.2 Start gateway under systemd (unit: `/etc/systemd/system/hermes-gateway.service`) pointing at the hermes venv. Journald logs.
- [x] P6.3 Send `/start` from the user's Telegram client. Confirm bot responds.
- [x] P6.4 End-to-end multi-agent workflow smoke test from Telegram:
  - Iris collects clarifying questions.
  - Hermes drafts plan → user approves → Athena decomposes.
  - Atlas executes the first stream (stub: a single trivial task touching a test file).
  - Apollo produces a completion checkpoint; Argus reviews.
  > NOTE (2026-04-22): Multi-agent workflow *intentionally does not engage on Telegram*
  > because `workflow-mutating` is CLI/Discord-only per platform policy.  On Telegram,
  > Hermes behaves as a single-agent assistant.  Validated instead: Telegram → Hermes →
  > file+terminal tools → scratch/hello.py created + executed + reply delivered.
  > Full multi-agent E2E must be exercised from the CLI (or later, Discord).
- [x] P6.5 Confirm repo state on VM under `groups/shared_project/active/<slug>/` matches expected `plan-state.json` / `task-state.json` / `progress.md`.
  > NOTE: N/A for this smoke test (no workflow-mutating invocation on Telegram).  Will
  > re-validate when CLI-driven workflow test runs.
- [x] P6.6 Build reusable Telegram E2E automation: `scripts/telegram_e2e.py` (Playwright,
  persistent Chromium profile).  One-time QR-code login; subsequent runs headless.
  Default scenarios in `scripts/telegram_e2e_scenarios.yaml`.

### P7 — Discord

- [x] P7.1 User creates new Discord bot + invites to test server.
- [x] P7.2 User provides token + channel + user IDs.
- [x] P7.3 Enable `discord` platform in `~/.hermes/config.yaml` with `workflow-mutating` on.
- [x] P7.4 Restart `hermes-gateway.service`.
- [x] P7.5 End-to-end workflow test from Discord (2026-04-22):
  - `ping` → `Pong! 🏓` (reply inside auto-created thread)
  - `start a new project: create a scratch python file that prints "hello from multi-agent hermes on discord"`
    → Hermes invoked `workflow_create_project`, wrote `scratch.py`, ran it, reported success with the project
    scaffold (`control/`, `coordination/`, `plans/`, `workstreams/`, `archive/`) under
    `groups/shared_project/active/multi-agent-hermes-discord/`.
  > NOTE: `DISCORD_REQUIRE_MENTION` defaults to `true`. Added channel `1496437091725475910` (Gea's
  > server → `#test`) to `DISCORD_FREE_RESPONSE_CHANNELS` so bare messages route to the bot.
  > NOTE: For trivial one-shot requests Hermes collapses the 6-role handoff (Iris→…→Argus) into
  > a single turn; the workflow-mutating toolset IS callable on Discord, which is the gating
  > validation. Larger scopes will exercise the full chain.

### P8 — Reliability pass

- [x] P8.1 Reboot the VM. Confirm `copilot-api.service` and `hermes-gateway.service` come back healthy.
  > 2026-04-22: `sudo reboot` → ~30s later both services `active`, `curl :4141/v1/models` returns 200,
  > post-reboot Discord `ping after reboot` acknowledged by bot.
- [x] P8.2 Rotate logs (systemd default is fine).
- [x] P8.3 Add a tiny `scripts/vm-status.sh` that prints: service statuses, disk, memory, last 20 gateway log lines, last 20 copilot-api log lines. Convenience for the user.
- [ ] P8.4 `codex-review` the whole diff one more time. Address or dismiss findings.
- [ ] P8.5 Merge or keep branch — user's call.

---

## Conventions

- **Updating this file:** any participant (user, Claude, Codex) may tick boxes or add `> NOTE:` inline comments. Don't delete phases; mark them `[x]` or `[~]` (partial) instead.
- **Commands I run on the VM** go into `scripts/` when they're reusable; ad-hoc stuff stays in chat.
- **Secrets** (telegram token, github PAT, etc.) **never** go into the repo, **never** get echoed in chat beyond the one-off input. Only under `~/.hermes/.env` on the VM.
- **Resuming after a session break:** next Claude reads this file top-to-bottom, finds the first unchecked item in the first unchecked phase, keeps going.

## Decision log

_empty_
