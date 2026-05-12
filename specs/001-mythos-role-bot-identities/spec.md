# Feature Specification: Mythos Role Bot Identities

**Feature Branch**: `codex/discord-role-bots-claude-spec-kit`
**Created**: 2026-05-12
**Status**: Draft
**Input**: Add separate Discord bot identities for Mythos roles and finish the benchmark gaps from `personal/bench/claude-spec-kit`.

## User Scenarios & Testing

### User Story 1 - Role-Specific Discord Bot Speech (Priority: P1)

An operator can configure optional Discord bot tokens for Prometheus, Argus, Hephaestus, Apollo, Atlas, and Athena. Agent-authored Discord messages are posted through that role bot when configured, and fall back to the primary bot when not configured.

**Independent Test**: Configure role tokens in a fake Discord adapter and verify Prometheus drafts, Argus reviews, and specialist completions carry the expected author role.

### User Story 2 - Keep One Inbound/Admin Bot (Priority: P1)

The primary Discord bot remains the only client that reads messages, creates project channels/categories, and routes workflow events. Role bots are send-only presentation identities.

**Independent Test**: Start the orchestrator with role tokens and verify project/channel creation still goes through the primary adapter while outbound agent messages use `send_message_as`.

### User Story 3 - Announce Final Project Completion (Priority: P2)

When every specialist discipline has posted its `WORK COMPLETE` marker, Athena announces project completion once in the project channel and the project state becomes `complete`.

**Independent Test**: Drive the translator happy path through frontend, backend, and test specialists, then verify `ProjectState.COMPLETE`, all completed disciplines are persisted, and only one Athena completion message is posted.

## Requirements

- **FR-001**: Mythos MUST accept optional role bot tokens from environment variables and YAML config.
- **FR-002**: Mythos MUST preserve existing single-bot behavior when no role token is configured.
- **FR-003**: Role bot clients MUST be send-only and MUST NOT register inbound message handlers.
- **FR-004**: The primary bot MUST remain responsible for inbound routing and channel/category creation.
- **FR-005**: Agent-authored messages MUST route through the matching role identity when available.
- **FR-006**: Duplicate completion markers MUST NOT produce duplicate project-complete announcements.
- **FR-007**: Codex-backed Mythos roles MUST include the benchmark-required sandbox bypass flag in their default command.

## Success Criteria

- **SC-001**: Existing Mythos tests continue to pass.
- **SC-002**: Config tests cover role bot tokens from env and YAML.
- **SC-003**: Orchestrator tests prove role authorship and final completion aggregation.
