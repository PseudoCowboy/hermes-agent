# Mythos — Operator Setup Guide

Mythos is a Discord-based multi-agent development orchestrator. It runs as
a Python process that connects to Discord and shells out to three CLI
families to drive specialized agents:

| Codename     | Role             | CLI            | Model (default)                      |
|--------------|------------------|----------------|--------------------------------------|
| Athena       | Main agent       | Claude Code    | `claude-opus-4.7-1m-internal`        |
| Prometheus   | Draft-plan agent | Claude Code    | `claude-opus-4.7-1m-internal`        |
| Argus        | Review agent     | Codex          | `gpt-5.5`                            |
| Hephaestus   | Test agent       | Codex          | `gpt-5.5`                            |
| Apollo       | Frontend agent   | Gemini CLI     | `gemini-3.1-pro-preview`             |
| Atlas        | Backend agent    | Claude Code    | `claude-opus-4.7-1m-internal`        |

## 1. Prerequisites

Install these on the host that will run mythos.

- **Python 3.11+** (the hermes-agent repo's interpreter is fine).
- **Claude Code CLI** — the `claude` binary on `$PATH`.
- **Codex CLI** — the `codex` binary on `$PATH`. Verify with `codex exec --help`.
- **Gemini CLI** — the `gemini` binary on `$PATH`. Verify with `gemini --help`.
- **A model proxy** listening on `http://127.0.0.1:4141` that speaks both the
  Anthropic Messages API and the OpenAI Responses API. Mythos points Claude Code
  and Codex at this proxy by default. Gemini uses its own host credentials.

Install hermes-agent's messaging extras so `discord.py` is available:

```bash
pip install -e ".[messaging]"
```

## 2. Discord bot

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications)
   and create a new Application. Inside the application:
   - **Bot tab**: Add Bot. Copy the token — that's your `DISCORD_BOT_TOKEN`.
   - **Privileged Gateway Intents**: enable **MESSAGE CONTENT INTENT**.
2. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `View Channels`, `Send Messages`, `Manage Channels`,
     `Read Message History`, `Mention Everyone` (optional), `Use External Emojis`.
   - Open the generated URL and invite the bot to the guild.
3. In Discord, **enable Developer Mode** (User Settings → Advanced → Developer Mode).
   Then right-click your guild icon → **Copy Server ID** for `DISCORD_GUILD_ID`.
4. Pick the channel that will act as the **main intake channel**. Right-click →
   **Copy Channel ID** for `MYTHOS_MAIN_CHANNEL_ID`.

## 3. Environment variables

Copy `mythos/.env.example` to `.env` and fill in the values:

```bash
# ── Discord (required) ──────────────────────────────────────────────────
DISCORD_BOT_TOKEN=...              # from step 2 above
DISCORD_GUILD_ID=000000000000000000
MYTHOS_MAIN_CHANNEL_ID=000000000000000000
MYTHOS_ALLOWED_USER_IDS=           # optional, comma-separated; empty = all

# ── Mythos (optional — defaults are listed in mythos/config.py) ─────────
MYTHOS_STATE_DIR=~/.hermes/mythos
MYTHOS_WORKSPACE_ROOT=~/.hermes/mythos/workspaces

# ── Anthropic / Claude Code agents (Athena, Prometheus, Atlas) ─────────
# Defaults below match the benchmark spec — only override if you have a
# different proxy URL or want to point at the real Anthropic endpoint.
ANTHROPIC_BASE_URL=http://127.0.0.1:4141
ANTHROPIC_AUTH_TOKEN=dummy
ANTHROPIC_MODEL=claude-opus-4.7-1m-internal

# ── OpenAI / Codex agents (Argus, Hephaestus) ──────────────────────────
OPENAI_BASE_URL=http://127.0.0.1:4141/v1
OPENAI_API_KEY=dummy

# ── Gemini agent (Apollo) ──────────────────────────────────────────────
# No env required — Apollo uses gemini's own preconfigured credentials.
# If you need to override:
# GEMINI_API_KEY=...
# GOOGLE_API_KEY=...
```

> Note: Mythos passes `ANTHROPIC_BASE_URL` / `OPENAI_API_KEY` / etc. into the
> child processes via `mythos/roles.py`. If you don't set them in the
> shell, the defaults from `roles.py` win. Set the env in the shell if you
> need to override on a particular host.

## 4. Optional `mythos.yaml`

If you'd rather not use env vars, drop a `mythos.yaml` in either the working
directory or `~/.hermes/mythos.yaml`. Anything you set in the YAML is the
default; env vars still win.

```yaml
mythos:
  discord:
    bot_token: "..."          # prefer env var for secrets
    guild_id: "1234567890"
    main_channel_id: "9876543210"
    project_channel_prefix: "proj-"
    workstream_channel_template: "{project}-{workstream}"
    archive_on_complete: false
    allowed_user_ids: ["111", "222"]
  workspace:
    root: "~/.hermes/mythos/workspaces"
  state_dir: "~/.hermes/mythos"
  agents:
    main:
      argv: ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
      env:
        ANTHROPIC_MODEL: "claude-opus-4.7-1m-internal"
    review:
      argv: ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"]
      env:
        OPENAI_BASE_URL: "http://127.0.0.1:4141/v1"
    frontend:
      argv: ["gemini", "-m", "gemini-3.1-pro-preview", "-y"]
```

The schema accepts any role from
`AgentRole` — `main`, `draft_plan`, `review`, `test`, `frontend`, `backend` —
and merges your `argv`/`env` over the role's defaults.

## 5. Run it

```bash
# from the hermes-agent repo root
python -m mythos                 # uses ./mythos.yaml or ~/.hermes/mythos.yaml + env
# or
python -m mythos /path/to/mythos.yaml
```

The bot connects to Discord and starts listening for project requests in the
configured main channel. You can leave it running under `tmux`, systemd, or
hermes-agent's existing process supervisor — it makes no assumptions beyond
being able to spawn subprocesses.

## 6. Hermes-agent integration touchpoints

Mythos lives **alongside** hermes-agent's existing gateway, not inside it.
Specifically:

- `mythos.discord_adapter.DiscordClient` is its own discord.py client — it
  does not steal events from `gateway/platforms/discord.py`. If you want
  hermes to share a single bot connection, configure mythos with a different
  bot/token, or write a small adapter that proxies messages from your existing
  `BasePlatformAdapter` into `Orchestrator.handle_message`.
- Subprocess management uses `subprocess.Popen` directly. `tools/process_registry.py`
  is available if you want long-running agent processes registered for
  hermes-style supervision; see `mythos/runners.py:SubprocessRunner.run` for
  the hook point.

## 7. Smoke test

After starting `python -m mythos`, post in the main channel:

```
I want to build a chrome extension, a translator extension, when I double click on the website it will search in its built-in dictionary and give me the translation.
```

Within ~10s you should see:

1. A reply from **Athena** in the main channel naming the new project channel.
2. The new `proj-XXXXXXXX` channel with the original request, a draft from
   **Prometheus**, and a review from **Argus**.

Reply `approve` in the project channel and Mythos will:

3. Create `<project>-frontend`, `<project>-backend`, and `<project>-test` channels.
4. **Apollo / Atlas** post implementation outputs in their own channels.
5. **Hephaestus** validates after implementation.
6. **Athena** posts a final project summary back in the project channel.

For the full manual checklist see `mythos/HAPPY_PATH_CHECKLIST.md`.

## 8. Troubleshooting

- **Bot is online but never replies**: confirm MESSAGE CONTENT INTENT is
  enabled in the developer portal AND that the bot has read access to the
  configured main channel.
- **Channel creation fails with a 403**: the bot needs `Manage Channels`
  on the guild.
- **`claude: not found` / `codex: not found`**: the CLI isn't on `$PATH`
  for the user running mythos. Run `python -m mythos` from the same shell
  where `which claude` / `which codex` succeed.
- **Proxy connection refused**: verify the `http://127.0.0.1:4141` proxy
  is up before starting mythos. The agent CLIs surface the connection
  error as a non-zero exit; mythos posts that into the channel.
