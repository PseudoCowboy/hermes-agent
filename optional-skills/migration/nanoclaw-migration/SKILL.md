---
name: nanoclaw-migration
description: Migrate durable NanoClaw workflow artifacts into Hermes Agent. Imports repo-first workflow docs and role instructions, transforms group CLAUDE.md files into reference skills, and archives NanoClaw-specific runtime metadata for manual review.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [Migration, NanoClaw, Hermes, Workflow, Multi-Agent]
    related_skills: [workflow-create-project, workflow-plan, workflow-review-stream]
---

# NanoClaw -> Hermes Migration

Use this skill when a user wants to preserve the useful multi-agent workflow knowledge from a NanoClaw repo without rebuilding NanoClaw's Discord/runtime stack inside Hermes.

## What this skill does

It uses `scripts/nanoclaw_to_hermes.py` to:

- import top-level `AGENTS.md` and `CLAUDE.md` into Hermes as workspace references
- import the durable workflow docs and specs that map cleanly to Hermes repo-first workflows
- import planning/decision/review docs as a reusable reference library
- convert `groups/*/CLAUDE.md` role instructions into Hermes reference skills under `~/.hermes/skills/nanoclaw-imports/`
- archive NanoClaw-only runtime/container metadata and Discord command docs for manual review instead of pretending Hermes natively supports them
- emit a structured JSON/markdown report showing what was migrated, archived, skipped, or conflicted

## Migration modes

Prefer these two presets:

- `portable`
  Imports only the workflow knowledge that maps directly into Hermes.
- `full`
  Performs the same imports and also archives runtime-specific NanoClaw metadata for manual review.

## Path resolution

The helper script lives at:

- `scripts/nanoclaw_to_hermes.py`

When the skill is installed from the Skills Hub, the normal installed path is:

- `~/.hermes/skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py`

Before running the helper:

1. Prefer the installed path under `~/.hermes/skills/migration/nanoclaw-migration/`.
2. If that path fails, resolve the script relative to the installed `SKILL.md`.
3. Only use `find` as a fallback if the installed location is missing or the skill was moved manually.

## Default workflow

1. Run a dry run first.
2. Summarize what would be imported directly versus archived.
3. Use the `portable` preset unless the user explicitly wants the runtime/archive bundle too.
4. If destination conflicts exist, ask whether to re-run with `--overwrite`.
5. After execution, treat the script's JSON report as the source of truth.

## Commands

Dry run with the portable preset:

```bash
python3 ~/.hermes/skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py --preset portable
```

Dry run with the full preset against a custom source repo:

```bash
python3 ~/.hermes/skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py --preset full --source /path/to/nanoclaw
```

Execute the portable migration:

```bash
python3 ~/.hermes/skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py --execute --preset portable
```

Execute the full migration and overwrite conflicts:

```bash
python3 ~/.hermes/skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py --execute --preset full --overwrite
```

## Reporting rules

After execution:

- base all counts on `report.summary`
- list imported items only when the item status is exactly `migrated`
- list archived items only when the item status is exactly `archived`
- call out `conflict`, `skipped`, and `error` items explicitly
- include the `output_dir` when present so the user can inspect `report.json`, `summary.md`, `archive/`, and `backups/`
