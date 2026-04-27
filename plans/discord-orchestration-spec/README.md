# Discord Orchestration Spec Set

This directory is a derived rewrite of the existing Discord orchestration planning artifacts. It does not modify or replace the original files under `plans/`; it separates them into cleaner layers with clearer responsibilities.

## Layers

1. `01-product-spec.md`
   Proposed source of truth for product behavior. This is the contract for what the Discord orchestration system must do.

2. `02-technical-design.md`
   Repo-specific implementation design for Hermes. This is one concrete way to satisfy the product spec.

3. `03-acceptance-scenario.md`
   End-to-end acceptance scenario. This is the pass/fail scenario for the bring-up.

4. `04-claude-e2e-test-cases.md`
   Operator-facing verification procedure written so another agent can execute and record the E2E checks consistently.

## How To Use These Docs

- If product behavior and implementation detail conflict, `01-product-spec.md` wins.
- `02-technical-design.md` may change if the code structure changes, as long as the product spec still holds.
- `03-acceptance-scenario.md` is the end-to-end gate for claiming the feature works.
- `04-claude-e2e-test-cases.md` is the execution guide for running those checks and collecting evidence.

## Source Material

These files were derived from the existing planning artifacts without editing them:

- `plans/discord-orchestration-session-handoff.md`
- `plans/discord-orchestration-bringup-test.md`
- `57a822a3:plans/discord-orchestration-design.md`

## Notes

- This rewrite intentionally removes scenario-specific requirements from the product contract when they are not load-bearing product behavior.
- This rewrite also resolves the main artifact drift by treating merge eligibility as bound to a reviewed revision, not to a moving branch tip.
