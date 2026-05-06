# Spec Templates

These templates are a starter pack for writing modular specs that humans and LLMs can use reliably.

They are designed for this workflow:

1. A human describes an idea.
2. An LLM fills in the spec templates.
3. A reviewer checks the spec before implementation.
4. Another LLM implements against the approved spec.
5. A verifier runs the acceptance and E2E cases.

## Default Project Rule

Every new project MUST create a new repository unless the spec explicitly says the work is a modification to an existing repository. The new repository requirement must be recorded in the index, product spec, technical design, acceptance scenario, and task brief.

If the task is intentionally changing an existing repository, the spec must state:

- the existing repository path,
- why a new repository is not being created,
- which files or modules are in scope,
- what must not be touched.

Recommended order:

1. `00-spec-index-template.md`
2. `01-product-spec-template.md`
3. `02-subsystem-spec-template.md`
4. `10-technical-design-template.md`
5. `20-acceptance-scenario-template.md`
6. `21-llm-e2e-test-cases-template.md`
7. `30-llm-task-brief-template.md`
8. `40-spec-review-rubric-template.md`

You do not need every template for every feature.

## Minimum Useful Set

For a new project, use at least:

- `00-spec-index-template.md`
- `01-product-spec-template.md`
- `10-technical-design-template.md`
- `20-acceptance-scenario-template.md`
- `30-llm-task-brief-template.md`
- `40-spec-review-rubric-template.md`

Add subsystem specs when the feature has several independently implementable areas.
