---
name: workflow-plan
description: Create or refine a file-backed workflow plan and persist it under `plans/<plan-slug>/` with `workflow_save_plan`.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, planning, nanoclaw, implementation-plan]
    related_skills: [plan, writing-plans, workflow-approve-plan, workflow-status]
---

# Workflow Plan

Use this skill when the user wants a durable project plan saved into the NanoClaw-style workflow layout.

## Core behavior

- Start from repo files, not chat memory. Read `control/draft-plan.md`, existing `plans/<plan-slug>/` artifacts, and relevant coordination files.
- Use `workflow_status` early to discover current plan state and avoid clobbering an approved or decomposed plan.
- Follow the structure and specificity standards from the `writing-plans` skill when writing the actual plan content.
- Persist plan artifacts with `workflow_save_plan`, not with ad hoc file writes.
- Use `artifact="plan"` for the initial draft and `artifact="plan-v2"` for refined/final review output.

## State rules

- `workflow_save_plan` is the source of truth for `plan-state.json`.
- Use `state="draft"` for a first-pass plan, `state="clarifying"` when open questions block convergence, and `state="under_review"` when the refined plan is ready for human approval.
- If questions remain, write `questions.md` through `workflow_save_plan`.
- If decisions were made during planning, write `decision-log.md` through `workflow_save_plan`.
- Never mark a plan approved inside this skill. Approval must happen separately through `workflow_approve_plan`.

## Completion gate

Planning is not complete when the prose looks good. It is complete only when:

- the plan markdown is written under `plans/<plan-slug>/`
- `plan-state.json` reflects the current lifecycle state
- optional `questions.md` and `decision-log.md` are persisted when relevant

If persistence fails, say so clearly and do not imply the plan is ready.
