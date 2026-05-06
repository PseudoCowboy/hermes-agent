# Subsystem Spec: <subsystem-name>

## How To Fill This Template

Use this file only when the feature has a subsystem large enough to deserve its own contract. Keep requirements local to this subsystem. Reference product requirement IDs instead of restating whole product rules.

## Purpose

Describe the subsystem boundary and why it exists.

## Related Product Requirements

- `PROD-<id>`:

## In Scope

-

## Out Of Scope

-

## Interfaces

- Inputs:
- Outputs:
- Adjacent systems:
- Ownership boundary:
- Data this subsystem may mutate:
- Data this subsystem must not mutate:

## Requirements

- `<SUBSYSTEM>-001`. The subsystem MUST ...
- `<SUBSYSTEM>-002`. The subsystem MUST NOT ...
- `<SUBSYSTEM>-003`. On failure, the subsystem MUST ...

## Data Contract

| Field or message | Type | Required | Meaning | Producer | Consumer |
|---|---|---|---|---|---|
| `<field>` | `<type>` | yes | `<meaning>` | `<producer>` | `<consumer>` |

## State And Transitions

| State | Entered when | Exited when | Allowed next states |
|---|---|---|---|
| `<state>` | `<event>` | `<event>` | `<state>` |

## Edge Cases

- timeout:
- duplicate input:
- partial failure:
- restart/retry:
- concurrent operations:
- invalid permissions:
- missing dependencies:

## Negative Requirements

- The subsystem MUST NOT ...
- The subsystem MUST reject ...

## Observability

- What evidence shows this subsystem is working?

## Acceptance Mapping

| Subsystem requirement | Acceptance check | E2E case |
|---|---|---|
| `<SUBSYSTEM>-001` | `ACC-<id>` | `E2E-<id>` |
