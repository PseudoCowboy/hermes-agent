# Mythos — happy-path manual checklist (real Discord)

Use this checklist to confirm an end-to-end real-Discord run after
following `mythos/SETUP.md`. Estimated total time: ~10 min.

Pre-flight:

- [ ] Bot is online in your guild (green dot).
- [ ] `python -m mythos.run` is running with no startup errors.
- [ ] You can see the **main channel** (id matches `MYTHOS_MAIN_CHANNEL_ID`).

## 1. Intake (User Story 1)

- [ ] In the **main channel**, post: `I want to build a Chrome translator extension that, when I double-click a word, looks it up in a built-in dictionary and shows the translation.`
- [ ] Within ~30s, **Athena** replies in the main channel with an
      acknowledgment that includes a project ID and channel name.
- [ ] A new text channel `proj-...-<id>` exists.
- [ ] The first message in the new channel contains:
  - the seed request quote
  - `@Prometheus` mention
  - the project ID

## 2. Drafting + clarifying questions (User Story 2)

- [ ] Within ~60s, **Prometheus** posts either:
  - clarifying questions (each prefixed `QUESTION:`), OR
  - a structured draft spec with sections Overview / Goals / Non-Goals /
    User Flows / Components / Open Questions.
- [ ] If clarifying questions appeared, answer them in the project channel
      and confirm Prometheus then posts a draft spec.

## 3. Review (User Story 3)

- [ ] Within ~60s of the draft, **Argus** posts a structured review
      (Strengths / Gaps / Risks / Recommended Changes) ending with
      `RECOMMENDATION: APPROVE | REVISE | REJECT`.
- [ ] **Athena** then posts a message asking the owner to reply
      `approve`, `revise <feedback>`, or `reject`.

## 4. Iteration (User Story 4)

- [ ] Reply `revise add a quick keyboard shortcut to toggle the overlay`.
- [ ] Confirm Athena forwards to Prometheus and a new spec version is posted.
- [ ] Argus produces a new review.
- [ ] Reply `approve`.
- [ ] Athena posts a confirmation that approval was recorded with the
      spec version number.

## 5. Decomposition (User Story 5)

- [ ] Within ~30s of approval, Athena posts that N sub-channels were
      created and lists them.
- [ ] In Discord's sidebar, the new sub-channels appear under a category
      `mythos-<id>`. Expect channels named `frontend-<id>`, `backend-<id>`,
      `test-<id>` (only the disciplines actually mentioned in the spec).
- [ ] Each sub-channel's first message includes the approved spec content
      and a ping to the assigned specialist.

## 6. Specialist work (User Story 6)

- [ ] Apollo posts in `frontend-<id>` only and ends with `FRONTEND WORK COMPLETE`.
- [ ] Atlas posts in `backend-<id>` only and ends with `BACKEND WORK COMPLETE`.
- [ ] Hephaestus posts in `test-<id>` only and ends with `TEST WORK COMPLETE`.
- [ ] No specialist's messages appear in the parent project channel.
- [ ] No specialist's messages appear in the wrong sub-channel.

## 7. Concurrency / isolation (User Story 7)

- [ ] From a second user account, post a different project request
      (e.g. `Build a CLI todo app`) in the main channel.
- [ ] Confirm a second project channel is created and the workflow proceeds
      independently.
- [ ] At any point, confirm messages in project A's channels do **not**
      reference project B's id and vice versa.

## 8. Edge cases (sanity)

- [ ] In the awaiting-approval state, post `looks ok i guess`. Athena should
      ask for an explicit `approve` / `revise` / `reject`.
- [ ] From a non-owner account, try to post `approve` in someone else's
      project channel. Athena should refuse.
- [ ] Post `revise X` more than `MYTHOS_MAX_ITERATIONS` times in a row.
      Athena should announce the cap was reached.

If every box is checked, the system passes the happy path.
