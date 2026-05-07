# Mythos — Operator Setup Runbook

Mythos is a Discord-based multi-agent development orchestrator built on top of
hermes-agent. It coordinates six role-specialised CLI agents (Athena,
Prometheus, Argus, Hephaestus, Apollo, Atlas) across per-project Discord
channels.

This runbook lists everything you must configure before pointing it at a real
Discord server.

---

## 1. Prerequisites on the host

Install these CLIs and confirm each runs from your `$PATH`:

| Agent       | Role             | CLI binary | Verify |
|-------------|------------------|-----------|--------|
| Athena, Prometheus, Atlas | main / planning / backend | `claude` (Claude Code CLI) | `claude --version` |
| Argus, Hephaestus | review / test | `codex` (OpenAI Codex CLI) | `codex --version` |
| Apollo | frontend | `gemini` (Google Gemini CLI) | `gemini --version` |

Mythos invokes each binary as a subprocess with these defaults:

- **Claude Code agents** — `claude --dangerously-skip-permissions --effort high --print`
  with `ANTHROPIC_BASE_URL=http://127.0.0.1:4141`,
  `ANTHROPIC_AUTH_TOKEN=dummy`,
  `ANTHROPIC_MODEL=claude-opus-4.7-1m-internal`.
- **Codex agents** — `codex exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check`
  with `OPENAI_BASE_URL=http://127.0.0.1:4141/v1`, `OPENAI_API_KEY=dummy`.
- **Gemini agent** — `gemini -p <prompt> -m gemini-3.1-pro-preview -y`
  using the host's preconfigured Gemini credentials (no proxy).

If you want to use real upstream APIs instead of the local proxy on `127.0.0.1:4141`,
override `MYTHOS_CLAUDE_BASE_URL` and `MYTHOS_CODEX_BASE_URL` (or set the
provider `env` blocks in `mythos/config.yaml`) and provide real
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` to the underlying CLIs.

## 2. Create the Discord bot

1. Go to <https://discord.com/developers/applications> and create a new
   application named **Mythos** (or whatever you like).
2. Under **Bot**, click **Add Bot**, copy the **Token** — you'll set
   `DISCORD_BOT_TOKEN` to this.
3. Under **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT
   INTENT**. Mythos relies on the message content intent to read user
   requests in the main channel.
4. Under **OAuth2 → URL Generator**, tick:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**: `Send Messages`, `Manage Channels`, `View
     Channels`, `Read Message History`, `Embed Links`, `Attach Files`,
     `Mention Everyone` (for `<@user>` pings)
5. Open the generated invite URL and add the bot to your server.

Permissions integer matching the above is `277025770576` (View Channels,
Send Messages, Manage Channels, Read Message History, Embed Links, Attach
Files, Mention Everyone).

The invite URL will look like:

```
https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=277025770576&scope=bot%20applications.commands
```

## 3. Get IDs from your server

Enable Developer Mode in Discord (User Settings → Advanced → Developer
Mode), then right-click each item to copy its ID:

| Variable | What to copy |
|----------|--------------|
| `DISCORD_GUILD_ID` | Your server (right-click the server icon → Copy Server ID) |
| `DISCORD_MAIN_CHANNEL_ID` | The text channel you want as the intake channel |
| `DISCORD_PROJECT_CATEGORY_ID` *(optional)* | A category to nest project channels under (right-click the category) |

## 4. Required environment variables

Copy `mythos/.env.example` to `.env` (or export them in your shell):

```
DISCORD_BOT_TOKEN=<paste from step 2>
DISCORD_GUILD_ID=<id from step 3>
DISCORD_MAIN_CHANNEL_ID=<id from step 3>
DISCORD_PROJECT_CATEGORY_ID=<optional>

# Where Mythos persists project state and workspaces (default ~/.mythos).
MYTHOS_STATE_DIR=~/.mythos/state
MYTHOS_WORKSPACE_ROOT=~/.mythos/workspaces

# Underlying CLI credentials — read by the CLIs themselves, not by Mythos.
# Required if you bypass the local 127.0.0.1:4141 proxy:
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=     # or GOOGLE_API_KEY — gemini CLI accepts either
```

## 5. Optional: write a `mythos/config.yaml`

The defaults baked into `mythos/config.py` already match the build spec.
Only create `mythos/config.yaml` if you need to override them. Start by
copying `mythos/config.yaml.example`:

```bash
cp mythos/config.yaml.example mythos/config.yaml
$EDITOR mythos/config.yaml
```

Recognised top-level keys:

- `discord.bot_token`, `discord.guild_id`, `discord.main_channel_id`,
  `discord.project_category_id`
- `state_dir`, `workspace_root` — paths (env still wins if set)
- `providers.<claude|codex|gemini>.command` — full argv as a list
- `providers.<…>.env` — extra environment variables for that CLI
- `providers.<…>.timeout_seconds` — per-call timeout (default 1800)

## 6. Install hermes-agent + mythos

From the repo root:

```bash
pip install -e '.[messaging]'   # discord.py is in the messaging extra
pip install -e '.[dev]'         # for the test suite
```

The `mythos.*` packages are picked up automatically — they're listed in
`pyproject.toml` under `[tool.setuptools.packages.find]`.

## 7. Run the bot

```bash
python -m mythos.bot --config mythos/config.yaml
# or, if you set everything via env:
python -m mythos.bot
```

You should see:

```
INFO mythos.discord_py: Mythos bot online as Mythos#1234
```

## 8. Sanity-check in Discord

In your main channel, type a project request. Within a few seconds you
should see Athena reply with a link to a newly created project channel.
See `HAPPY_PATH_CHECKLIST.md` for the full manual smoke test.

## 9. Run the automated tests

```bash
pytest tests/mythos/ -q
```

All tests use in-memory fakes — they neither touch Discord nor spawn the
real CLIs. They're safe to run anywhere.
