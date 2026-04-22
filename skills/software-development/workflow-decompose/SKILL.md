---
name: workflow-decompose
description: Turn an approved plan into durable workstream contracts, then seed bounded task lists and task-state files for each stream.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, decomposition, workstreams, nanoclaw]
    related_skills: [workflow-approve-plan, workflow-execute-stream, workflow-status]
---

# Workflow Decompose

Use this skill after a human-approved plan exists and the project is ready to break into workstreams.

## Core behavior

- Read `control/approved-plan.md` and the most relevant plan artifacts before proposing streams.
- Build honest stream contracts: `name`, `owner`, `completionMode`, `reviewer` for code streams, `dependencies`, `acceptanceCriteria`, and optional `scope`.
- Call `workflow_decompose` with the stream list.
- After decomposition succeeds, replace placeholder `scope.md` and `tasks.md` content with concrete stream-specific content when needed.
- When you create checkbox tasks in `workstreams/<stream>/tasks.md`, call `workflow_sync_tasks` so `task-state.json` matches the human-readable task list.

## Invariants

- Do not decompose without an approved plan.
- Do not omit reviewers for code streams.
- Do not invent vague streams like "misc" or "cleanup" when the plan implies clearer ownership.
- `tasks.md` should contain bounded checkbox tasks, not themes or vague epics.

## Completion gate

Decomposition is only complete when `workstreams/manifest.json` exists and each declared stream has usable `scope.md`, `tasks.md`, and `task-state.json` artifacts.
