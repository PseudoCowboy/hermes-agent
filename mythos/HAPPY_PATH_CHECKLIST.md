# Mythos — Manual Happy-Path Checklist

Run through this in your real Discord server after completing
`SETUP.md`. Each step has an explicit pass criterion. If any step fails,
stop and check the troubleshooting section of `SETUP.md`.

## Pre-flight

- [ ] `python -m pytest tests/mythos/ -q` is green.
- [ ] Bot is online in your guild (green dot in the member list).
- [ ] You have `Manage Channels` permission yourself, so you can clean up
      after the run.

## Phase 1 — Intake

1. [ ] In the configured main channel, post:

       ```
       I want to build a Chrome extension translator: when I double-click
       a word it looks it up in a built-in dictionary and shows the
       translation.
       ```

   **Pass criteria:**
   - [ ] Within ~30 s, the bot replies in the main channel mentioning the
         new project channel name.
   - [ ] A new text channel exists, named `proj-<id>-<slug>`, e.g.
         `proj-a1b2c3-chrome-translator-extension`.
   - [ ] In the new project channel, **Athena** has posted the original
         request and pinged **Prometheus**.

## Phase 2 — Draft and review

2. [ ] Within ~60 s, **Prometheus** has either posted a clarifying
      question (label `Prometheus: I need a clarification`) **or** a full
      design spec.

   - [ ] If clarifying: answer the question in the project channel and
         wait for the spec.

3. [ ] Once a spec is posted, **Argus** posts a review within ~60 s.
   - [ ] Review ends with `STATUS: APPROVE` or `STATUS: REVISE`.
   - [ ] If REVISE, the loop runs again automatically (up to
         `max_revision_rounds`, default 2).

4. [ ] **Athena** posts the approval prompt:
       *"design spec is ready for your decision. Reply with `approve`..."*

## Phase 3 — Approval

5. [ ] In the project channel, post: `approve`

   **Pass criteria:**
   - [ ] **Athena** acknowledges the approval and announces decomposition.
   - [ ] Three new channels appear:
     * `<project-channel>-frontend`
     * `<project-channel>-backend`
     * `<project-channel>-test`
   - [ ] Each new channel has a "pinging <agent>" handoff message.

## Phase 4 — Specialist execution

6. [ ] Within a few minutes:
   - [ ] **Apollo** posts in the frontend channel and ends with
         `STATUS: DONE` (or `STATUS: BLOCKED`).
   - [ ] **Atlas** does the same in the backend channel.
   - [ ] **Hephaestus** does the same in the test channel.
   - [ ] **Athena** posts a completion summary in the *project* channel
         pointing to each workstream channel.

## Phase 5 — Isolation check

7. [ ] In the **frontend** channel, post: `nice progress`

   **Pass criteria:**
   - [ ] Apollo replies in the same channel only.
   - [ ] No message appears in the project channel, backend channel, or
         test channel.

8. [ ] Open a second project from the main channel with a different
      request, e.g.:

       ```
       Build a CLI tool that converts CSV files to JSON.
       ```

   **Pass criteria:**
   - [ ] A second `proj-…` channel is created with a different ID.
   - [ ] All Phase 1–4 messages for the second project happen in *its*
         channels — never in the first project's channels.

## Phase 6 — Edge cases (optional but recommended)

9. [ ] In the main channel, post `hello team`. Athena should respond
      with "doesn't look like a project request" and **not** create a
      channel.

10. [ ] Start a third project but reply with `please change the storage
       to use IndexedDB instead of localStorage` instead of `approve`.
       The system should treat that as change requests and re-run the
       draft / review loop.

11. [ ] Stop the bot mid-run. Restart `python -m mythos.app`. Existing
       project state is in-memory only (current implementation), so
       in-flight projects will be orphaned — this is expected and noted
       in `TEST_REPORT.md`.

When every box above is checked, Mythos is verified end-to-end.
