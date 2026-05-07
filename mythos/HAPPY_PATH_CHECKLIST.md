# Mythos Happy-Path Manual Checklist

Run this after `python -m mythos` is up and the bot is in your server.
Each step has an expected observation in Discord.

## Pre-flight

- [ ] Bot status is **Online** in your Discord server's member list.
- [ ] Bot has `View Channels`, `Send Messages`, `Manage Channels`, `Read Message History` permissions on the main channel.
- [ ] You can see the main channel's ID match `MYTHOS_MAIN_CHANNEL_ID`.

## Single-project happy path

1. **Post the project request** in the configured main channel:
   > I want to build a chrome extension, a translator extension, when I double click on a website, it will search in its built-in dictionary, give me the translation.

   ✅ Expected: Athena replies in the main channel with `project prj_xxxxxxxxxx accepted. Spinning up channel for: …`.

2. **Look for the new project channel** (named `prj-i-want-to-build-…`).

   ✅ Expected: a new text channel appears (under the configured category, if set).

3. **Inside the project channel**, expect Athena to post the original request and ping Prometheus.

   ✅ Expected: a `**Prometheus**:` message follows shortly after, either with a clarifying question or a `## Design Spec` block.

4. **If Prometheus asked a clarifying question**, answer it in the project channel (e.g. `Bundle the dictionary, no network calls.`).

   ✅ Expected: Athena acknowledges and Prometheus posts the design spec; then `**Argus**:` posts review comments; then Athena asks **Please reply approve …**.

5. **Reply `approve`** in the project channel.

   ✅ Expected: Athena posts `design approved. Decomposing work and creating implementation channels.` Within seconds, three new channels appear:
   - `…-frontend` (Apollo posts here)
   - `…-backend` (Atlas posts here)
   - `…-test` (Hephaestus posts here)

6. **Verify channel discipline**:
   - [ ] Apollo's messages appear **only** in the `-frontend` channel.
   - [ ] Atlas's messages appear **only** in the `-backend` channel.
   - [ ] Hephaestus's messages appear **only** in the `-test` channel.
   - [ ] None of them post in the project channel.

7. **Observe completion**:

   ✅ Expected: each specialist posts a `Completion Summary` (Apollo / Atlas) or `Test Report` (Hephaestus). Athena posts the final `project prj_xxx reached completion.` message.

## Concurrent-project test

1. Open two main-channel windows (or two browsers).
2. Post **two different requests** at roughly the same time, each from a different account.
3. Verify two distinct project channels appear, each with its own Athena acknowledgement and its own Prometheus post.
4. Approve project A, then approve project B.
5. Verify both reach `reached completion` independently.
6. Inspect `mythos_state/projects/`:
   - Two project sub-directories with different IDs.
   - Each has its own `project.json`, `channels.json`, `artifacts.json`.
7. Inspect `mythos_workspaces/`:
   - Two project sub-directories.
   - Approved-design artifacts under each `artifacts/approved-design.md`.

## Failure-handling spot check

1. Disable Discord's `Manage Channels` permission for the bot temporarily.
2. Post a new project request.
3. Athena should still acknowledge in the main channel; project will move to `failed` state in the JSON store. Re-grant the permission and restart for the next test.

## Approval-gate spot check (only owner can approve)

1. From an account other than the project's original poster, reply `approve` in the project channel during step 5 above.

   ✅ Expected: Athena posts `only the project owner can approve.` and the project stays in `awaiting_approval`.

## What success looks like

- ☑️ Each project gets its own channel set.
- ☑️ Specialist agents only speak in their assigned channel.
- ☑️ Implementation only starts after explicit user approval.
- ☑️ State and artifacts persist in `mythos_state/` and `mythos_workspaces/`.
- ☑️ The same flow works concurrently for multiple projects.
