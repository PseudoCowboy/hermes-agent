# Feature Spec Index: <feature-name>

## How To Fill This Template

Replace every `<placeholder>`. Do not leave empty sections. If a section does not apply, write `Not applicable` and explain why in one sentence.

This index is the first file another LLM should read. It must tell the LLM which documents exist, which document wins on conflict, and whether this is a new repository or an existing-repository change.

## Status

Draft | In Review | Approved

## Owner

- Human owner:
- Spec authoring LLM:
- Reviewer:

## Source Request

Paste or summarize the original user request that started this spec.

## Purpose

One-paragraph summary of the feature and why it exists.

## Repository Policy

- Repository mode: `new-repository` | `existing-repository-change`
- New repository name:
- New repository parent directory:
- Existing repository path, if this is not a new repository:
- Reason if not creating a new repository:

Default rule: every new project MUST create a new repository. Use `existing-repository-change` only when the user explicitly asks to modify an existing repo.

## Scope

- In scope:
- Out of scope:

## Success Summary

The feature is complete when:

-

## Document Map

- `01-product-spec.md`: behavioral contract
- `02-subsystem-<name>.md`: subsystem requirements
- `10-technical-design.md`: repo-specific implementation design
- `20-acceptance-scenarios.md`: end-to-end pass/fail checks
- `21-llm-e2e-test-cases.md`: verification procedure for another LLM
- `30-task-plan.md`: implementation order and task slicing
- `40-spec-review-rubric.md`: review checklist and scoring rubric

## Precedence

1. `01-product-spec.md`
2. subsystem specs
3. `10-technical-design.md`
4. `20-acceptance-scenarios.md`
5. `30-task-plan.md`

If there is a conflict, update the lower-precedence document instead of silently choosing one behavior.

## Glossary

- `<term>`:
- `<term>`:

## Requirement Groups

- `PROD-*`: product-wide requirements
- `<SUBSYSTEM>-*`: subsystem requirements
- `ACC-*`: acceptance checks
- `E2E-*`: executable verification cases
- `TASK-*`: implementation tasks

## Requirement Traceability

| Requirement ID | Defined in | Verified by | Implemented by |
|---|---|---|---|
| `PROD-001` | `01-product-spec.md` | `ACC-001`, `E2E-001` | `TASK-001` |

## Assumptions

-

## Open Questions

-

## Risks

-

## Notes

- Add links to source artifacts if this spec is derived from earlier notes, issue threads, or design docs.
