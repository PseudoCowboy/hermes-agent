# Mythos Setup Runbook

Mythos is a Discord-native multi-agent development orchestration system
that lives inside the `hermes-agent` repository. A single user prompt in
a designated Discord channel kicks off a per-project channel category in
which six specialised AI agents (each backed by a different CLI)
collaborate to plan, review, and implement software.

This document is the **operator runbook**: every action a fresh operator
must take to bring up a working Mythos deployment, end to end.

## 1. Prerequisites

**Host machine**

* Python 3.10+
* Network access to the Discord gateway (`gateway.discord.gg:443`).
* The three CLI tools below must be installed and on `$PATH` for the
  user that runs Mythos:
  * `claude` (Claude Code CLI)
  * `codex` (OpenAI Codex CLI)
  * `gemini` (Google Gemini CLI)
* For local model proxying, an OpenAI/Anthropic-compatible proxy
  listening on `http://127.0.0.1:4141` (the brief defaults). If you
  point the runners at the official APIs instead, override the env vars
  listed below.

**Python packages**

```bash
pip install discord.py pyyaml
# optional, only needed when wiring through hermes-agent's own gateway:
pip install -e .
```

The integration tests do **not** require `discord.py` — they use an
in-memory fake.

## 2. Create a Discord application + bot

1. Go to <https://discord.com/developers/applications> and click
   **New Application**. Name it `Mythos` (or whatever you like).
2. **Bot tab → Add Bot**. Reset the token and copy it; this becomes
   `DISCORD_BOT_TOKEN`.
3. Under **Privileged Gateway Intents**, enable:
   * `MESSAGE CONTENT INTENT` — so the bot can read user messages.
   * `SERVER MEMBERS INTENT` — so it can see the user list (used for
     `@user` mention resolution).
4. **OAuth2 → URL Generator**:
   * Scopes: `bot`, `applications.commands`.
   * Bot Permissions: `Manage Channels`, `Manage Webhooks`,
     `Read Messages/View Channels`, `Send Messages`,
     `Send Messages in Threads`, `Embed Links`, `Attach Files`,
     `Add Reactions`, `Read Message History`, `Use Slash Commands`,
     `Mention Everyone` (off by default; off is fine if you don't
     mention everyone).
5. Copy the generated URL and open it as the server admin to invite the
   bot to your guild. The shape is:
   ```
   https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=536996928&scope=bot%20applications.commands
   ```
6. In the Discord client, right-click your server → **Copy Server ID**
   (you may need to enable Developer Mode in user settings). This
   becomes `DISCORD_GUILD_ID`.
7. Create or pick a channel that will be the **main** intake channel
   (e.g. `#mythos-main`). Right-click → **Copy Channel ID**. This
   becomes `MYTHOS_MAIN_CHANNEL_ID`.

## 3. Required environment variables

Copy `mythos/.env.example` to `mythos/.env` and fill in:

| Variable | Required? | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Bot token from step 2.2. |
| `DISCORD_GUILD_ID` | yes | Server ID from step 2.6. |
| `MYTHOS_MAIN_CHANNEL_ID` | yes | Channel ID from step 2.7. |
| `ANTHROPIC_BASE_URL` | no | Defaults to `http://127.0.0.1:4141`. Set to `https://api.anthropic.com` if hitting the real API. |
| `ANTHROPIC_AUTH_TOKEN` | no | Sent as Anthropic API token by Claude Code CLI. Defaults to `dummy` for proxy use. Set to your real key if hitting the real API. |
| `ANTHROPIC_API_KEY` | no | Alias for `ANTHROPIC_AUTH_TOKEN`; honoured if `ANTHROPIC_AUTH_TOKEN` is unset. |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-opus-4.7-1m-internal`. |
| `OPENAI_BASE_URL` | no | Defaults to `http://127.0.0.1:4141/v1`. Set to `https://api.openai.com/v1` for real API. |
| `OPENAI_API_KEY` | no | Codex CLI key. Defaults to `dummy` for proxy use; set real key if hitting OpenAI directly. |
| `GEMINI_API_KEY` *or* `GOOGLE_API_KEY` | no | Only if your Gemini CLI is not already authenticated through `gemini auth login`. The brief assumes the host already has Gemini credentials set up. |
| `MYTHOS_WORKSPACE_ROOT` | no | Defaults to `./workspaces`. Per-project working directories live under here. |
| `MYTHOS_STATE_PATH` | no | Defaults to `./mythos-state.json`. JSON-backed project metadata store. |
| `MYTHOS_TIMEOUT_SECONDS` | no | Per-CLI-call timeout in seconds. Defaults to `600`. |
| `MYTHOS_MAX_PLANNING_ROUNDS` | no | Max design-revision rounds before forcing the user to decide. Defaults to `2`. |
| `MYTHOS_MAX_HANDOFF_STREAK` | no | Max consecutive agent-to-agent pings without a human reply before pausing. Defaults to `6`. |
| `MAIN_RUNNER` / `DRAFT_PLAN_RUNNER` / `REVIEW_RUNNER` / `TEST_RUNNER` / `FRONTEND_RUNNER` / `BACKEND_RUNNER` | no | Override the role-to-runner binding (must reference one of `claude_code`, `codex`, `gemini`). Useful for swapping a backing CLI without code changes. |

## 4. Optional `config.yaml` keys

If you prefer YAML over env vars, drop a section into `config.yaml`:

```yaml
mythos:
  discord_guild_id: "1234567890"
  main_channel_id: "1234567890"
  workspace_root: ./workspaces
  state_db_path: ./mythos-state.json
  timeout_seconds: 600
  max_planning_rounds: 2
  max_handoff_streak: 6
  agents:
    apollo:
      avatar_url: "https://example.com/apollo.png"
  runners:
    gemini:
      command: ["gemini", "-m", "gemini-3.1-pro-preview", "-y"]
```

Pass it to the entrypoint with `--config /path/to/config.yaml`.

## 5. Running

Two deployment modes are supported.

### 5a. Standalone (recommended for first bring-up)

```bash
cd hermes-agent
export $(grep -v '^#' mythos/.env | xargs)
python -m mythos.run --log-level INFO
```

The standalone entrypoint owns its own discord.py client and is the
fastest path to a working bot.

### 5b. Mounted inside hermes-agent's gateway

If you already run a hermes-agent gateway and want Mythos to share the
Discord connection:

```python
from gateway.platforms.discord import DiscordAdapter as HermesDiscord
from mythos.hermes_bridge import HermesDiscordBridge
from mythos.orchestrator import Orchestrator
from mythos.config import load_config
from mythos.projects import ProjectStore
from mythos.agents.registry import AgentRegistry
from mythos.runners.subprocess_runner import build_runner_from_spec

cfg = load_config()
runners = {n: build_runner_from_spec(s) for n, s in cfg.runners.items()}
store = ProjectStore(cfg.state_db_path, cfg.workspace_root)
registry = AgentRegistry(cfg, runners)

hermes_adapter = HermesDiscord(...)        # configured per hermes' own docs
bridge = HermesDiscordBridge(hermes_adapter)
orchestrator = Orchestrator(config=cfg, store=store, registry=registry, discord=bridge)
bridge.add_listener(orchestrator.handle_message)

# Then arrange for hermes' Discord adapter to call `bridge.deliver(event)`
# from its inbound message-handling pipeline.
```

`HermesDiscordBridge` implements the same `DiscordAdapter` Protocol as
the standalone client, so the orchestrator's behaviour is identical.

## 6. Verifying the install

1. Start the bot (`python -m mythos.run`).
2. Watch the log for `Mythos discord adapter ready as Mythos#1234`.
3. In your guild's main channel, post:
   `I want to build a Chrome extension translator: when I double-click on a website it should look up a built-in dictionary and show the translation.`
4. Within 5 seconds, you should see:
   * an acknowledgement reply from Athena in the main channel,
   * a new category `proj-i-want-to-build-a-chrome-extension-translator…`,
   * a `#planning` channel inside it with an intake summary and a
     ping to Prometheus,
   * Prometheus posting either clarifying questions ending with `@user`
     OR a draft summary ending with `@Argus`.
5. From there, the **HAPPY_PATH_CHECKLIST.md** walk-through has the
   step-by-step user actions to drive the project to completion.

## 7. Troubleshooting

* **No reply from the bot in main channel**: confirm
  `MESSAGE CONTENT INTENT` is enabled in the developer portal AND
  re-invite the bot if you toggled it after invite.
* **Channel category not created**: the bot needs `Manage Channels`
  permission in the guild. Recheck the OAuth invite URL.
* **`runner X command not found`**: the relevant CLI is not on `$PATH`
  for the user that started Mythos. Run `which claude`, `which codex`,
  `which gemini` as that user.
* **CLI subprocess times out repeatedly**: raise
  `MYTHOS_TIMEOUT_SECONDS` (defaults to 10 minutes; large refactors can
  exceed that).
* **`discord.py` import fails**: `pip install discord.py>=2.3`. The
  test suite does not need it; only the standalone runtime does.

## 8. Operating tips

* Each project's working directory is at `./workspaces/<project_id>/`.
  Code produced by the implementation agents lives there.
* `./mythos-state.json` is the metadata store. It is safe to back up; on
  bot restart the orchestrator rebuilds the channel index from it.
* To archive a finished project, call
  `Orchestrator.archive_project(project_id)`. The bot will stop routing
  messages from that project's channels (you can leave the channels in
  place for history).
