---
name: workflow-status
description: Report project workflow state from repo files only, using `workflow_status` as the machine-readable source of truth.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, status, dashboard, nanoclaw]
    related_skills: [workflow-dashboard, workflow-plan, workflow-decompose]
---

# Workflow Status

Use this skill when the user wants the current state of a workflow project without changing it.

## Core behavior

- Call `workflow_status`.
- Summarize only what the repo files say: plan states, approved-plan presence, workstream status, and coordination artifacts.
- If a manifest or task-state file is corrupt or missing, say that explicitly instead of reconstructing likely state from chat.

## Output style

- Keep the summary concise.
- Highlight blockers, missing artifacts, and ambiguous states first.
- When useful, point the user to the specific file paths that need repair.
