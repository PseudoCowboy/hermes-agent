# Mythos Happy-Path Manual Test Checklist

Use this checklist after `mythos/SETUP.md` to verify the system end-to-end
in a real Discord guild. The scenario mirrors the benchmark's
`user-scenario.md`: building a Chrome translator extension.

## Setup

- [ ] All env vars from `mythos/.env.example` are exported (or
      `mythos.yaml` is in place).
- [ ] `claude --version`, `codex --version`, `gemini --version` all succeed
      in the same shell.
- [ ] The proxy at `$ANTHROPIC_BASE_URL` (default `http://127.0.0.1:4141`)
      responds to a simple curl.
- [ ] The bot is invited to the guild and you can see it in the member list.

## Phase 1 — Intake

- [ ] Run `python -m mythos` and wait for `Mythos starting; main channel=...`.
- [ ] In the configured main channel, post:
      > I want to build a chrome extension, a translator extension, when I
      > double click on the website it will search in its built-in dictionary
      > and give me the translation.
- [ ] Within ~10s, **Athena** replies in the main channel pointing at a new
      `proj-XXXXXXXX` channel.
- [ ] The new project channel appears in the channel list.
- [ ] Post a non-project chatter message (e.g. "lol") in the main channel
      and verify Mythos does NOT create a second project.

## Phase 2 — Planning + review

- [ ] Inside the project channel, **Athena** posts a welcome.
- [ ] **Prometheus** posts either:
      - a numbered set of clarifying questions prefixed with `QUESTIONS:`, OR
      - a draft design spec containing Scope, User-facing behavior,
        Technical approach, Assumptions, Risks, and Workstream candidates.
- [ ] If Prometheus asks questions, answer them in the project channel —
      Prometheus then re-runs and posts the design spec.
- [ ] **Argus** posts a review (either `APPROVED` or `- REQUIRED CHANGE: ...`
      lines).
- [ ] If Argus requested changes, Prometheus revises and Argus re-reviews
      automatically.
- [ ] **Athena** prompts you with: *"Reply **approve** in this channel to
      start implementation, or describe revisions."*

## Phase 3 — Approval gate

- [ ] BEFORE replying `approve`, verify NO `*-frontend`, `*-backend`, or
      `*-test` channels have been created.
- [ ] Reply with a **revision request** instead (e.g. "actually use a
      remote API for the dictionary"). Verify Mythos bounces the work
      back to Prometheus and re-reviews — still no implementation channels.
- [ ] Now reply `approve`.

## Phase 4 — Decomposition + implementation

- [ ] Within ~30s, **Athena** posts: *"created workstream channels:"* with
      links to `<project>-frontend`, `<project>-backend`, `<project>-test`.
- [ ] In `*-frontend`: **Apollo** posts a handoff (from Athena), then a
      start message, then implementation output, then a ✅ completion line.
- [ ] In `*-backend`: **Atlas** does the same.
- [ ] **Apollo and Atlas only post in their own channels** — neither
      shows up in `*-test` or in the project channel directly.
- [ ] After both implementation workstreams complete, **Hephaestus** posts
      validation results in `*-test`.

## Phase 5 — Completion

- [ ] **Athena** posts a final summary in the project channel listing
      every workstream's status.
- [ ] No further messages arrive from any agent.
- [ ] The state file at `$MYTHOS_STATE_DIR/projects/<project_id>.json`
      contains `"phase": "complete"`.

## Phase 6 — Concurrency

- [ ] In the main channel, post a SECOND project request from a different
      account (or just a different worded request from yours):
      > build a small notes app with a simple backend api
- [ ] A second `proj-XXXXXXXX` channel is created.
- [ ] Both projects continue to operate independently — answers in one
      project channel do not affect the other.
- [ ] Approving one project does NOT cause workstream channels to be
      created for the other.

## Phase 7 — Restart resilience

- [ ] With at least one project sitting in `awaiting_approval`, kill mythos
      with Ctrl-C.
- [ ] Restart `python -m mythos`.
- [ ] Reply `approve` in that project's channel.
- [ ] Mythos picks up where it left off and creates the workstream channels.

If every box above is checked, the happy path passes.
