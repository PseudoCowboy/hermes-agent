# Mythos Multi-Bot Identity — Spec (BMAD)

**Date:** 2026-05-12
**Status:** Approved (verbal)
**Scope:** Extend `bench/claude-bmad` so each Mythos role posts to Discord
under its own bot identity, instead of all roles sharing one bot.

## Product Brief

### Problem
Today every agent role (Hermes, Prometheus, Argus, Hephaestus, Apollo,
Atlas) posts through a single Discord bot. Users distinguish speakers
only by a leading `[Hermes · Main]` text tag. Avatars, usernames, and
mention semantics all collapse to a single account, which:
- makes long project channels visually noisy
- prevents per-role permissioning at the Discord layer
- prevents users from filtering / muting individual agents

### Goal
Each role posts as its own Discord bot account with its own username and
avatar. The orchestrator transparently routes each `send` to the right
underlying connection. No change to the project lifecycle, channel
layout, or CLI invocation contracts.

### Non-goals
- Per-project token rotation
- Slash commands / interactions
- Any role besides the six in `Role` enum getting its own bot
- Replacing the in-memory test double

## Requirements (PRD)

| # | Requirement |
|---|---|
| R1 | Operator can configure 1..N bot tokens, one per role, via `MYTHOS_BOT_TOKENS` (JSON dict). |
| R2 | If `MYTHOS_BOT_TOKENS` is absent but legacy `DISCORD_BOT_TOKEN` is set, the system falls back to single-bot mode (current behavior). |
| R3 | If a role has no token configured in multi-bot mode, its messages fall back to Hermes's bot. |
| R4 | Hermes's bot is the only inbound listener and the only client that creates categories/channels. |
| R5 | In multi-bot mode, the `[Role · Title]` text prefix is dropped from outbound messages (identity is conveyed by the bot's Discord username). |
| R6 | All existing tests in `tests/mythos/` continue to pass without modification of their assertions about the in-memory IO. |
| R7 | New tests cover: config parsing of `MYTHOS_BOT_TOKENS`, multi-bot routing, Hermes fallback, and that prefix-stripping only applies in multi-bot mode. |

## Architecture

### Components touched
- `mythos/config.py` — new field `bot_tokens: Dict[Role, str]`, parses
  `MYTHOS_BOT_TOKENS` JSON env var.
- `mythos/discord_io.py` —
  - `DiscordIO.send` signature gains optional `role: Optional[Role] = None`.
  - New `MultiBotDiscordIO` holds one `RealDiscordIO`-like connection
    per role. Hermes's connection is the inbound + channel-mgmt one;
    other roles run minimal send-only clients.
  - `InMemoryDiscordIO.send` accepts and records `role` for tests.
- `mythos/orchestrator.py` — `_post` / `_post_long` / `_post_error`
  pass `role=` to `discord.send`. Tag prefix is conditional on
  `discord.uses_role_identity` flag (true for `MultiBotDiscordIO`).
- `mythos/__main__.py` — picks `MultiBotDiscordIO` if `cfg.bot_tokens`,
  else `RealDiscordIO`.
- `mythos/SETUP.md` — adds a "Multi-bot mode" section.

### Data flow

```
            ┌──────────────────────────────────────────────┐
            │              MythosOrchestrator              │
            │   _post(channel_id, role, body)              │
            └────────────────┬─────────────────────────────┘
                             │ send(channel_id, body, role=role)
                             ▼
                ┌────────────────────────────┐
                │     MultiBotDiscordIO      │
                │  clients: {role: RealIO}   │
                └─────┬──────────────────────┘
                      │ pick clients[role] or clients[HERMES]
                      ▼
              one of N discord.py Bot connections
```

Inbound: only Hermes's underlying `discord.py` client registers an
`on_message` handler; the orchestrator-facing `on_message` registration
is delegated to it.

Channel/category creation: routed unconditionally through Hermes's
client (only one needs `Manage Channels`).

### Config schema

```bash
# Multi-bot mode (preferred)
export MYTHOS_BOT_TOKENS='{"hermes":"MTQ...","prometheus":"MTQ...","argus":"MTQ...","hephaestus":"MTQ...","apollo":"MTQ...","atlas":"MTQ..."}'

# Legacy single-bot fallback (still works, unchanged behavior)
export DISCORD_BOT_TOKEN='MTQ...'
```

If both are set, `MYTHOS_BOT_TOKENS` wins. Roles missing from the JSON
fall back to whichever bot is bound to Hermes (which itself falls back
to `DISCORD_BOT_TOKEN` if `"hermes"` is absent from the JSON).

### Backwards compatibility
- Single-bot deployments: no env change, no behavioral change. The
  `[Role · Title]` text prefix continues to identify speakers.
- Existing integration tests in `tests/mythos/` use `InMemoryDiscordIO`,
  which keeps the prefix (it sets `uses_role_identity = False`). All
  existing assertions on message bodies remain valid.

## Test plan

| Test | What it proves |
|---|---|
| `test_config_parses_bot_tokens_json` | `MYTHOS_BOT_TOKENS` env var deserializes into `cfg.bot_tokens`. |
| `test_config_falls_back_to_single_bot` | When only `DISCORD_BOT_TOKEN` set, `bot_tokens` is empty. |
| `test_multibot_io_routes_by_role` | `MultiBotDiscordIO.send(..., role=R)` calls the R-specific client; missing roles route to Hermes. |
| `test_orchestrator_drops_prefix_in_multibot_mode` | When `discord.uses_role_identity` is True, posted bodies omit `[Role · Title]`. |
| `test_orchestrator_keeps_prefix_in_single_bot_mode` | Default in-memory IO still sees prefixed bodies — guards existing tests. |
| Existing `tests/mythos/` suite | No regressions. |

## Out of scope (this change)
- Distinct per-role permissions in Discord (operator concern; document only).
- Web-based bot management UI.
- Migrating channel ownership between bots.

---

# Addendum — Projects Index Channel + Short-Name Categories (2026-05-12)

**Status:** Implemented in same code base.

## Motivation
Long auto-generated slugs (e.g. `mythos-proj-chrome-extension-manifest-v3-20260512-263f02`) made the Discord sidebar unreadable, and there was no single place to scan all active projects. Replace with a 4-char short id and add a dedicated index channel.

## Changes
- **New env var `MYTHOS_PROJECTS_CHANNEL_ID`** — operator-provisioned text channel where Hermes posts a one-line card per new project (slug, 4-char id, created time, workspace path, intake goal).
- **`mythos/config.py`** — added `projects_channel_id: int` parsed from the env var.
- **`mythos/project_manager.py`** — added `short_id_for(slug)` (first 4 chars of the slug's 6-hex tail). Category renamed from `mythos-<slug>` to `mythos-<XXXX>`. Channels renamed from `<slug>-{general,frontend,backend,test}` to `<XXXX>-{general,frontend,backend,test}`.
- **`mythos/orchestrator.py`** — `_handle_main` now also posts the project card to `projects_channel_id` when configured. Falls back silently when the env var is unset (single-channel legacy behaviour preserved for tests).
- Legacy long-name behaviour is **removed**, not toggleable. In-flight projects from before the change are cleaned up out-of-band.

## Tests
Existing `tests/mythos/` suite unaffected (in-memory IO records the synthesized names; no test asserted on the long form). Smoke-tested live on the dict-pop Chrome MV3 scenario after deploy.
