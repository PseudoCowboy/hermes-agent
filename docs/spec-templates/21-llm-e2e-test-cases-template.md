# LLM E2E Test Cases: <feature-name>

## How To Fill This Template

Write this document for another LLM that will verify the feature. The verifier should not need hidden context. Every test case needs exact steps, expected results, and evidence to capture.

## Execution Rules

- Do not modify implementation while testing unless the operator explicitly switches to fixing mode.
- Record evidence for every test case.
- Mark blocked cases explicitly and explain the blocker.
- Treat mismatches with the product spec as defects unless the operator changes the spec.
- Verify the repository requirement before feature behavior.

## Preconditions

- Target repository path:
- Test environment:
- Required services:
- Required credentials or tokens:
- Seed data:
- Cleanup expectations:

## Evidence Format

For each test case, record:

- Status: pass | fail | blocked | not run
- Requirement IDs covered:
- Evidence:
- Notes:

## Test Cases

### E2E-REPO-001 Repository creation or selection

Goal:

Verify the project uses the repository mode defined in the spec.

Steps:

1. Inspect the repository path from the spec index.
2. Verify the repository exists.
3. Verify implementation artifacts are inside that repository.
4. If this is an existing-repository change, verify the exception is documented and out-of-scope files were not touched.

Expected results:

- New projects have a new repository.
- Existing-repository changes have a documented exception.
- Implementation artifacts are not mixed into an unrelated repository.

Evidence to capture:

- Repository path.
- `git status --short` or equivalent.
- Initial commit or change evidence.

### E2E-001 <happy-path-name>

Goal:

Verify the primary user journey.

Requirement IDs covered:

- `PROD-<id>`
- `ACC-<id>`

Steps:

1. 
2. 
3. 

Expected results:

-

Evidence to capture:

-

### E2E-002 <negative-case-name>

Goal:

Verify a required rejection or forbidden behavior.

Requirement IDs covered:

- `PROD-<id>`
- `ACC-<id>`

Steps:

1. 
2. 

Expected results:

-

Evidence to capture:

-

### E2E-003 <failure-recovery-name>

Goal:

Verify failure behavior and recovery.

Requirement IDs covered:

- `PROD-FAIL-<id>`
- `ACC-<id>`

Steps:

1. Trigger `<failure>`.
2. Observe `<state>`.
3. Retry or recover using `<command/action>`.

Expected results:

-

Evidence to capture:

-

## Final Report Template

1. Overall verdict: pass | fail.
2. Repository requirement verdict.
3. Passed cases.
4. Failed cases.
5. Blocked cases.
6. Highest-severity defects.
7. Spec ambiguities found during verification.
