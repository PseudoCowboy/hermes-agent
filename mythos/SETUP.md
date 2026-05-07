# Mythos — Operator Setup

Mythos is a Discord-based multi-agent development orchestrator built on top
of hermes-agent. A user posts a software request in a Discord channel; the
system creates an isolated project channel, drafts a design spec, has it
reviewed, asks the user to approve, then decomposes the work into
frontend / backend / test workstreams driven by separate agents in their
own channels.

This document is the runbook for getting a fresh deployment running.

---

## 1. Prerequisites

On the host machine:

* Python 3.11+ with hermes-agent already installed (`pip install -e .` from
  the repo root, or use the repo's standard setup script).
* Three CLI binaries on `PATH`:
  * `claude` — Claude Code CLI (used by Athena, Prometheus, Atlas)
  * `codex` — Codex CLI (used by Argus, Hephaestus)
  * `gemini` — Gemini CLI (used by Apollo)

  Verify with `which claude codex gemini`.
* Optional but recommended: a local LLM relay listening on
  `http://127.0.0.1:4141` if you want to fan all Anthropic/OpenAI traffic
  through one place. Mythos defaults to that endpoint with placeholder
  credentials. To use real APIs directly, override the endpoints in
  `mythos/config.yaml` and supply real keys.

---

## 2. Discord bot setup

1. Open <https://discord.com/developers/applications> and click
   **New Application**. Name it (e.g. *Mythos*) and create.
2. In the left sidebar, click **Bot** → **Add Bot**. Copy the bot token —
   you'll set it as `DISCORD_BOT_TOKEN`.
3. Under **Privileged Gateway Intents**, enable:
   * **MESSAGE CONTENT INTENT** (required so the bot can read messages)
   * **SERVER MEMBERS INTENT** (recommended — lets the bot resolve user
     mentions in approval messages)
4. Under **OAuth2 → URL Generator**:
   * Scopes: `bot`, `applications.commands`
   * Bot Permissions:
     * View Channels
     * Send Messages
     * Manage Channels (required for creating per-project channels)
     * Read Message History
     * Embed Links
     * Mention @everyone, @here and All Roles (optional)
   * Copy the generated URL and visit it to invite the bot to your server.
5. In Discord (with developer mode on), right-click your server →
   **Copy ID** to get `DISCORD_GUILD_ID`. Right-click the channel you want
   to use as the main intake channel → **Copy ID** to get
   `MYTHOS_MAIN_CHANNEL_ID`.

The bot needs `MANAGE_CHANNELS` because every project gets its own
channel and every workstream gets a sub-channel.

---

## 3. Required environment variables

Copy `mythos/.env.example` to `mythos/.env` (or export them however you
manage env vars). Required:

| Variable                  | Purpose |
|---------------------------|---------|
| `DISCORD_BOT_TOKEN`       | Bot token from step 2.2. |
| `DISCORD_GUILD_ID`        | Numeric server ID. |
| `MYTHOS_MAIN_CHANNEL_ID`  | Numeric ID of the main intake channel. |

Optional:

| Variable                  | Purpose |
|---------------------------|---------|
| `MYTHOS_CONFIG`           | Path to a `config.yaml`. Defaults to `mythos/config.yaml` next to the package. |
| `MYTHOS_WORKSPACE_DIR`    | Where per-project working directories live. Defaults to `~/.hermes/mythos`. |
| `ANTHROPIC_API_KEY`       | Only needed if you override the default base URL to talk to Anthropic directly. |
| `OPENAI_API_KEY`          | Only needed if you override the default Codex base URL to talk to OpenAI directly. |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Only needed if you want to override Gemini's host-level credentials. |

---

## 4. Mythos config

Copy the example:

```bash
cp mythos/config.yaml.example mythos/config.yaml
```

Edit it to set any non-default values. The most useful keys:

```yaml
discord:
  guild_id: 123456789012345678
  main_channel_id: 123456789012345678

workspace_dir: ~/.hermes/mythos

# Approval keywords (case-insensitive prefix match) the user types in the
# project channel to move it from awaiting_approval -> decomposing.
approval_keywords:
  - approve
  - approved
  - lgtm
  - ship it

# How many revision rounds before Mythos stops auto-revising and asks the
# user to decide.
max_revision_rounds: 2

# Per-agent overrides — only needed if you don't want the build defaults.
agents:
  main:
    model: claude-opus-4.7-1m-internal
    base_url: http://127.0.0.1:4141
    env:
      ANTHROPIC_BASE_URL: http://127.0.0.1:4141
      ANTHROPIC_AUTH_TOKEN: dummy
      ANTHROPIC_MODEL: claude-opus-4.7-1m-internal
  review:
    model: gpt-5.5
    base_url: http://127.0.0.1:4141/v1
    env:
      OPENAI_BASE_URL: http://127.0.0.1:4141/v1
      OPENAI_API_KEY: dummy
  frontend:
    model: gemini-3.1-pro-preview
```

Defaults are equivalent to the build instructions:

* **Claude Code** (Athena, Prometheus, Atlas):
  `claude --dangerously-skip-permissions --effort high --print` with
  `ANTHROPIC_BASE_URL=http://127.0.0.1:4141`,
  `ANTHROPIC_AUTH_TOKEN=dummy`,
  `ANTHROPIC_MODEL=claude-opus-4.7-1m-internal`.
* **Codex** (Argus, Hephaestus):
  `codex exec -m gpt-5.5 -c model_reasoning_effort=high
  --skip-git-repo-check` with `OPENAI_BASE_URL=http://127.0.0.1:4141/v1`,
  `OPENAI_API_KEY=dummy`.
* **Gemini** (Apollo): `gemini -p <prompt> -m gemini-3.1-pro-preview -y`
  using the host's preconfigured Gemini credentials (no extra env).

---

## 5. Running Mythos

From the repo root:

```bash
# Required env (or set them in your shell rc):
export DISCORD_BOT_TOKEN=...
export DISCORD_GUILD_ID=...
export MYTHOS_MAIN_CHANNEL_ID=...

python -m mythos.app
```

You should see:

```
INFO mythos.orchestrator: orchestrator started; main_channel=...
INFO discord.client: logging in using static token
INFO discord.gateway: Shard ID None has connected to Gateway
```

Now post a project request in the main channel and follow it through.

### Running it under hermes-agent's gateway

If you want Mythos to share the Discord client with hermes-agent's
existing platform adapter (so a single bot identity handles both general
hermes-agent traffic and Mythos), pass the existing `DiscordAdapter` to
`HermesDiscordTransport(adapter=...)` instead of letting Mythos create its
own client. This avoids logging in twice. (See `mythos/transport.py`.)

The plain `python -m mythos.app` path runs Mythos with its own dedicated
discord.py client, which is the simplest deployment.

---

## 6. Verification

Run the integration test suite to confirm the install:

```bash
python -m pytest tests/mythos/ -q
```

Then walk through `mythos/HAPPY_PATH_CHECKLIST.md` against your real
Discord server.

---

## 7. Troubleshooting

* **"DISCORD_BOT_TOKEN is required"** at startup — environment variable
  not set; see section 3.
* **Bot connects but doesn't reply** — check the bot was invited with
  `MESSAGE CONTENT INTENT` enabled on the developer portal; also make sure
  `MYTHOS_MAIN_CHANNEL_ID` matches the channel you're posting in.
* **"failed to create a channel"** — bot is missing `Manage Channels` in
  the Discord guild role assigned to it.
* **CLI subprocess hangs / times out** — `claude` / `codex` / `gemini`
  needs to be on `PATH` and able to authenticate. Try running each by
  hand with the same env to reproduce.
