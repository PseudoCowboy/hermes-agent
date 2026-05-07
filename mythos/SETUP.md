# Mythos Operator Setup

Mythos is a Discord-driven multi-agent development orchestrator that lives
inside the hermes-agent repo. It coordinates **six** named agents:

| Role            | Codename     | CLI driver           |
|-----------------|--------------|----------------------|
| Main / Orchestrator | Athena   | Claude Code CLI      |
| Draft Plan      | Prometheus   | Claude Code CLI      |
| Review          | Argus        | Codex CLI            |
| Test            | Hephaestus   | Codex CLI            |
| Frontend        | Apollo       | Gemini CLI           |
| Backend         | Atlas        | Claude Code CLI      |

Users post a project request in a configured Discord intake channel. Mythos
creates a per-project category, runs the planning + review + approval
loop, then decomposes work into specialist channels.

## 1. Prerequisites

Install on the host where mythos will run:

* **Python 3.11+** (the parent hermes-agent repo's interpreter is fine —
  mythos only depends on hermes' existing `discord.py`).
* **Claude Code CLI** — installed and on `$PATH` as `claude`. Verify with
  `claude --version`.
* **Codex CLI** — installed as `codex`. Verify with `codex --version`.
* **Gemini CLI** — installed as `gemini` and pre-authenticated against
  Google (mythos does not pass auth env to it). Verify with
  `gemini --version` and run a hello world prompt.

For benchmark/test environments there is also typically a local
multi-provider proxy serving Anthropic-shaped requests on
`http://127.0.0.1:4141` and OpenAI-shaped requests on
`http://127.0.0.1:4141/v1`. Mythos defaults to those endpoints.

## 2. Create the Discord bot

1. Go to <https://discord.com/developers/applications>, click
   **New Application**, name it (e.g. `Mythos`).
2. Under **Bot**, click **Add Bot**, then copy the **Token** — that is
   `DISCORD_BOT_TOKEN`.
3. Under **Privileged Gateway Intents**, enable:
   * `Message Content Intent` (required — mythos parses message text)
   * `Server Members Intent` (recommended — for mention resolution)
4. Under **OAuth2 → URL Generator**:
   * Scopes: `bot`, `applications.commands`
   * Bot permissions:
     * View Channels
     * Send Messages
     * Manage Channels (to create per-project categories + channels)
     * Read Message History
     * Mention Everyone (optional)
5. Open the generated URL and invite the bot to your server.
6. In Discord, **enable Developer Mode** (Settings → Advanced) and
   right-click your server name → **Copy Server ID** — that is
   `DISCORD_GUILD_ID`.
7. Right-click the channel you want to use as the intake channel →
   **Copy Channel ID** — that is `MYTHOS_MAIN_CHANNEL_ID`.

## 3. Configure environment variables

Copy `mythos/.env.example` to `mythos/.env` and fill in:

```bash
cp mythos/.env.example mythos/.env
# …edit mythos/.env…
set -a; source mythos/.env; set +a
```

Required:

* `DISCORD_BOT_TOKEN`
* `DISCORD_GUILD_ID`
* `MYTHOS_MAIN_CHANNEL_ID`

Optional but useful:

* `MYTHOS_WORKSPACE_ROOT` (default `~/.mythos/workspaces`) — per-project
  workspaces live here, one directory per project id.
* `MYTHOS_STATE_PATH` (default `~/.mythos/state.json`) — JSON-backed
  project store.
* `MYTHOS_CLAUDE_BASE_URL`, `MYTHOS_CLAUDE_MODEL`,
  `MYTHOS_CODEX_BASE_URL` — override defaults if your proxy lives
  elsewhere or you want a different model.

The CLIs themselves expect their own credentials:

* Claude Code CLI: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_MODEL` (mythos sets these per-invocation from
  `MythosConfig.claude_cli.env`).
* Codex CLI: `OPENAI_BASE_URL`, `OPENAI_API_KEY` (mythos sets these too).
* Gemini CLI: uses its own preconfigured Google credentials. Run
  `gemini` once interactively to authenticate, or set
  `GEMINI_API_KEY` / `GOOGLE_API_KEY` in your shell environment if your
  Gemini CLI version supports key-based auth.

## 4. Optional: YAML overlay

If you want to override CLI flags without editing code, set
`MYTHOS_CONFIG_PATH` to a YAML file:

```yaml
workspace_root: /var/lib/mythos/workspaces
state_path: /var/lib/mythos/state.json
agent_timeout_seconds: 900
agents:
  claude:
    command:
      - claude
      - --dangerously-skip-permissions
      - --effort
      - high
      - --print
    env:
      ANTHROPIC_MODEL: claude-opus-4.7-1m-internal
  codex:
    command:
      - codex
      - exec
      - -m
      - gpt-5.5
      - -c
      - model_reasoning_effort=high
      - --skip-git-repo-check
  gemini:
    command:
      - gemini
      - -m
      - gemini-3.1-pro-preview
      - -y
```

## 5. Launch

```bash
cd /path/to/hermes-agent
python -m mythos
```

You should see something like:

```
INFO mythos: Mythos starting; main channel = …, guild = …
INFO mythos.discord_bot: mythos discord client ready as Mythos#1234
```

## 6. Try the happy path

Post a message in your intake channel:

> I want to build a Chrome extension, a translator extension, when I
> double click on a word it should look it up in a built-in dictionary
> and show the translation.

Mythos will:

1. Create a category `project-<short-id>-<slug>` and a `plan` channel.
2. Run **Athena** to acknowledge.
3. Run **Prometheus** to ask clarifying questions.
4. Wait for your reply in the project channel.
5. Generate a draft spec, hand to **Argus** for review.
6. Present the spec + review and ask you to reply `approve` or
   `revise: <feedback>`.
7. On approval, create `frontend`, `backend`, `test` channels and run
   **Apollo**, **Atlas**, **Hephaestus** in their own channels.

See `mythos/HAPPY_PATH_CHECKLIST.md` for a manual test script.

## 7. Hermes integration notes

Mythos reuses the `discord.py` dependency that hermes-agent already
declares. It does **not** plug into hermes' platform registry or
`delegate_task`, because:

* hermes' delegation is in-process / thread-based and uses `AIAgent`
  Python objects, but the spec mandates real CLI subprocesses with a
  specific argv + environment.
* Running mythos as a separate `python -m mythos` process keeps the
  Discord intent set, command surface, and channel creation logic
  isolated from hermes' core gateway and lets hermes-driven Discord
  flows run unaffected.

If you need to run both the hermes Discord gateway and mythos against
the same bot, give them **different** bot tokens (one per Discord
application), or scope mythos to a separate intake channel and let
hermes use the rest.
