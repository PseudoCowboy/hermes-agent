# Spec Writing Guide For LLM-Driven Development

## Purpose

Use this guide when you want to write a spec that a human or an LLM can reliably implement, review, and test.

The main rule is simple:

**one logical spec, multiple physical documents is fine**

Large systems should usually be split into multiple files. The important part is not keeping everything in one markdown file. The important part is keeping one source of truth.

## Core Principles

### 1. Split the documents, not the truth

- A requirement should be defined in exactly one place.
- Other docs should reference it, not restate it differently.
- If the same rule appears in three docs, the spec will drift.

### 2. Separate layers

Keep these layers distinct:

- `Product spec`
  - what the system must do
- `Subsystem specs`
  - what one area must do
- `Technical design`
  - how this repo will implement it
- `Acceptance scenarios`
  - how pass/fail is judged end to end
- `Task plan`
  - implementation order and PR breakdown

Do not mix all five layers into one document unless the work is very small.

### 3. Write for execution, not for inspiration

A spec is not a brainstorm note. It should answer:

- what must happen
- what must never happen
- what is in scope
- what is out of scope
- what pass/fail looks like
- what edge cases matter
- what the LLM is allowed to change and what it must preserve

### 4. Prefer normative language

Use words intentionally:

- `MUST`: mandatory behavior
- `MUST NOT`: forbidden behavior
- `SHOULD`: strong default, can be broken with reason
- `MAY`: optional behavior

If a sentence matters for correctness, it should usually be written as a normative requirement.

### 5. Keep implementation detail out of the product contract

These belong in technical design, not product spec:

- file names
- class names
- exact modules
- exact libraries unless mandatory to the product
- PR phases
- preferred internal architecture

The product spec should survive even if the code structure changes.

## When To Split A Spec

Split the spec when any of these are true:

- the doc is too large to load comfortably into one LLM session
- multiple subsystems have different rules
- product behavior and implementation details are getting mixed together
- the same rules are being repeated in several places
- different tasks only need different slices of the spec

Practical rule of thumb:

- if one markdown file is getting past roughly 150 to 300 lines of dense requirements, consider sharding it
- if one task only needs 20 percent of the doc, split it

## Recommended Spec Stack

For non-trivial work, use this layout:

```text
specs/<feature>/
├── README.md
├── 00-spec-index.md
├── 01-product-spec.md
├── 02-subsystem-<name>.md
├── 03-subsystem-<name>.md
├── 10-technical-design.md
├── 20-acceptance-scenarios.md
├── 21-llm-e2e-test-cases.md
└── 30-task-plan.md
```

You do not need every file for every feature. For small work, use fewer.

## Document Roles

### `00-spec-index.md`

This is the entry point.

It should contain:

- feature summary
- doc list
- precedence order
- glossary
- stable scope statement
- list of requirement groups

This is the first file you give an LLM.

### `01-product-spec.md`

This is the behavioral contract.

It should contain:

- goals
- non-goals
- actors
- core concepts
- normative requirements
- invariants
- external commands and meanings
- success definition

This should be implementation-agnostic.

### `02-subsystem-*.md`

Use subsystem specs when one feature area has enough complexity to stand on its own.

Examples:

- Discord UX
- workflow state machine
- stream runtime
- merge and review
- crash recovery

Each subsystem spec should define only its own requirements plus its interfaces with adjacent subsystems.

### `10-technical-design.md`

This is the repo-specific implementation choice.

Put here:

- module/file ownership
- data layout
- state-file shape
- exact runtime flow
- model routing
- tool boundaries
- migration strategy
- implementation phases

### `20-acceptance-scenarios.md`

This is the product-level pass/fail gate.

It should be scenario-driven and observable.

Good checks are things like:

- command input
- visible system behavior
- artifact creation
- crash recovery behavior
- isolation behavior
- negative cases

### `21-llm-e2e-test-cases.md`

This is the operator-facing execution guide for another LLM.

It should include:

- preconditions
- exact steps
- expected results
- evidence to capture
- reporting format

### `30-task-plan.md`

This is not part of the normative spec.

Use it only for:

- implementation ordering
- PR slicing
- ownership
- milestones

Task plans may change often. Product specs should change less often.

## Precedence Rules

Every spec set should state precedence explicitly. Recommended order:

1. `01-product-spec.md`
2. subsystem specs
3. `10-technical-design.md`
4. `20-acceptance-scenarios.md`
5. `30-task-plan.md`

If you want acceptance scenarios to be the final source of truth for pass/fail, say that explicitly in the index.

## Requirement IDs

Use stable IDs for important requirements.

Example scheme:

- `PROD-001`
- `PROD-002`
- `DISCORD-001`
- `STATE-001`
- `RECOVERY-001`
- `ACC-001`

Rules:

- IDs should be stable across edits when possible.
- IDs should identify one requirement, not a whole paragraph of unrelated rules.
- Acceptance checks may reference product IDs.

Example:

```text
PROD-014. The system MUST NOT begin implementation before plan approval.

ACC-006. Verify implementation does not begin before approval. Covers PROD-014.
```

## Writing Rules

### Good spec writing

- State observable behavior.
- State invariants explicitly.
- State failure behavior explicitly.
- State what happens on restart or retry.
- Distinguish stream-scoped behavior from project-scoped behavior.
- Prefer exact commands and exact states when they matter.

### Avoid these patterns

- “should probably” for important requirements
- mixing product behavior with internal file names in the same section
- repeating the same rule in product spec, design, and acceptance with slightly different wording
- vague pass criteria like “works well” or “feels good” without interpretation
- hidden assumptions that only exist in the author’s head

## What Makes A Spec LLM-Friendly

### 1. Small retrievable shards

A good spec set lets you load only what the task needs.

For an implementation task, usually give the LLM:

- the spec index
- the relevant product section or subsystem shard
- the technical design for that area
- the acceptance checks for that area

Do not load the entire spec set if the task is local.

### 2. Clear boundaries

The LLM should be able to tell:

- what is fixed by the spec
- what is an implementation choice
- what is still open

### 3. Explicit edge cases

LLMs miss hidden assumptions. Write them down:

- timeouts
- retries
- crashes
- duplicate input
- concurrency
- naming collisions
- partial failure

### 4. Observable acceptance

A spec is much easier for an LLM to implement when it knows how success will be judged.

## Authoring Workflow

Recommended flow:

1. Write `00-spec-index.md`.
2. Write `01-product-spec.md`.
3. If the feature is large, split subsystem specs.
4. Write `20-acceptance-scenarios.md`.
5. Only then write `10-technical-design.md`.
6. After the behavior is clear, write `30-task-plan.md`.
7. Implement from the spec.
8. If implementation reveals a behavior gap, update the spec first, then update code.

This is the difference between design-led work and true spec-driven work.

## Review Checklist

Before handing the spec to an LLM, check these:

- Is there one clear source of truth for each rule?
- Are product requirements separated from design choices?
- Are non-goals explicit?
- Are edge cases and failure behavior specified?
- Are pass/fail criteria observable?
- Can the work be implemented by loading only the relevant shard?
- Are commands, states, and scopes unambiguous?
- Are there any contradictions between acceptance and design docs?

## LLM Hand-Off Pattern

When asking an LLM to implement, do not just say “read the spec.” Give a bounded task packet.

Use this structure:

```text
Task:
Implement <small scoped change>.

Source of truth:
- specs/<feature>/00-spec-index.md
- specs/<feature>/01-product-spec.md
- specs/<feature>/02-subsystem-<name>.md
- specs/<feature>/10-technical-design.md
- specs/<feature>/20-acceptance-scenarios.md

Implement only:
- <specific requirement IDs>

Do not change:
- <areas out of scope>

Done when:
- <specific acceptance checks>
```

This keeps the model focused and reduces context waste.

## Change Management

When requirements change:

1. Update the source requirement first.
2. Update any cross-references.
3. Update acceptance checks.
4. Then update technical design if needed.
5. Then update the task plan.
6. Then change code.

If you change code first and spec later, the spec stops being authoritative.

## Anti-Patterns

- One giant markdown file containing product requirements, code design, PR plan, and testing notes.
- Acceptance cases that conflict with the design doc.
- A “design doc” being treated as the product contract even though it hardcodes repo details.
- Specs with no non-goals.
- Specs with no failure behavior.
- Plans that are more detailed than the requirements.

## Minimal Starter Set

For most medium-to-large tasks, start with:

- `00-spec-index.md`
- `01-product-spec.md`
- one or more `02-subsystem-*.md`
- `10-technical-design.md`
- `20-acceptance-scenarios.md`
- `21-llm-e2e-test-cases.md`

If the task is small, you can collapse subsystem specs into the product spec.

## Templates

Starter templates live in `docs/spec-templates/README.md`.

Use them as a starting structure, then rename and trim as needed.
