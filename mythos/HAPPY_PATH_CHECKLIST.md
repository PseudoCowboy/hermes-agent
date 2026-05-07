# Mythos Happy-Path Manual Test Checklist

Use this in real Discord after `python -m mythos.run` is up. Each step lists
the expected outcome; check the box only after you observe it.

## Setup precheck

- [ ] Bot is online in Discord member list.
- [ ] `mythos online: 6 agents …` appears in the runner logs.
- [ ] The configured main channel exists and is visible to both you and the bot.
- [ ] The `mythos-projects` category exists (or the bot has `Manage Channels`
      to create it).

## Story 1 — Project intake

1. Post in main channel: **"Build a Chrome translator extension that triggers
   on double-click and uses a built-in dictionary."**
   - [ ] Within 30 s, Athena posts a reply in main channel that **mentions the
         new project channel link** (`<#…>`).
   - [ ] A new text channel appears under `mythos-projects`, named
         `proj-translator-extension` (or similar slug).
   - [ ] In that new channel, the welcome message names the request.
   - [ ] Prometheus posts an analysis message in the new channel.

2. Concurrent isolation: while step 1 finishes, post **another** request in
   main channel: **"Build a sticky-notes extension."**
   - [ ] A second project channel is created with a distinct name (e.g.
         `proj-sticky-notes-extension`).
   - [ ] Messages from project A do not appear in project B's channel and
         vice-versa.

## Story 2 — Draft, clarify, review, approve

3. If Prometheus asked clarifying questions:
   - [ ] Questions appear **only** in the project channel.
   - [ ] After you answer, Prometheus posts the spec in the same channel.
   - [ ] Otherwise: Prometheus posts the spec directly.
4. Argus posts a review with `recommendation: accept` or `request_changes`.
   - [ ] Review message appears in the **project** channel, not the main channel.
5. Athena prompts you for `approve` / `changes:` / `reject`.
6. Reply with **`changes: please add an offline fallback`**.
   - [ ] Prometheus posts a revised spec (v2).
   - [ ] Argus re-reviews.
7. Reply with **`approve`** in the project channel.
   - [ ] Athena posts a decomposition summary listing frontend/backend/test
         workstreams.

## Story 3 — Decomposition into specialist channels

8. Three new channels are created:
   - [ ] `proj-…-frontend`
   - [ ] `proj-…-backend`
   - [ ] `proj-…-test`
9. Each new channel receives a "Received spec v2; my scope is …" message
   from the corresponding agent (Apollo, Atlas, Hephaestus).

## Story 4 — Confined specialist work

10. Each specialist posts a completion message in **its own** channel.
    - [ ] Apollo's "frontend done" is in the frontend channel only.
    - [ ] Atlas's "backend done" is in the backend channel only.
    - [ ] Hephaestus's "test plan ready" is in the test channel only.
11. Athena posts a final summary in the project channel listing all three
    workstream channels and the completed status.
12. The project workspace under `${MYTHOS_WORKSPACE_ROOT}/<slug>/` contains:
    - [ ] `artifacts/spec-v1.md`, `artifacts/spec-v2.md`, `artifacts/review-v*.md`
    - [ ] `frontend/<artifact_path>` written by Apollo.
    - [ ] `backend/<artifact_path>` written by Atlas.
    - [ ] `test/<artifact_path>` written by Hephaestus.

## Edge case probes

13. While project A is in `awaiting_approval`, post `lolwhat` in its channel.
    - [ ] Athena posts a hint: needs explicit approve / changes / reject.
14. In the test channel, type `please tell me a joke`.
    - [ ] No specialist posts in the project channel in response. (Specialists
          confined to their channel per FR-022.)
15. Stop the runner, then start it again. Re-post in main channel — a new
    project should be created normally (state is stored in memory + JSONL log
    so historical state isn't restored, but new flows still work).

When all of the above are checked, the manual happy path passes.
