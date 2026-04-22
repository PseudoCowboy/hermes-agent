---
name: workflow-execute-stream
description: Execute one workflow stream at a time using file-backed task state, honest checkpoints, and Hermes-native implementation/review skills.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, execution, workstreams, implementation]
    related_skills: [subagent-driven-development, requesting-code-review, workflow-review-stream]
---

# Workflow Execute Stream

Use this skill to work a single decomposed stream forward one bounded task at a time.

## Core behavior

- Read the stream contract first: `workstreams/manifest.json`, `scope.md`, `tasks.md`, `task-state.json`, `progress.md`, and any relevant handoffs.
- If `tasks.md` contains checkbox tasks but `task-state.json` is empty or stale, call `workflow_sync_tasks` before execution.
- Pick one task. Do not work multiple tasks in parallel inside the same stream unless the files clearly support that split.
- Use `workflow_checkpoint` to record honest state transitions.

## Required task discipline

- Move a task to `in_progress` when active execution starts.
- Move a task to `implemented` only after the code or artifact exists, local checks/self-review are complete, and evidence paths are known.
- Add evidence paths through `workflow_checkpoint` when you have test logs, screenshots, builds, or similar artifacts.
- Do not self-approve code streams. Review must happen through `workflow_review_task` via the review skill.

## Skill composition

- Use the `subagent-driven-development` skill pattern when the stream has a real implementation plan.
- Use the `requesting-code-review` skill pattern for pre-review quality checks before handing work to the reviewer.

## Stop conditions

- If the stream files are missing, inconsistent, or still placeholders, stop and repair the workflow artifacts first.
- If review ownership is unclear, do not guess. Preserve the file-backed contract.
