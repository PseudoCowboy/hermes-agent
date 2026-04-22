---
name: workflow-create-project
description: Create a NanoClaw-style repo-first project workspace under `groups/shared_project/active/<slug>/` and prepare the draft control files for planning.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, project, nanoclaw, scaffolding, multi-agent]
    related_skills: [workflow-plan, workflow-status, workflow-decompose]
---

# Workflow Create Project

Use this skill when the user wants to start a new repo-first workflow project.

## Core behavior

- Call `workflow_create_project` first.
- Treat the tool result as the source of truth for the project slug and created path.
- If the user supplied enough intent, update `control/draft-plan.md` so it reflects the real scope instead of leaving only the scaffold placeholder.
- Never claim the project exists until `workflow_create_project` succeeds.

## Required outcome

The project should exist at:

- `groups/shared_project/active/<project-slug>/`

Minimum expected structure:

- `control/`
- `coordination/`
- `plans/`
- `workstreams/`
- `archive/`

## Interaction style

- Be explicit about the created slug and path.
- If the project already exists, preserve existing files and explain what was reused versus newly scaffolded.
- End with the next step: run `workflow-plan` to save the initial plan artifacts.
