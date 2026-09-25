# scout-ui-presentation Specification

## Purpose

Define how scout sessions become identifiable and live-visible in an ordinary DSH workspace UI: readable names, exact-cwd workspace grouping, live streaming and completion without reload in the scout runtime's UI, and the honest limits of that visibility.

## Requirements

### Requirement: Readable session names
Scout sessions MUST appear under a readable, human-stable title derived from the session key rather than the `MODE: WRITE.`/`MODE: READ_ONLY.` prompt prefix. On the web backend the title MUST be pinned as a user-owned title before the first prompt, so it survives DSH's first-prompt title generation. The first prompt MUST still lead with a bounded identity line so sessions remain identifiable in raw logs and on the explicit `sdk` backend, where DSH's deterministic first-prompt title fallback applies.

#### Scenario: pinned title in the scout UI
- **WHEN** the web backend creates a scout session
- **THEN** the UI lists it under the session key (bounded to DSH's title limits, control/escape characters stripped) and later automatic title generation does not replace it

#### Scenario: readable fallback without a host
- **WHEN** a turn runs on the explicit `sdk` backend
- **THEN** the first message begins with `Session <session-key>` so DSH's deterministic fallback produces a readable title instead of the mode prefix

### Requirement: Exact-cwd workspace grouping
Scout sessions MUST be grouped under the workspace registered for the exact canonical execution cwd (including worktree and symlinked directories), and grouping MUST NOT rewrite or reinterpret the execution cwd. On the web backend grouping MUST be established when the session is created inside the owning runtime, validated by DSH's own canonical-path membership rule.

#### Scenario: session groups under its exact directory
- **WHEN** a scout session runs with cwd `/abs/project` (or a worktree path)
- **THEN** the UI shows it under the workspace for that canonical path, not under *Ungrouped*

#### Scenario: cwd is never rewritten for grouping
- **WHEN** the workspace path and session header cwd canonicalize to the same directory
- **THEN** grouping succeeds without altering the path tools execute in

### Requirement: Live visibility without reload
While a scout turn runs, an already-open browser tab connected to the scout-managed runtime MUST receive that turn's durable events (tool activity, assistant messages) and the completion event over the same live channel the UI consumes, without reloading; the session's running/busy state MUST reflect the turn because execution happens in the runtime's own process. A viewer that connects mid-turn MUST receive a snapshot and then subsequent frames without a restart of the runtime.

#### Scenario: tool activity and assistant stream arrive live
- **WHEN** a delegated turn executes tools and streams its answer
- **THEN** an open UI tab shows the tool activity and assistant text while the turn is still running

#### Scenario: completion arrives without reload
- **WHEN** the turn reaches `turn/end`
- **THEN** the same open tab shows the turn as completed without any page refresh

#### Scenario: late viewer joins mid-turn
- **WHEN** a second live connection opens while a turn is active
- **THEN** it receives an opening snapshot followed by the remaining frames of that turn

### Requirement: Documented visibility limits
The documentation MUST state, and behavior MUST match, these limits: live delivery exists only in the scout-managed runtime for sessions it owns (DSH delivers events process-locally, so an unrelated already-running DSH host shows only persisted rows, never live scout turns); the shared session store means other DSH processes may list scout sessions after restart, and DSH uses a session lease to reject a second owner and this skill MUST NOT attempt foreign-host activation; opening the scout UI requires the per-activation launch URL; and the UI shows no live progress before the runtime starts (the first delegation of a mode).

#### Scenario: foreign host shows no live scout turn
- **WHEN** an operator's separate DSH web host is already running while a scout turn executes
- **THEN** that host does not stream the scout turn, and the documentation directs the operator to the scout runtime's URL instead

#### Scenario: limits stated in the docs
- **WHEN** an operator reads the README/spec for the UI behavior
- **THEN** the process-local delivery limit, shared-store listing, launch-URL requirement, and ownership boundary are all stated explicitly
