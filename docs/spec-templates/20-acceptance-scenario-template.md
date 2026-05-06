# Acceptance Scenario: <feature-name>

## How To Fill This Template

Acceptance checks must be observable. Avoid vague checks like "works well". Each check should say what action happens and what evidence proves it passed.

## Purpose

Describe the end-to-end scenario that determines pass or fail.

## Scenario Summary

- Primary scenario:
- Secondary or isolation scenario:

## Preconditions

-

## Repository Acceptance

- [ ] `ACC-REPO-001`. A new repository exists for this project.
- [ ] `ACC-REPO-002`. The repository name and path match the spec index and technical design.
- [ ] `ACC-REPO-003`. The project implementation is committed or otherwise recorded in that repository.
- [ ] `ACC-REPO-004`. If this is an existing-repository change, the spec documents the exception and no out-of-scope files were touched.

## Acceptance Checks

### A. <area>

- [ ] `ACC-001`. Given `<input/action>`, when `<condition>`, then `<observable result>`. Covers `PROD-<id>`.
- [ ] `ACC-002`. Given `<input/action>`, when `<condition>`, then `<observable result>`. Covers `PROD-<id>`.

### B. <area>

- [ ] `ACC-010`. 
- [ ] `ACC-011`. 

### C. Failure And Recovery

- [ ] `ACC-020`. Trigger `<failure>` and verify `<expected recovery behavior>`. Covers `PROD-FAIL-<id>`.
- [ ] `ACC-021`. Trigger `<partial completion>` and verify `<expected safe state>`. Covers `PROD-FAIL-<id>`.

### D. Negative Cases

- [ ] `ACC-030`. Invalid input `<input>` is rejected with `<expected result>`.
- [ ] `ACC-031`. Unauthorized or out-of-scope action `<action>` is rejected or ignored.

## Evidence Required

- Repository evidence:
- UI or CLI evidence:
- Logs:
- State files:
- Test output:

## Verdict

Define the exact condition for calling the feature a pass.
