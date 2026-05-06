# Technical Design: <feature-name>

## How To Fill This Template

This file explains how this repository will implement the product spec. It may mention files, modules, libraries, state paths, and commands.

Do not introduce behavior here that is not backed by a product or subsystem requirement. If new behavior is discovered, update the product or subsystem spec first.

## Purpose

Describe how this repo will implement the product and subsystem requirements.

## Requirements Covered

- `PROD-<id>`:
- `<SUBSYSTEM>-<id>`:

## Repository Plan

- Repository mode: `new-repository` | `existing-repository-change`
- New repository name:
- New repository creation command or process:
- New repository initial branch:
- New repository remote, if any:
- Existing repository path, if modifying an existing repo:

Default rule: new projects create a new repository. If this design modifies an existing repository, explain the exception and list the allowed files/modules.

## Chosen Architecture

-

## Main Decisions

### 1. <decision>

- Choice:
- Why:
- Tradeoff:
- Requirement IDs covered:

### 2. <decision>

- Choice:
- Why:
- Tradeoff:
- Requirement IDs covered:

## System Flow

Describe the happy path as numbered steps.

1. 
2. 
3. 

Describe important alternate paths.

-

## Data And State

- storage paths:
- state files:
- schemas:

| Artifact | Path or location | Created by | Read by | Cleanup policy |
|---|---|---|---|---|
| `<artifact>` | `<path>` | `<component>` | `<component>` | `<policy>` |

## Interfaces

- commands:
- tools:
- APIs:

| Interface | Caller | Callee | Request shape | Response shape | Failure behavior |
|---|---|---|---|---|---|
| `<interface>` | `<caller>` | `<callee>` | `<shape>` | `<shape>` | `<behavior>` |

## Repo Surface

- files/modules to create or modify:
- files/modules explicitly out of scope:

## Concurrency And Idempotency

- Concurrent operations:
- Locking or serialization:
- Idempotency keys:
- Retry behavior:

## Failure And Recovery Design

-

## Security And Permissions

- Required permissions:
- Data access boundaries:
- Secrets handling:
- Destructive actions and safeguards:

## Observability

- Logs:
- Metrics:
- User-visible evidence:
- Debug artifacts:

## Test Strategy

- Unit tests:
- Integration tests:
- E2E tests:
- Manual checks:

## Implementation Phases

1. `TASK-001`:
2. `TASK-002`:
3. `TASK-003`:

## Open Implementation Questions

-
