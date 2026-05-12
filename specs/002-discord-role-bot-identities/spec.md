# Feature Specification: Discord Role Bot Identities

**Feature Branch**: `002-discord-role-bot-identities`
**Created**: 2026-05-12
**Status**: Draft
**Input**: User request: "for now there are only one bot ... I need you to add different bot role, the bot token I will set later."

## User Scenarios & Testing

### User Story 1 - Configure Bot Identities Per Agent Role (Priority: P1)

An operator can configure separate Discord bot tokens for multi-agent roles such as Athena, Prometheus, Argus, Apollo, Atlas, and Hephaestus. When no role token is configured, that role keeps using the existing primary Discord bot so current deployments continue to run.

**Why this priority**: The current orchestration path uses a single bot identity for all agent speech. Role-specific bot identities are the requested user-visible behavior.

**Independent Test**: Load gateway config with role bot token environment variables and verify each configured role resolves to the intended token without requiring a real Discord connection.

**Acceptance Scenarios**:

1. **Given** `DISCORD_FRONTEND_BOT_TOKEN` is set, **When** a frontend stream posts to Discord, **Then** the message is sent through the frontend role bot client.
2. **Given** no backend role token is set, **When** a backend stream posts to Discord, **Then** the message is sent through the primary Discord adapter.
3. **Given** two roles use the same token, **When** the adapter connects, **Then** only one auxiliary client is created for that token and both roles share it.

---

### User Story 2 - Keep Inbound Routing and Admin Actions Single-Owner (Priority: P1)

The primary Discord bot remains responsible for receiving user messages, creating project categories/channels, routing sessions, and waiting for reactions. Auxiliary role bots are send-only identities for agent-authored messages.

**Why this priority**: Multiple bots all processing inbound messages would duplicate agent turns and break project isolation. Keeping one inbound authority preserves the behavior that passed the benchmark.

**Independent Test**: Configure role bots and verify channel creation and reaction wait tools still use the primary adapter while `discord_post_message` uses the role sender for the active dispatch context.

**Acceptance Scenarios**:

1. **Given** a role bot is configured, **When** `!new` is posted in the home channel, **Then** only the primary bot handles project bootstrap.
2. **Given** the orchestrator waits for an approval reaction on a role-authored message, **When** the project owner reacts, **Then** the primary bot's reaction waiter resolves the approval.
3. **Given** a role bot connection fails, **When** the role tries to post, **Then** the system falls back to the primary bot and logs the failure rather than stopping the workflow.

---

### User Story 3 - Preserve Project and Stream Isolation (Priority: P2)

Role bot identities must not let one stream post into another stream's channel or mutate another project's state. The session-bound channel, project scope, and stream scope remain authoritative.

**Why this priority**: Distinct bot identities are presentation-level routing, not a new authority boundary.

**Independent Test**: Dispatch `discord_post_message` from frontend and backend contexts and verify the target channel is still taken from the active session when omitted, and explicit wrong-channel attempts are still constrained by existing prompt/tool policy.

**Acceptance Scenarios**:

1. **Given** a frontend stream session, **When** it posts a status update, **Then** it is sent by the frontend role bot to only the frontend stream channel.
2. **Given** a backend stream session, **When** auto-emitted progress is posted, **Then** it is sent by the backend role bot to only the backend stream channel.
3. **Given** two projects share a role bot token, **When** both projects are active, **Then** channel IDs and project runstate still isolate messages and progress updates.

---

### User Story 4 - Preserve Specialist Roles Through Decomposition (Priority: P2)

An approved spec can decompose into frontend, backend, and test workstreams. The selected `agentRole` is persisted into the workstream manifest and used to select the matching persona and Discord bot identity.

**Why this priority**: The benchmark showed spec-kit decomposition could drop specialist lane intent and accidentally route everything through the backend path.

**Independent Test**: Decompose a project with frontend, backend, and test streams and verify the manifest, status output, stream bootstrap, and personas preserve those roles.

**Acceptance Scenarios**:

1. **Given** a workstream with `agentRole: frontend`, **When** stream bootstrap runs, **Then** the frontend persona and frontend bot identity are bound.
2. **Given** a workstream with `agentRole: test`, **When** stream bootstrap runs, **Then** the test-agent persona and test bot identity are bound.
3. **Given** an invalid `agentRole`, **When** decomposition runs, **Then** the manifest is rejected rather than silently defaulting to backend.

---

### User Story 5 - Announce Project Completion (Priority: P2)

When every stream has passed review, the orchestrator reports project completion in the main channel exactly once and persists the project phase as done.

**Why this priority**: The benchmark showed branches could finish specialist work yet remain stuck without a project-level completion signal.

**Independent Test**: Mark the final incomplete stream as complete and verify the pinned rollup updates to all-complete, the project runstate becomes `phase: done`, and one final orchestrator/Athena message is sent.

**Acceptance Scenarios**:

1. **Given** all but one stream are complete, **When** the last stream review is approved, **Then** the main rollup says all streams are complete and the project phase is `done`.
2. **Given** an orchestrator role bot is configured, **When** completion is announced, **Then** the announcement is sent through the orchestrator/Athena bot identity.
3. **Given** the same stream emits a duplicate approved review event, **When** status is updated again, **Then** no second completion announcement is posted.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST support optional Discord bot token configuration per agent role.
- **FR-002**: The system MUST preserve existing single-token behavior when no role-specific token is configured.
- **FR-003**: The primary Discord adapter MUST remain the only component that receives inbound Discord messages, performs project/channel admin operations, and owns reaction waits.
- **FR-004**: Role-specific bot clients MUST be send-only clients used for agent-authored outbound messages.
- **FR-005**: `discord_post_message` MUST send through the bot identity associated with the active agent role when available.
- **FR-006**: Auto-emitted stream progress messages MUST send through the bot identity associated with the active stream role when available.
- **FR-007**: Role bot connection failure MUST NOT prevent the primary Discord adapter from connecting.
- **FR-008**: If a configured role bot is unavailable at send time, the system MUST fall back to the primary Discord adapter and log a warning.
- **FR-009**: The same token configured for multiple roles MUST be connected at most once and reused by all mapped roles.
- **FR-010**: Role names MUST support both product names (`athena`, `prometheus`, `argus`, `apollo`, `atlas`, `hephaestus`) and implementation role aliases (`orchestrator`, `frontend`, `backend`, `test`, `review`, `draft_plan`).
- **FR-011**: Role bot tokens MUST be accepted from config and from environment variables so tokens can be set later without code changes.
- **FR-012**: `workflow_decompose` MUST persist `agentRole` for each stream and reject values outside `frontend`, `backend`, and `test`.
- **FR-013**: Stream bootstrap MUST bind `frontend`, `backend`, and `test` streams to matching personas and Discord bot roles.
- **FR-014**: When all streams in project runstate are `complete`, the system MUST set project `phase` to `done`.
- **FR-015**: When a project first reaches all-streams-complete, the system MUST send one final main-channel completion announcement through the orchestrator role sender when available.
- **FR-016**: Duplicate complete events MUST NOT post duplicate completion announcements.

### Key Entities

- **Role Bot Config**: Mapping from a role alias to a token or token environment variable.
- **Role Bot Client**: A send-only Discord client authenticated with one role bot token.
- **Dispatch Role**: The agent role bound into a tool dispatch context for one agent turn.
- **Primary Discord Adapter**: The existing full Discord adapter that owns inbound routing, channel admin, reaction waits, and fallback sending.

## Success Criteria

- **SC-001**: Existing Discord gateway tests pass without role bot configuration.
- **SC-002**: Unit tests prove role-token config loads from environment variables and config blocks.
- **SC-003**: Unit tests prove outbound role dispatch selects the role client when available and falls back to the primary adapter when missing.
- **SC-004**: Stream bootstrap tests continue to create frontend and backend stream sessions with their role metadata intact.
- **SC-005**: Workflow decomposition and stream bootstrap tests prove `test` stream roles are preserved and routed to the test-agent persona.
- **SC-006**: Project status tests prove all-stream completion updates the rollup, persists `phase: done`, and sends one completion announcement only once.

## Assumptions

- Auxiliary role bot accounts are invited to the same Discord server with permission to view and send in orchestration channels.
- The primary bot token remains required for gateway startup.
- Role bots are presentation identities only; they do not independently run the gateway or receive messages in v1.
