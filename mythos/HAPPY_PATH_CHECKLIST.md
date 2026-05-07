# Mythos Happy-Path Manual Test Checklist

This is the **real-Discord** smoke test. Follow it after completing
`SETUP.md`. Each step lists what you do and what you should observe;
mark each box as you go.

## Pre-flight

- [ ] `python -m mythos.run --log-level INFO` is running and the log
      shows `Mythos discord adapter ready as <BotName>#NNNN`.
- [ ] You can see the Mythos bot in your guild's member list.
- [ ] You are in the Discord channel whose ID you put in
      `MYTHOS_MAIN_CHANNEL_ID`.

## 1. Intake

- [ ] In the main channel, post:

  > I want to build a Chrome extension translator: when I double-click
  > on a website it should look up a built-in dictionary and show the
  > translation.

- [ ] **Within 5 seconds**, Athena replies in the main channel with
      `Got it — I created **<project name>** (proj_xxxxxxxx). Continuing
      in the project's #planning channel.`
- [ ] A new category appears named `proj-<slug>`.
- [ ] A `#planning` text channel appears inside it.
- [ ] In `#planning`, Athena posts the intake summary and ends with
      `@Prometheus please draft a design spec.`

## 2. Drafting

- [ ] Prometheus posts in `#planning`. EITHER:
      - It asks 1–3 numbered clarifying questions ending with `@user`.
        Reply with the answers and ping Prometheus again
        (`@Prometheus offline EN/ES, tooltip presentation`). Repeat at
        most twice. **OR**
      - It posts a draft summary ending with `@Argus please review.`
- [ ] When Prometheus has enough information, it has written
      `design-spec.md` into `./workspaces/<project_id>/`.

## 3. Review

- [ ] Argus posts review comments in `#planning` grouped under
      `**Must fix:**`, `**Should fix:**`, `**Nit:**` (each header may
      contain `- (none)`), ending with `@Athena`.
- [ ] Athena follows with a 5–8 line summary and a final line:
      `Reply` `` `approve` `` ` / ` `` `lgtm` `` ` to proceed, or describe
      changes. @user`.

## 4. Approval

- [ ] In `#planning`, EITHER react `✅` or `👍` on Athena's
      approval-request message **OR** reply `approve` (or `lgtm`).
- [ ] Athena posts `Approved (spec hash …). I'll now decompose the
      work.` in `#planning`.
- [ ] A status update appears in the main channel referencing the
      project name.

## 5. Decomposition + Implementation

- [ ] Athena posts a fenced ```` ```decomposition.json ```` block in
      `#planning`. The orchestrator parses it; matching sub-channels
      appear (`#frontend`, `#backend`, `#test` — only the ones present
      in the manifest).
- [ ] Each sub-channel receives a briefing that ends with
      `@<SpecialistDisplayName> please implement and end with @Athena
      when done.`
- [ ] Apollo (in `#frontend`) and Atlas (in `#backend`) each post a
      completion message ending with `@Athena` and listing the files
      they touched. Files appear under
      `./workspaces/<project_id>/`.

## 6. Tests

- [ ] After both implementation specialists report completion,
      Hephaestus is dispatched in `#test` (it does NOT start before
      its dependencies finish — verify by reading the bot log).
- [ ] Hephaestus posts a completion message ending with `@Athena`.

## 7. Wrap-up

- [ ] Athena posts a one-liner in the main channel:
      `**<project name>** (proj_xxxxxxxx): all scopes complete. ✅`
- [ ] `./mythos-state.json` shows the project's status as `done`.

## Concurrent isolation check (optional, satisfies project-isolation
spec)

- [ ] Repeat **step 1** with a second, unrelated project request before
      step 4 is reached on the first one.
- [ ] Verify the two projects each get their own category, sub-channels,
      and workspace directory; messages in one project's `#planning`
      never appear in the other's.

## Negative checks

- [ ] Mention `@Apollo` in `#planning` — the bot logs the mention but
      Apollo does NOT respond. (Channel-scoped activation.)
- [ ] Reply with free text after Athena's approval ask (e.g. "drop the
      popup, use a tooltip"). The orchestrator treats it as a revision
      request and re-pings Prometheus, capped at
      `MYTHOS_MAX_PLANNING_ROUNDS` rounds.

## Logs / metrics to spot-check

- [ ] Every dispatch line in the bot's log contains `project_id=`,
      `channel_id=`, `role=`, `agent=`, `runner=`, and **does not**
      contain any API keys or `ANTHROPIC_AUTH_TOKEN` value.
- [ ] `./mythos-state.json` is updated atomically (no `.tmp` left
      behind on disk).
