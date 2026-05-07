# Happy-Path Checklist (manual Discord smoke test)

Run this in a real Discord server after completing `SETUP.md`.

## Pre-flight

- [ ] `mythos` bot is online (green dot in member list).
- [ ] You can see the main intake channel.
- [ ] Bot has **Manage Channels** permission in the guild.
- [ ] `claude`, `codex`, and `gemini` CLIs are on `$PATH` for the bot's process.

## 1. Intake creates a project channel

- [ ] In the main channel, post:
      `I want to build a Chrome translator extension. When I double-click on a website, look up the word in a built-in dictionary and show the translation.`
- [ ] **Athena** replies in the main channel with a `<#…>` link to the new
      project channel within ~10 seconds.
- [ ] Exactly **one** new channel appears (named like `proj-translator-…`).

## 2. Planning runs in the project channel only

- [ ] In the project channel: **Athena** posts an acknowledgement.
- [ ] **Prometheus** posts either a clarifying question or a draft design
      spec wrapped between `<<MYTHOS:DESIGN_BEGIN>>` / `END` markers.
- [ ] If Prometheus asks a question, answer it in the project channel.
      Prometheus then posts the design spec.

## 3. Review

- [ ] **Argus** posts review comments + a decision marker
      (`<<MYTHOS:DECISION:approve|request_changes|block>>`).
- [ ] **Athena** summarises the review and either:
   - asks you to type `approve` (decision was `approve`), or
   - tells you a revision is in flight (decision was `request_changes`).

## 4. Approval gate

- [ ] No frontend / backend / test channel has been created yet.
- [ ] No implementation agent has posted anywhere.
- [ ] Type `approve` in the project channel.

## 5. Decomposition + implementation

- [ ] Three new channels appear:
      `proj-<slug>-frontend`, `proj-<slug>-backend`, `proj-<slug>-test`.
- [ ] **Apollo** posts in the frontend channel (and only there).
- [ ] **Atlas** posts in the backend channel (and only there).
- [ ] After both finish, **Hephaestus** posts in the test channel.
- [ ] **Athena** posts a "All phases complete" line in the project channel.

## 6. Cross-project isolation

- [ ] In the main channel, post a *second* request, e.g.
      `Build me a simple markdown editor`.
- [ ] A new project channel appears with a different slug.
- [ ] Messages in project A's channels never appear in project B's, and
      vice versa.
- [ ] Type a message in project B's frontend channel — Apollo for project A
      does not respond there.

## 7. Workspace isolation

On the host:

- [ ] `ls $MYTHOS_WORKSPACE_ROOT` shows two directories
      (`<slug-A>__proj_<id>` and `<slug-B>__proj_<id>`).
- [ ] Each contains `frontend/`, `backend/`, `test/` subdirs.

## 8. State persistence

- [ ] `ls $MYTHOS_STATE_DIR` shows one JSON file per project.
- [ ] Restart the bot (`Ctrl-C`, then re-run `python -m mythos.bot ...`).
- [ ] Old projects are still listed in the JSON files; new requests do not
      collide with them.

If every box is ticked, the system passes the user-scenario.md happy path.
