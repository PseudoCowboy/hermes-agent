---
name: workflow-approve-plan
description: Approve a saved workflow plan, copy the final artifact into `control/approved-plan.md`, and advance the plan state to `approved`.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, approval, plan-state, nanoclaw]
    related_skills: [workflow-plan, workflow-decompose, workflow-status]
---

# Workflow Approve Plan

Use this skill when a human wants to approve a converged plan for execution.

## Core behavior

- Confirm the target project and plan slug from file-backed state.
- Call `workflow_approve_plan`.
- Treat the tool result as the source of truth for which artifact was approved (`plan-v2.md` preferred, otherwise `plan.md`).
- Do not improvise approval by copying files manually unless the deterministic tool is unavailable.

## Required outcome

Approval is only real when:

- `control/approved-plan.md` exists
- `plans/<plan-slug>/plan-state.json` reports `approved`

After approval, explain that the next valid step is `workflow-decompose`.
