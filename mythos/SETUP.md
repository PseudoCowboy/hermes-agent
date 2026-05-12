# Mythos Operator Setup Guide

This document walks an operator through everything required to take a
freshly-cloned hermes-agent repo to a running Mythos installation that
responds to messages in a real Discord server.

> **What is Mythos?** A multi-agent development orchestrator that turns
> a Discord channel into the user-facing surface for a roster of
> mythologically-named agents. See `mythos/__init__.py` for a one-paragraph
> overview, and `_bmad-output/planning-artifacts/architecture.md` (in the
> spec source) for the full design rationale.

## 1. Discord prerequisites

### 1a. Create a Discord application + bot

1. Go to https://discord.com/developers/applications
2. **New Application** → name it (e.g. *Mythos Dev*).
3. **Bot** sidebar → **Add Bot**.
4. Copy the **Bot Token** — this becomes `DISCORD_BOT_TOKEN`.
5. Under **Privileged Gateway Intents**, enable:
   - ✅ Message Content Intent
   - ✅ Server Members Intent (optional; only needed if you allowlist by username)
6. Save changes.

### 1b. Required bot permissions

Mythos needs to **create channels and categories on the fly** in the target
guild, plus standard send-message permissions.

OAuth2 → URL Generator → **Scopes:**
- ✅ `bot`
- ✅ `applications.commands` (optional; reserved for future slash commands)

**Bot Permissions:**
- ✅ View Channels
- ✅ Send Messages
- ✅ Read Message History
- ✅ Manage Channels  ← required for category/channel creation
- ✅ Mention @everyone, @here, and All Roles  *(safe with the default
  `discord.AllowedMentions(everyone=False, roles=False, users=True)` cap
  that the adapter applies — used for user pings only)*

Copy the generated invite URL, paste it into your browser, and authorize the
bot into a Discord server you control.

### 1c. Identify the guild + main channel

1. In Discord settings, enable **Developer Mode** (Advanced → Developer Mode).
2. Right-click your server name → **Copy Server ID** → `DISCORD_GUILD_ID`.
3. Create (or pick) a text channel where users will type their initial ideas
   — typically `#main`. Right-click it → **Copy Channel ID** →
   `MYTHOS_MAIN_CHANNEL_ID`.

> The bot only watches `MYTHOS_MAIN_CHANNEL_ID` for new project requests.
> All other channels it talks in are ones it created itself for active
> projects.

### 1d. Multi-bot mode (one Discord identity per role) — recommended

By default Mythos can run with a single bot that posts on behalf of
every role (Hermes, Prometheus, Argus, Hephaestus, Apollo, Atlas) and
distinguishes speakers via a `[Role · Title]` text prefix. For better
UX in busy channels you can give each role its own Discord bot account
so each speaker has its own username and avatar.

1. Repeat steps 1a–1b for each role you want to split out, naming the
   apps after the role (e.g. *Mythos · Prometheus*). Invite each bot
   into the same guild.
2. **Hermes's bot must have `Manage Channels`** — it is the only client
   that creates project categories/channels and the only client that
   listens for inbound messages. The other bots only need `View
   Channels` + `Send Messages` + `Read Message History`.
3. Set `MYTHOS_BOT_TOKENS` to a JSON dict of `{role: token}`:

   ```bash
   export MYTHOS_BOT_TOKENS='{
     "hermes":     "MTQ...",
     "prometheus": "MTQ...",
     "argus":      "MTQ...",
     "hephaestus": "MTQ...",
     "apollo":     "MTQ...",
     "atlas":      "MTQ..."
   }'
   ```

   - Roles missing from the dict fall back to Hermes's bot.
   - If `"hermes"` is omitted but the legacy `DISCORD_BOT_TOKEN` is set,
     Hermes uses the legacy token.
   - When `MYTHOS_BOT_TOKENS` is unset, Mythos runs in single-bot mode
     (legacy behavior — `DISCORD_BOT_TOKEN` only).

In multi-bot mode the `[Role · Title]` text prefix is dropped from
outbound messages because the Discord username/avatar already conveys
the speaker.

## 2. Required environment variables

Copy `mythos/.env.example` → `.env` and fill in real values.

| Variable                | Purpose                                                       | Example                  |
|-------------------------|---------------------------------------------------------------|--------------------------|
| `DISCORD_BOT_TOKEN`     | Bot token from the Discord Developer Portal. Required only in single-bot mode (see §1d). | `MTIz…` |
| `DISCORD_GUILD_ID`      | Numeric Discord guild ID where Mythos operates.               | `1029384756`             |
| `MYTHOS_MAIN_CHANNEL_ID`| Numeric ID of the channel users post intake messages in.      | `1029384800`             |
| `ANTHROPIC_API_KEY`     | API key used by the Claude Code CLI underneath Hermes/Prometheus/Atlas. *(Only required if your local proxy at 127.0.0.1:4141 demands it; the default config sends `dummy`.)* | `sk-ant-…` |
| `OPENAI_API_KEY`        | API key used by the Codex CLI underneath Argus/Hephaestus.    | `sk-…`                   |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | API key the Gemini CLI uses for Apollo. The Gemini CLI also accepts pre-existing `~/.config/gcloud` credentials. | `AIza…` |

### Optional model / endpoint overrides

| Variable                  | Default                                  | Notes                            |
|---------------------------|------------------------------------------|----------------------------------|
| `MYTHOS_CLAUDE_BASE_URL`  | `http://127.0.0.1:4141`                  | `ANTHROPIC_BASE_URL` for `claude` CLI. |
| `MYTHOS_CLAUDE_AUTH_TOKEN`| `dummy`                                  | `ANTHROPIC_AUTH_TOKEN` for `claude` CLI. |
| `MYTHOS_CLAUDE_MODEL`     | `claude-opus-4.7-1m-internal`            | Sets `ANTHROPIC_MODEL`.          |
| `MYTHOS_CODEX_BASE_URL`   | `http://127.0.0.1:4141/v1`               | `OPENAI_BASE_URL` for `codex`.   |
| `MYTHOS_CODEX_API_KEY`    | `dummy`                                  | `OPENAI_API_KEY` for `codex`.    |
| `MYTHOS_CODEX_MODEL`      | `gpt-5.5`                                | Replaces `-m gpt-5.5` in command. |
| `MYTHOS_GEMINI_MODEL`     | `gemini-3.1-pro-preview`                 | Replaces `-m …` in command.      |
| `MYTHOS_WORKSPACE_ROOT`   | `~/mythos/projects`                      | Where per-project working trees live. |
| `MYTHOS_LOG_LEVEL`        | `INFO`                                   | Standard Python log level.       |

## 3. CLI prerequisites

Mythos drives three coding CLIs as subprocesses. Install each on the host
running `python -m mythos`:

| Role(s)               | CLI       | Install                                          | Verify                              |
|-----------------------|-----------|--------------------------------------------------|-------------------------------------|
| Hermes, Prometheus, Atlas | `claude`  | https://docs.anthropic.com/en/docs/claude-code | `claude --version`                  |
| Argus, Hephaestus     | `codex`   | OpenAI Codex CLI install instructions          | `codex --version`                   |
| Apollo                | `gemini`  | Google Gemini CLI install instructions         | `gemini --version`                  |

### Default invocation contracts (in code: `mythos/roles.py`)

- **Claude Code:**
  `claude --dangerously-skip-permissions --effort high --print`
  with `ANTHROPIC_BASE_URL=http://127.0.0.1:4141`,
  `ANTHROPIC_AUTH_TOKEN=dummy`,
  `ANTHROPIC_MODEL=claude-opus-4.7-1m-internal`. Prompt is piped on stdin.
- **Codex:**
  `codex exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check`
  with `OPENAI_BASE_URL=http://127.0.0.1:4141/v1`,
  `OPENAI_API_KEY=dummy`. Prompt is piped on stdin.
- **Gemini:**
  `gemini -m gemini-3.1-pro-preview -y -p <prompt>`. Uses the host's
  pre-configured Gemini credentials — no proxy, no extra env. Prompt is the
  last positional argument.

If you run a local proxy (e.g. for benchmark capture) at
`http://127.0.0.1:4141`, the defaults work as-is. If not, set the
`MYTHOS_CLAUDE_BASE_URL` / `MYTHOS_CODEX_BASE_URL` env vars to point at the
real upstream endpoints (e.g. `https://api.anthropic.com`, `https://api.openai.com/v1`)
and supply real keys via `MYTHOS_CLAUDE_AUTH_TOKEN` / `MYTHOS_CODEX_API_KEY`.

## 4. Optional `mythos/config.yaml` keys

The shipped `mythos/config.yaml` contains every tunable. Common changes:

```yaml
workspace_root: "/srv/mythos/projects"   # Production-grade location
max_concurrent_specialists: 3            # Cap parallel CLI subprocess load
max_approval_rounds: 3                   # More patience on spec iteration
cli_timeout_seconds: 1200                # Long-running test agents
category_prefix: "mythos"                # Discord category name prefix
```

Any value here is overridden by an environment variable of the same name
(uppercased and prefixed with `MYTHOS_`).

## 5. Running Mythos

Mythos can run two ways.

### A. Standalone (recommended for first-time setup)

```bash
cd /path/to/hermes-agent
pip install -e ".[messaging]"            # brings in discord.py
pip install pyyaml                       # if not already present
export $(cat mythos/.env | xargs)        # or use direnv / systemd
python -m mythos
```

You should see:

```
INFO mythos.discord: Connected as Mythos#1234
```

The bot is now listening in your designated `MYTHOS_MAIN_CHANNEL_ID`.

### B. Bridged into a running hermes-agent process

If you already operate hermes-agent and want Mythos to share its Discord
session, use the bridge in `mythos/hermes_bridge.py`:

```python
# In your gateway wiring code:
from gateway.platforms.discord import DiscordAdapter
from mythos.config import load_config
from mythos.hermes_bridge import HermesDiscordIO
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import ProjectManager
from mythos.supervisor import Supervisor

discord_adapter: DiscordAdapter = ...    # the existing hermes adapter
cfg = load_config()
io = HermesDiscordIO(discord_adapter)
pm = ProjectManager(cfg, io)
sup = Supervisor(cfg)
orch = MythosOrchestrator(cfg, io, pm, sup)
orch.install_handlers()

# Then in hermes' message loop, route Mythos-channel events to:
await io.feed_event(message_event)
```

The bridge reuses hermes' Discord client (no second login) and is the
recommended path if you already run hermes daemon-style.

## 6. Sanity-checking the install

Run the integration test suite (no Discord, no CLIs needed):

```bash
pytest tests/mythos/ -q
```

Expected: **34 passed** in well under 5 seconds. If a test fails, the env
is broken — fix that before going live.

For a real-world Discord smoke test, follow `mythos/HAPPY_PATH_CHECKLIST.md`.

## 7. Where things live on disk

Per-project workspace layout under `MYTHOS_WORKSPACE_ROOT`:

```
~/mythos/projects/
├── index.json                         # project slug → category id, paths, phase
└── proj-translator-extension-20260507-a3f5d2/
    ├── frontend/                      # Apollo's working dir
    ├── backend/                       # Atlas's working dir
    ├── tests/                         # Hephaestus's working dir
    ├── spec/
    │   ├── spec-v1.md                 # Prometheus drafts
    │   └── spec-v2.md                 # subsequent revisions
    ├── logs/
    │   ├── hermes.log
    │   ├── prometheus.log
    │   └── …
    └── state.json                     # Recovery-hint snapshot
```

## 8. Troubleshooting

- **Bot online but silent in #main**: confirm `MYTHOS_MAIN_CHANNEL_ID`
  matches the channel you're typing in (right-click → Copy Channel ID).
- **`CLI binary not found for prometheus`**: `claude` is not on `$PATH`
  for the user running mythos. Verify with `which claude`.
- **`CLI timed out`**: bump `cli_timeout_seconds` in `config.yaml` or
  via env. Check the log directory under the project workspace for the
  full transcript.
- **Channels not being created**: bot is missing the **Manage Channels**
  permission in your guild. Re-invite using the URL generator with the
  correct permission scope.

## 9. What this is *not*

Mythos v1 is single-host, single-Discord-server, single-tenant. Out of
scope per the architecture doc:

- Multi-host / hosted SaaS deployment
- Web dashboard
- Cross-project agent collaboration
- Per-project role binding overrides
- Voice / video agent surfaces
- Auto-merge / CI / deploy automation
