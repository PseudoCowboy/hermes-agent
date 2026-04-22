---
name: workflow-review-stream
description: Review a stream task against file-backed scope, tasks, diffs, and evidence, then persist the verdict with `workflow_review_task`.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, review, evidence, code-review]
    related_skills: [requesting-code-review, workflow-execute-stream, workflow-status]
---

# Workflow Review Stream

Use this skill when a stream task is ready for review and the verdict must become durable repo state.

## Core behavior

- Review against files, not memory: `scope.md`, `tasks.md`, `task-state.json`, diffs, and evidence artifacts.
- For code work, follow the verification mindset from `requesting-code-review` before persisting a verdict.
- Persist the result with `workflow_review_task`.
- Treat the tool result as the source of truth for state transitions and review artifact paths.

## Review contract

- Record `approved` only when the current slice satisfies the scope and available evidence.
- Record `changes_requested` when behavior, quality, or evidence is insufficient.
- Include concrete findings, behavior coverage, and missing evidence in the tool payload.
- Do not let a code-stream owner review their own task.

## Completion gate

Review is complete only when:

- `workflow_review_task` succeeds
- `review-report.md` has been updated
- the task state changed to `approved` or `changes_requested`

Do not describe a verdict as durable until those artifacts exist.
