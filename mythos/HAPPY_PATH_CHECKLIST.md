# Mythos Happy-Path Manual Test Checklist

A live, in-Discord smoke test that confirms Mythos works end-to-end with
real CLIs and real users. Run this after `tests/mythos/` passes and after
finishing `mythos/SETUP.md`. Estimated wall-clock: 15–30 minutes.

> Pre-req: a Discord server you control where the Mythos bot is invited
> (via the URL from `SETUP.md` §1b), a designated `#main` channel whose
> ID is in `MYTHOS_MAIN_CHANNEL_ID`, and `python -m mythos` running
> against that bot's token.

## Phase 0 — Bot online

- [ ] In your terminal, `python -m mythos` started without error.
- [ ] Within ~5s the log line `Connected as <YourBot>#NNNN` appeared.
- [ ] In Discord, the bot avatar shows green/online in the member list.

## Phase 1 — Project kickoff

- [ ] In the `#main` channel, post:
  ```
  I want to build a chrome extension, a translator,
  double-click on a word, look it up in a built-in
  dictionary, show the translation.
  ```
- [ ] Within 5s, **Athena** posts an acknowledgement in `#main` mentioning a
  project slug like `proj-translator-extension-YYYYMMDD-xxxxxx`.
- [ ] A new Discord category appears in the sidebar named
  `mythos-<slug>`.
- [ ] Inside that category, exactly **one** channel exists:
  `<slug>-general`.
- [ ] **Athena** posted an opening message in `<slug>-general` repeating the
  user's request and announcing the handoff to Prometheus.

## Phase 2 — Spec drafting

- [ ] Within ~30–120s (depending on CLI latency), **Prometheus** posts a
  Markdown spec inside `<slug>-general`. The message header includes
  `[Prometheus · Draft Plan]`.
- [ ] On disk, `spec/spec-v1.md` exists under the project workspace
  (default `~/mythos/projects/<slug>/spec/spec-v1.md`).

## Phase 3 — Spec review

- [ ] **Argus** posts a review in `<slug>-general` after Prometheus, with a
  `## Verdict` line containing either `APPROVED` or `CHANGES_REQUESTED`,
  followed by a `## Comments` bullet list.
- [ ] **Athena** posts a follow-up asking the user to approve or describe
  changes.

## Phase 4 — Approval round

Pick **one** of (a) approve straight through or (b) request changes first.

### 4a. Approve straight through
- [ ] In `<slug>-general`, type `approve`.
- [ ] **Athena** posts a confirmation that decomposition is starting.

### 4b. Request changes
- [ ] In `<slug>-general`, type a substantive change request (e.g.
  `the dictionary should be offline-only, and double-click should also
  work on selected text, not just words`).
- [ ] **Athena** acknowledges the change request and pings Prometheus
  again.
- [ ] Within ~60s, **Prometheus** posts `spec-v2.md` and **Argus**
  re-reviews.
- [ ] **Athena** asks for approval again. Reply `approve`.

## Phase 5 — Decomposition

- [ ] **Athena** posts a decomposition with three sections:
  `## Frontend (Apollo)`, `## Backend (Atlas)`, `## Test (Hephaestus)`.
- [ ] Three new channels appear in the project category:
  `<slug>-frontend`, `<slug>-backend`, `<slug>-test`.
- [ ] **Athena** confirms in `<slug>-general` that specialist channels
  are open.

## Phase 6 — Specialist execution

- [ ] **Apollo** (Gemini CLI) posts only in `<slug>-frontend`.
- [ ] **Atlas** (Claude Code CLI) posts only in `<slug>-backend`.
- [ ] **Hephaestus** (Codex CLI) posts only in `<slug>-test`.
- [ ] **No specialist's name appears in another specialist's channel.**
  Apollo never speaks in `-backend`; Atlas never speaks in `-test`; etc.
- [ ] On disk, each specialist's working directory has been touched —
  `frontend/`, `backend/`, `tests/` under the project workspace contain
  the files the agent claims to have created.

## Phase 7 — Question-in-own-channel

If a specialist needs clarification, it should post a line like
`❓ I need a clarification before I can finish: …` in **its own
channel only**. Confirm:

- [ ] If a specialist asks a question, the question appears only in
  that specialist's channel.
- [ ] When you reply in that same channel with the answer, the
  specialist resumes work and posts another update there.

## Phase 8 — Project complete

- [ ] **Athena** posts a final "All specialists report complete" message
  in `<slug>-general`.
- [ ] On disk, `state.json` in the project workspace shows `"phase":
  "complete"`.

## Phase 9 — Concurrent project isolation

- [ ] In `#main`, post a *second* unrelated request (e.g.
  `build a markdown notebook editor`).
- [ ] A second category + general channel are created with a different
  slug.
- [ ] No messages from project A appear in project B's channels.
- [ ] Both projects can be in different phases simultaneously without
  interference.

## Failure handling spot-checks

- [ ] Stop one of the CLI binaries (or temporarily rename it on PATH) and
  trigger a new project. Mythos should post an `[error]` message in the
  affected channel — not crash.
- [ ] Restart `python -m mythos`. The `index.json` file under the
  workspace root should still list pre-existing projects (Discord remains
  the system of record for live state).

If every box is checked, the system passes. File any deviation in
`mythos/TEST_REPORT.md` under the *Known Gaps* section.
