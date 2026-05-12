# Mythos — operator setup runbook

Mythos is a Discord-based multi-agent development orchestration system that
runs on top of `hermes-agent`. This runbook lists the **exact configuration
the operator must supply before testing in real Discord**.

## 1. System dependencies

The following CLIs must be on `$PATH` and authenticated:

| Agent | CLI | Install + auth |
|---|---|---|
| Athena, Prometheus, Atlas | `claude` (Claude Code) | `npm i -g @anthropic-ai/claude-code` (or per Claude Code install docs); the bot reaches Anthropic via the proxy at `http://127.0.0.1:4141` (configurable) |
| Argus, Hephaestus | `codex` | `npm i -g @openai/codex`; reaches OpenAI via `http://127.0.0.1:4141/v1` |
| Apollo | `gemini` | `npm i -g @google/gemini-cli`; uses gemini's own preconfigured credentials on this host |

Verify all three are installed:

```bash
which claude codex gemini
claude --version && codex --version && gemini --version
```

## 2. Create the Discord bot

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Name it "Mythos" (or whatever you like).
3. **Bot tab → Reset Token** → copy the token. This is your `DISCORD_BOT_TOKEN`.
4. **Bot tab → Privileged Gateway Intents** → enable:
   - **MESSAGE CONTENT INTENT** (required: we read free-form project requests)
   - **SERVER MEMBERS INTENT** (used for owner-id checks)
5. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions:
     - View Channels
     - Send Messages
     - Manage Channels (required: Athena creates project + discipline channels)
     - Manage Threads
     - Read Message History
     - Add Reactions
     - Mention Everyone (only used to ping the project owner)
   - Copy the generated URL and paste it in your browser to invite the bot to
     your guild.
6. In your Discord server, **right-click your guild → Copy Server ID**
   (requires Developer Mode in User Settings → Advanced). This is `DISCORD_GUILD_ID`.
7. Create (or pick) a text channel to be the **main intake channel**. Right-click
   it → Copy Channel ID. This is `MYTHOS_MAIN_CHANNEL_ID`.

### Optional role bot identities

Create additional Discord applications only if you want each Mythos role to
appear as its own bot in the channels. Invite each role bot with **View
Channels**, **Send Messages**, and **Read Message History**. Do not run Mythos
with those tokens as separate processes; Mythos uses them as send-only clients.

Supported token variables:

- `DISCORD_ATHENA_BOT_TOKEN`
- `DISCORD_PROMETHEUS_BOT_TOKEN`
- `DISCORD_ARGUS_BOT_TOKEN`
- `DISCORD_HEPHAESTUS_BOT_TOKEN`
- `DISCORD_APOLLO_BOT_TOKEN`
- `DISCORD_ATLAS_BOT_TOKEN`

## 3. Required environment variables

Copy `mythos/.env.example` to `.env` (or export directly in your shell):

```bash
# --- Discord (required) ---
export DISCORD_BOT_TOKEN="..."          # from step 2.3
export DISCORD_GUILD_ID="123456789"     # from step 2.6
export MYTHOS_MAIN_CHANNEL_ID="987654"  # from step 2.7

# Optional role-bot tokens. Leave unset to fall back to the primary bot.
# export DISCORD_PROMETHEUS_BOT_TOKEN="..."
# export DISCORD_ARGUS_BOT_TOKEN="..."
# export DISCORD_HEPHAESTUS_BOT_TOKEN="..."
# export DISCORD_APOLLO_BOT_TOKEN="..."
# export DISCORD_ATLAS_BOT_TOKEN="..."

# --- Per-agent endpoint overrides (optional) ---
# Defaults match the benchmark spec exactly:
#   claude  -> ANTHROPIC_BASE_URL=http://127.0.0.1:4141, ANTHROPIC_AUTH_TOKEN=dummy
#   codex   -> OPENAI_BASE_URL=http://127.0.0.1:4141/v1, OPENAI_API_KEY=dummy
#   gemini  -> uses gemini-cli's own credentials, no extra env
# Codex defaults include --dangerously-bypass-approvals-and-sandbox because
# the benchmark VM sandbox cannot create its network namespace.
# Override only if you point at a different LLM gateway:
# export MYTHOS_CLAUDE_BASE_URL="http://127.0.0.1:4141"
# export MYTHOS_CLAUDE_MODEL="claude-opus-4.7-1m-internal"
# export MYTHOS_CODEX_MODEL="gpt-5.5"
# export MYTHOS_GEMINI_MODEL="gemini-3.1-pro-preview"

# --- State directory (optional) ---
export MYTHOS_STATE_DIR="./mythos_state"

# --- Iteration cap (optional) ---
export MYTHOS_MAX_ITERATIONS="2"
```

If you are using **real** Anthropic / OpenAI / Google APIs (no proxy), set the
authentication env vars the underlying CLIs expect (e.g. `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`) and override
`MYTHOS_CLAUDE_BASE_URL`/`MYTHOS_CODEX_BASE_URL` to point at the upstream.

## 4. Optional `mythos/config.yaml`

```yaml
state_dir: ./mythos_state
max_review_iterations: 2

discord_role_bots:
  prometheus: "..."
  argus: "..."
  hephaestus: "..."
  apollo: "..."
  atlas: "..."

agents:
  athena:
    timeout_seconds: 600
  apollo:
    # Override Apollo's command if you've installed gemini somewhere unusual:
    command: ["/opt/google/gemini-cli/bin/gemini", "-m", "gemini-3.1-pro-preview", "-y"]
    timeout_seconds: 900
```

Pass it in with `python -m mythos.run --config mythos/config.yaml`.

## 5. Wiring it up

This package lives inside the `hermes-agent` repo but **runs as its own
process** — it does not need `hermes` to be running. Concretely:

```bash
# from the hermes-agent repo root
pip install -e .              # installs hermes deps including discord.py
pip install pyyaml pytest pytest-asyncio   # if not already present
python -m mythos.run --config mythos/config.yaml
```

You can also run it under hermes-agent's existing systemd / docker setup —
the entrypoint `python -m mythos.run` is self-contained.

## 6. Testing

Once the process is running and the bot is online:

1. Post a project idea in the main channel.
2. Watch Athena reply, create the project channel, and ping Prometheus.
3. Follow the manual checklist in `mythos/HAPPY_PATH_CHECKLIST.md`.

For automated tests (no Discord, no LLMs):

```bash
pytest tests/mythos/ -q
```
