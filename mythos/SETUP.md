# Mythos Operator Runbook

Mythos is a Discord-based multi-agent development orchestrator that runs on top of
hermes-agent. A user posts a project request in a main Discord channel and the
system creates an isolated channel per project plus per-workstream channels for
frontend/backend/test specialists.

Agent roster:

| Role            | Name        | CLI          |
|-----------------|-------------|--------------|
| Main            | Athena      | Claude Code  |
| Draft Plan      | Prometheus  | Claude Code  |
| Review          | Argus       | Codex        |
| Test            | Hephaestus  | Codex        |
| Frontend        | Apollo      | Gemini       |
| Backend         | Atlas       | Claude Code  |

---

## 1. Discord bot setup

1. Open <https://discord.com/developers/applications> and click **New
   Application**. Name it `Mythos`.
2. In **Bot** → **Add Bot**. Copy the **Token** (you will set this as
   `DISCORD_BOT_TOKEN`).
3. Under **Privileged Gateway Intents** enable:
   - `MESSAGE CONTENT INTENT` — required to read user messages.
   - `SERVER MEMBERS INTENT` — optional, only if you want member lookups.
4. Under **OAuth2 → URL Generator**, select scopes:
   - `bot`
   - `applications.commands`
5. Bot permissions:
   - `View Channels`
   - `Send Messages`
   - `Send Messages in Threads`
   - `Manage Channels` (required to create per-project / per-workstream channels)
   - `Read Message History`
   - `Embed Links`
   - `Attach Files`
6. Copy the generated invite URL. Template:

   ```
   https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=395137057280&scope=bot%20applications.commands
   ```

   Paste it in your browser, pick the target server, **Authorize**.

7. In your server, create:
   - One **main channel** (e.g. `#mythos-main`) — users post project requests
     here.
   - One **category** named `mythos-projects` (default; configurable). The bot
     will create per-project channels under this category.

8. Right-click the main channel → **Copy Channel ID** (Developer Mode must be
   enabled in Discord Settings → Advanced). Save it as `MYTHOS_MAIN_CHANNEL_ID`.
9. Right-click the server icon → **Copy Server ID**. Save as `DISCORD_GUILD_ID`.

---

## 2. Required environment variables

| Variable                   | Required | Purpose                                                      |
|----------------------------|----------|--------------------------------------------------------------|
| `DISCORD_BOT_TOKEN`        | yes      | Bot token from step 1.2.                                     |
| `DISCORD_GUILD_ID`         | yes      | Numeric Discord server id (step 1.9).                        |
| `MYTHOS_MAIN_CHANNEL_ID`   | yes      | Numeric id of the main channel (step 1.8).                   |
| `MYTHOS_WORKSPACE_ROOT`    | no       | Where per-project working dirs live. Default `./workspaces`. |
| `MYTHOS_PROJECT_CATEGORY`  | no       | Discord category for project channels. Default `mythos-projects`. |
| `MYTHOS_CLI_TIMEOUT`       | no       | Per-CLI timeout in seconds. Default 600.                     |
| `MYTHOS_CONFIG`            | no       | Explicit path to a yaml config file.                         |
| `ANTHROPIC_API_KEY`        | no\*     | Used only if you point Claude/Claude Code at a real Anthropic endpoint. |
| `OPENAI_API_KEY`           | no\*     | Used only if you point Codex at a real OpenAI endpoint.      |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | no\* | Read by `gemini` itself; no proxying done by Mythos.   |

\* Mythos defaults route Claude and Codex through the local `litellm`-style
proxy at `http://127.0.0.1:4141` with dummy auth (matches the benchmark host
setup). Override under the `mythos.agents.<role>` section in `config.yaml` if
you want real cloud endpoints.

See `mythos/.env.example` for a template.

---

## 3. Optional `~/.hermes/config.yaml` keys

Mythos reads its configuration from the `mythos:` section of the existing
hermes-agent config, falling back to env vars otherwise.

```yaml
mythos:
  discord_bot_token: ${DISCORD_BOT_TOKEN}
  discord_guild_id: 123456789012345678
  main_channel_id: 234567890123456789
  project_category_name: mythos-projects
  workspace_root: /var/lib/mythos/workspaces
  cli_timeout_seconds: 600

  agents:
    athena:
      command: ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
      model: claude-opus-4.7-1m-internal
      base_url: http://127.0.0.1:4141
      auth_token: dummy
    prometheus:
      command: ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
      model: claude-opus-4.7-1m-internal
      base_url: http://127.0.0.1:4141
      auth_token: dummy
    atlas:
      command: ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
      model: claude-opus-4.7-1m-internal
      base_url: http://127.0.0.1:4141
      auth_token: dummy
    argus:
      command: ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"]
      model: gpt-5.5
      base_url: http://127.0.0.1:4141/v1
      auth_token: dummy
    hephaestus:
      command: ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"]
      model: gpt-5.5
      base_url: http://127.0.0.1:4141/v1
      auth_token: dummy
    apollo:
      command: ["gemini", "-m", "gemini-3.1-pro-preview", "-y"]
      model: gemini-3.1-pro-preview
```

Anything not listed inherits the defaults baked into `mythos/config.py`.

---

## 4. Required CLIs on PATH

Make sure the following are installed and on `PATH` for the user that runs
Mythos:

```bash
claude --version          # Claude Code CLI
codex --version           # Codex CLI
gemini --version          # Gemini CLI (already auth'd on this host)
```

For the benchmark host, these are pre-installed and the proxy at
`http://127.0.0.1:4141` is already running. Verify with:

```bash
curl -s http://127.0.0.1:4141/v1/models | head
```

---

## 5. Wiring it up to hermes-agent

Mythos lives next to the existing hermes-agent code as a top-level package
(`mythos/`). It does **not** modify the existing gateway; it reuses
`discord.py` (already in `pyproject.toml`) but provides its own bridge so it
can run independently of the hermes gateway.

You can run Mythos either standalone or under hermes:

```bash
# Standalone (recommended for testing):
DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... MYTHOS_MAIN_CHANNEL_ID=... \
  python -m mythos.run

# As an addition to hermes-agent (no extra wiring needed — hermes won't see
# Mythos channels because Mythos owns its own client):
python -m mythos.run &
hermes-agent ...
```

Logs go to stdout. Per-project event logs are appended as JSONL to
`${MYTHOS_WORKSPACE_ROOT}/mythos-events.jsonl`.

---

## 6. First smoke test

1. `python -m mythos.run` — wait for `Mythos online: 6 agents …`.
2. In the configured main channel, post:
   `Build a Chrome translator extension that triggers on double-click.`
3. Within ~30 s you should see:
   - Athena reply in main with a link to a new `proj-translator-extension` channel.
   - Prometheus draft message in that channel.
   - Argus review message follows.
   - Athena prompts for `approve`/`changes:`/`reject`.
4. Reply `approve` in the project channel.
5. Three new channels appear: `proj-translator-extension-frontend`,
   `…-backend`, `…-test`. Each posts a "received spec" then a "complete"
   message.

A full manual test plan lives in `mythos/HAPPY_PATH_CHECKLIST.md`.

---

## 7. Troubleshooting

| Symptom                                              | Fix                                                                      |
|------------------------------------------------------|--------------------------------------------------------------------------|
| Bot shows offline.                                   | Check `DISCORD_BOT_TOKEN` and `MESSAGE CONTENT INTENT` is enabled.       |
| `Failed to create project channel`.                  | Bot is missing `Manage Channels` perm in the target category.            |
| `CLI binary not found: claude`.                      | Install Claude Code CLI and ensure it's on PATH for the bot user.        |
| Specialist channel posts blocker about timeout.      | Bump `cli_timeout_seconds` in `config.yaml` or `MYTHOS_CLI_TIMEOUT`.     |
| Messages keep landing in main but no project channel.| `MYTHOS_MAIN_CHANNEL_ID` is wrong; the bot only acts on that exact id.   |
| Project state stuck in `awaiting_approval`.          | Reply `approve`, `changes: …`, or `reject` in the project channel.       |
