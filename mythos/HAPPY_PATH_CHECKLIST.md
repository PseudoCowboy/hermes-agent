# Mythos Happy-Path Manual Test Checklist

Use this checklist to confirm a real Discord deployment matches the
behavior the integration tests cover.

## Pre-flight

- [ ] `mythos/.env` is filled in (see `SETUP.md`).
- [ ] `claude --version`, `codex --version`, `gemini --version` all work.
- [ ] The bot is invited to the guild and has **Manage Channels**.
- [ ] The configured `MYTHOS_MAIN_CHANNEL_ID` exists in the guild.

## Launch

- [ ] `set -a; source mythos/.env; set +a`
- [ ] `python -m mythos` reports `mythos discord client ready as <bot>`.

## Step 1 — Intake

- [ ] In the main channel post:

  > I want to build a Chrome extension, a translator extension, when I
  > double click on a word it should look it up in a built-in
  > dictionary and show the translation.

- [ ] Within ~2 seconds Athena posts an acknowledgement in the **main**
  channel that includes a project short id and a link to the new plan
  channel.
- [ ] A new Discord category appears named
  `project-<short-id>-<slug>` containing a single `<short-id>-plan`
  channel.

## Step 2 — Plan channel

- [ ] In the plan channel, **Athena** posts an intake message naming the
  project.
- [ ] **Prometheus** posts a clarifying question message (numbered
  questions, with `QUESTION:` prefixes).

- [ ] Reply in the plan channel with answers:

  > English ↔ Chinese, offline only, simple popup is fine.

- [ ] **Prometheus** posts a design spec (sections 1–10).
- [ ] **Argus** posts a review (BLOCKING / SUGGESTION / QUESTION
  bullets, or "No blocking issues found").
- [ ] **Athena** posts an approval prompt and tells you which channels
  will be created on approval.

## Step 3 — Revision (optional)

- [ ] Reply with `revise: please add a permissions section`.
- [ ] **Prometheus** posts a v2 spec, **Argus** posts a v2 review,
  **Athena** asks for approval again.

## Step 4 — Approval & decomposition

- [ ] Reply `approve`.
- [ ] **Athena** posts that approval is recorded and decomposition is
  starting.
- [ ] Three new channels appear under the project category:
  `<short-id>-frontend`, `<short-id>-backend`, `<short-id>-test`.
- [ ] **Apollo** posts an introduction in the **frontend** channel and
  nowhere else.
- [ ] **Atlas** posts an introduction in the **backend** channel and
  nowhere else.
- [ ] **Hephaestus** posts an introduction in the **test** channel and
  nowhere else.

## Step 5 — Channel confinement

- [ ] In the frontend channel, post a follow-up question. Apollo
  responds in **frontend** only — Atlas / Hephaestus stay silent.
- [ ] Repeat in backend (only Atlas responds) and test (only
  Hephaestus responds).
- [ ] Post nonsense in the main channel — mythos does **not** start a
  new project unless your message looks like a project request.

## Step 6 — Concurrent projects

- [ ] Post a second, different project request in the main channel
  while the first is still running.
- [ ] Mythos creates a **separate** category, separate plan channel,
  separate workspace directory under `MYTHOS_WORKSPACE_ROOT/<project_id>`.
- [ ] Messages from project A never appear in project B's channels.

## Step 7 — Persistence

- [ ] Stop mythos with Ctrl-C.
- [ ] `cat ~/.mythos/state.json.d/<project_id>.json` shows the project
  with status, channels, design specs, approvals, and audit events.
- [ ] Restart `python -m mythos`. New messages in either project's
  channels are still routed correctly (the orchestrator looked up the
  project by channel id from disk).

## Step 8 — Workspace inspection

- [ ] `ls $MYTHOS_WORKSPACE_ROOT/<project_id>/artifacts/design/` shows
  one Markdown file per spec version (`v01.md`, `v02.md`, …).
- [ ] `ls $MYTHOS_WORKSPACE_ROOT/<project_id>/artifacts/reviews/`
  contains the matching review files.
- [ ] `ls $MYTHOS_WORKSPACE_ROOT/<project_id>/artifacts/logs/` contains
  one structured log per agent run including the prompt sent to the
  CLI, the stdout/stderr, the return code, and the wall time.
