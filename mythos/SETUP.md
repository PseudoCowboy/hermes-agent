# Mythos Setup Guide

This is the operator runbook for the Mythos Discord-based multi-agent
development system. Follow it once on the host where you'll run the bot.

## 0. Prerequisites

- Python 3.11+
- The hermes-agent repo checked out (you are reading this from inside it)
- Local CLIs reachable on `$PATH`:
  - `claude` (Claude Code CLI)
  - `codex` (Codex CLI)
  - `gemini` (Gemini CLI) — uses its own preconfigured creds
- A Discord server you administer

## 1. Install Python deps

From the repo root:

```sh
pip install -e '.[messaging]'   # installs discord.py 2.7+
pip install pyyaml pytest pytest-asyncio
```

## 2. Create the Discord bot

1. Visit <https://discord.com/developers/applications> and click **New Application**. Name it whatever you want (e.g. `Mythos`).
2. Go to **Bot** in the sidebar:
   - Click **Add Bot**.
   - Click **Reset Token** and copy the token. This is your `DISCORD_BOT_TOKEN`. Never commit it.
   - Under **Privileged Gateway Intents** enable:
     - `MESSAGE CONTENT INTENT` (required so the bot can read user messages)
     - `SERVER MEMBERS INTENT` (optional)
3. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `View Channels`, `Send Messages`, `Manage Channels`, `Manage Threads`, `Read Message History`, `Mention Everyone`, `Embed Links`, `Attach Files`
   - Copy the generated URL and visit it to invite the bot to your server.

## 3. Discover the channel/guild IDs

In Discord, enable **Settings → Advanced → Developer Mode**, then right-click on:

- Your server → **Copy Server ID** → this is `DISCORD_GUILD_ID`.
- The channel where users will post project requests → **Copy Channel ID** → this is `MYTHOS_MAIN_CHANNEL_ID`.
- (Optional) A category to nest project channels under → `MYTHOS_PROJECT_CATEGORY_ID`.

## 4. Configure environment variables

Copy `mythos/.env.example` to `.env` and fill it in. Minimum required vars:

| Var | Purpose |
|-----|---------|
| `DISCORD_BOT_TOKEN` | The bot token from step 2 |
| `DISCORD_GUILD_ID` | The Discord server ID |
| `MYTHOS_MAIN_CHANNEL_ID` | Channel users post requests in |
| `MYTHOS_PROJECT_CATEGORY_ID` | (optional) Discord category for project channels |
| `MYTHOS_WORKSPACES_ROOT` | (optional) where per-project workspaces live (default `./mythos_workspaces`) |
| `MYTHOS_STATE_DIR` | (optional) where JSON state lives (default `./mythos_state`) |
| `MYTHOS_CONFIG` | (optional) path to a `config.yaml` overriding agent commands |

The agent CLIs do **not** need `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars
in the operator's shell — Mythos forwards the **role-scoped** values
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_BASE_URL`,
`OPENAI_API_KEY`) directly into each subprocess. Defaults match the
benchmark's local proxy at `http://127.0.0.1:4141`.

If you want to point at the real provider APIs instead of the local proxy,
override per role:

```sh
export MYTHOS_REVIEW_BASE_URL=https://api.openai.com/v1
export MYTHOS_REVIEW_AUTH_TOKEN=sk-...
export MYTHOS_BACKEND_BASE_URL=https://api.anthropic.com
export MYTHOS_BACKEND_AUTH_TOKEN=sk-ant-...
```

For the **Gemini** agent (Apollo) the CLI uses its own preconfigured
credentials on the host. If you have not yet authenticated, run `gemini`
once interactively to log in (or set `GEMINI_API_KEY` / `GOOGLE_API_KEY`
following the Gemini CLI's own docs). Mythos does not inject these.

## 5. (Optional) Customize agent commands

If you want to override the CLI invocation (e.g. swap models), copy
`mythos/config.example.yaml` to `mythos/config.yaml` and edit it. Set
`MYTHOS_CONFIG=$(pwd)/mythos/config.yaml` so the loader picks it up.

The benchmark-mandated defaults are:

| Role | Codename | Provider | Command |
|------|----------|----------|---------|
| main | Athena | claude | `claude --dangerously-skip-permissions --effort high --print` |
| draft_plan | Prometheus | claude | `claude --dangerously-skip-permissions --effort high --print` |
| backend | Atlas | claude | `claude --dangerously-skip-permissions --effort high --print` |
| review | Argus | codex | `codex exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check` |
| test | Hephaestus | codex | `codex exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check` |
| frontend | Apollo | gemini | `gemini -m gemini-3.1-pro-preview -y` |

## 6. Run the bot

Smoke test (no Discord, fakes everything):

```sh
python -m mythos --dry-run
```

Real run:

```sh
set -a; source .env; set +a
python -m mythos
```

The bot connects, listens to the main channel, and runs the workflow.

## 7. How it ties into hermes-agent

Mythos lives alongside the hermes gateway and reuses the host's Python
runtime. It does **not** require the hermes gateway to be running — it has
its own Discord transport (`mythos.discord_bot.RealDiscordTransport`) so
it can run standalone or beside the existing gateway.

If you want to wire it into the hermes plugin system, register the
`Orchestrator` in your hermes startup hook and route Discord events to
`Orchestrator.handle_incoming`. The transport is a Protocol; substitute
your own to integrate with `gateway/platforms/discord.py`'s
`DiscordPlatformAdapter`.

## 8. Troubleshooting

- **Bot connects but never replies in main channel:** confirm `MESSAGE
  CONTENT INTENT` is enabled in the Discord developer portal, and that the
  bot has `Read Message History` and `Send Messages` in the channel.
- **`Manage Channels` errors:** the bot role must have that permission.
- **Subprocess `command not found`:** ensure `claude`, `codex`, `gemini`
  are on the bot process's `$PATH`. Use absolute paths in
  `mythos/config.yaml` if needed.
- **`use_fake_runners: true` left on:** in dry-run/test config; flip to
  `false` (or unset `MYTHOS_USE_FAKE_RUNNERS`) for production.
- **Approval message ignored:** only the user who created the project
  (their Discord ID matches `owner_user_id`) can approve.

See `mythos/HAPPY_PATH_CHECKLIST.md` for the in-Discord smoke test.
