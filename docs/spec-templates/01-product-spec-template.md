# Product Spec: <feature-name>

## How To Fill This Template

Write behavior, not implementation. Avoid file names, class names, framework choices, and PR phases unless they are externally visible product requirements.

Use requirement IDs. Every important rule should be testable by an acceptance check or E2E case.

## Purpose

Describe the user-visible outcome.

## Goals

-

## Non-Goals

-

## Actors

-

## Core Concepts

-

## Repository Requirement

- `PROD-REPO-001`. Every new project MUST create a new repository.
- `PROD-REPO-002`. The system MUST NOT place a new project's implementation inside an unrelated existing repository.
- `PROD-REPO-003`. If the user explicitly asks to modify an existing repository, the spec MUST identify the existing repository and explain why a new repository is not being created.
- `PROD-REPO-004`. The implementation MUST include repository creation or repository selection evidence in the acceptance results.

## Normative Requirements

### <area>

- `PROD-001`. The system MUST ...
- `PROD-002`. The system MUST NOT ...
- `PROD-003`. The system SHOULD ...

### Inputs And Outputs

- `PROD-010`. Given `<input>`, the system MUST produce `<output>`.
- `PROD-011`. The system MUST reject or pause when `<invalid condition>` occurs.

### State And Lifecycle

- `PROD-020`. The system MUST enter `<state>` after `<event>`.
- `PROD-021`. The system MUST NOT transition from `<state>` to `<state>` without `<condition>`.

### <area>

- `PROD-030`. The system MUST ...

## Failure Behavior

- `PROD-FAIL-001`. On `<failure>`, the system MUST ...
- `PROD-FAIL-002`. On retry, the system MUST ...
- `PROD-FAIL-003`. On partial completion, the system MUST ...

## Invariants

- `INV-001`. The system MUST always ...
- `INV-002`. The system MUST never ...

## External Commands And Meanings

- `<command>`:

## Permissions And Boundaries

- Who can trigger the feature:
- What data the feature may read:
- What data the feature may write:
- What the feature must not touch:

## Observability Requirements

- `OBS-001`. The system MUST provide evidence for ...
- `OBS-002`. Logs, artifacts, or UI state MUST show ...

## Acceptance Mapping

| Product requirement | Acceptance check | Notes |
|---|---|---|
| `PROD-001` | `ACC-001` | |

## Success Definition

Describe when the feature counts as complete from a product perspective.
