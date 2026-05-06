# Spec Review Rubric: <feature-name>

## Purpose

Use this rubric to review a spec filled in by an LLM before implementation starts.

The review should answer one question: is this spec clear, complete, testable, and safe enough for another LLM to implement without hidden context?

## Review Inputs

- `00-spec-index.md`
- `01-product-spec.md`
- subsystem specs, if any
- `10-technical-design.md`
- `20-acceptance-scenarios.md`
- `21-llm-e2e-test-cases.md`
- `30-task-plan.md` or task briefs, if any

## Scoring

Use this scale for each category:

- `0`: missing or unusable
- `1`: present but ambiguous or incomplete
- `2`: usable with minor gaps
- `3`: clear, complete, and implementation-ready

## Required Gates

The spec is not approvable if any required gate fails.

- [ ] New project creates a new repository, or the existing-repository exception is explicit and justified.
- [ ] Product behavior is separated from technical design.
- [ ] Each important requirement has an ID.
- [ ] Acceptance checks are observable.
- [ ] Failure and recovery behavior is specified.
- [ ] Out-of-scope areas are explicit.
- [ ] There are no direct contradictions between product spec, design, and acceptance checks.

## Evaluation Categories

### 1. Repository Boundary

Score: 0 | 1 | 2 | 3

Checks:

- New repository requirement is recorded.
- Repository name/path is specified.
- Existing-repository exceptions are explicit.
- Allowed and forbidden file scopes are clear.

Findings:

-

### 2. Product Clarity

Score: 0 | 1 | 2 | 3

Checks:

- Goals and non-goals are clear.
- Actors and core concepts are defined.
- Requirements use `MUST`, `MUST NOT`, `SHOULD`, or `MAY` intentionally.
- Important behavior is not hidden in prose.

Findings:

-

### 3. Scope Control

Score: 0 | 1 | 2 | 3

Checks:

- In-scope and out-of-scope areas are explicit.
- The task does not invite unrelated refactors.
- Permissions and data boundaries are defined.

Findings:

-

### 4. Technical Feasibility

Score: 0 | 1 | 2 | 3

Checks:

- Design maps to the product requirements.
- Data/state contracts are specific enough.
- Interfaces are defined with callers, callees, and failure behavior.
- Open questions are not blocking implementation.

Findings:

-

### 5. Failure, Recovery, And Idempotency

Score: 0 | 1 | 2 | 3

Checks:

- Timeout behavior is specified.
- Retry behavior is specified.
- Partial failure behavior is specified.
- Restart or resume behavior is specified when relevant.
- Concurrent or duplicate input behavior is specified when relevant.

Findings:

-

### 6. Acceptance Quality

Score: 0 | 1 | 2 | 3

Checks:

- Acceptance checks are observable.
- Negative cases are included.
- Repository creation or selection is verified.
- Checks map back to requirement IDs.

Findings:

-

### 7. LLM Execution Readiness

Score: 0 | 1 | 2 | 3

Checks:

- Another LLM can identify what to read first.
- Task briefs identify the exact requirements in scope.
- Allowed and forbidden edits are clear.
- Expected final evidence is clear.

Findings:

-

### 8. Consistency And Traceability

Score: 0 | 1 | 2 | 3

Checks:

- Requirement IDs are stable and referenced.
- Acceptance checks cover important requirements.
- Technical design does not introduce unapproved product behavior.
- Terms mean the same thing across documents.

Findings:

-

## Review Verdict

- Overall score:
- Required gates passed: yes | no
- Verdict: approve | approve with minor edits | revise before implementation | reject

## Required Fixes Before Implementation

-

## Optional Improvements

-

## Reviewer Notes

-
