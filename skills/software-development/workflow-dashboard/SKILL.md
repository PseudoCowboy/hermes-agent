---
name: workflow-dashboard
description: Produce an operator-friendly dashboard for a workflow project by combining `workflow_status` with the coordination files and recent durable artifacts.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, dashboard, coordination, nanoclaw]
    related_skills: [workflow-status, workflow-review-stream, workflow-execute-stream]
---

# Workflow Dashboard

Use this skill when the user wants a richer coordination view than a raw status dump.

## Core behavior

- Start with `workflow_status`.
- Read `coordination/status-board.md`, `coordination/dependencies.md`, and `coordination/integration-points.md` when they exist.
- Present a concise dashboard that covers:
  - current plan state and approved-plan status
  - stream ownership, reviewers, and task-state summaries
  - blockers or dependency bottlenecks
  - missing or corrupt workflow artifacts that need repair

## Rules

- Repo files are the source of truth. Do not reconstruct progress from chat history.
- If the dashboard shows stale placeholders, say so instead of polishing them into fake status.
- Prefer a clear operational summary over long narrative prose.
