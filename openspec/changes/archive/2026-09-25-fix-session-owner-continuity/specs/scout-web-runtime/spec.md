## MODIFIED Requirements

### Requirement: Cancellation, timeout, and failure semantics
A prompt timeout MUST attempt cancellation through `session/cancel` and MUST then wait a bounded interval for the admitted turn's durable `turn/end`. Cancellation counts as successful only when that turn end (or an equivalent proven idle state with no writer ever possible) is confirmed. On confirmed cancellation the controller MUST emit `turn_failed` with reason `web-timeout` (timeout) or `client-disconnected` (launcher disconnect) and MUST terminate its owned runtime before the failure event. On cancel rejection, confirmation timeout, broken transport, ambiguous turn ownership, or any dispatch/protocol/agent failure without a proven terminal state, the controller MUST terminate its own runtime process group BEFORE emitting the terminal `turn_failed` event (reason `prompt-failed`, or `web-timeout`/`client-disconnected` when those triggered the path), MUST record the session as lost, and MUST then permit a fresh safe restart only after explicit --new-session acknowledgement. A possibly-active writer MUST NEVER be kept after a reported failure. Failed prompts MUST NOT be retried automatically, and the next prompt MUST NOT queue behind an uncancellable zombie turn. `sdk-timeout` remains the SDK backend's timeout reason.

#### Scenario: timeout with confirmed cancel still stops the runtime
- **WHEN** the prompt deadline passes, `session/cancel` is accepted, and the turn's `turn/end` arrives within the bounded confirmation window
- **THEN** `turn_failed` carries reason `web-timeout`, the owned runtime exits before that event, and the next explicitly acknowledged --new-session prompt starts a fresh live session while historical records remain viewable

#### Scenario: unconfirmed cancel terminates the runtime before the terminal event
- **WHEN** `session/cancel` is rejected, the confirmation window expires, or the transport breaks while cancelling
- **THEN** the owned runtime process group is terminated before `turn_failed` is appended, the session is recorded as lost, and the next explicitly acknowledged --new-session prompt starts a fresh runtime and a fresh session

#### Scenario: launcher disconnect cancels the turn
- **WHEN** the requesting launcher socket closes mid-turn
- **THEN** cancellation is attempted with the same confirmation rule, `turn_failed` carries reason `client-disconnected`, and no possibly-active writer survives the reported failure

#### Scenario: remote error becomes prompt-failed
- **WHEN** the runtime answers a dispatch with a Remote error (for example authentication or model-unavailable)
- **THEN** the turn fails with reason `prompt-failed`, a bounded scrubbed detail is recorded, the owned runtime is terminated before the terminal supervision event, and no fallback backend starts

### Requirement: Normal history and conservative restart
Normal follow-up turns MUST reuse the owned live session and preserve its history, including after action_required. After failure, runtime death or controller restart, continuation MUST fail before backend dispatch until the operator explicitly supplies --new-session with a fresh delegation packet. Historical records MUST remain in DSH. --new-session MUST NOT replace a healthy live session, and --rotate MUST NOT bypass lost-context acknowledgement. Provider cache hit rate is not guaranteed.

#### Scenario: follow-up retains live history
- **WHEN** a turn ends with action_required and a second prompt uses the same key and live owner
- **THEN** both turns have the same session ID and runtime origin and the UI lookup returns that runtime

#### Scenario: lost context fails closed
- **WHEN** a prompt targets a failed or restarted session without --new-session
- **THEN** it fails with a lost-context explanation before creating a session, starting a turn or calling the provider

#### Scenario: failure starts fresh live context
- **WHEN** the operator acknowledges lost context using --new-session and a new packet
- **THEN** a fresh session is created and the restart is reported without deleting history

#### Scenario: healthy context cannot be reset accidentally
- **WHEN** --new-session targets a healthy live session
- **THEN** the request is rejected and the session is preserved

### Requirement: Verified child lifecycle
The controller MUST record runtime identity immediately after spawning it and before URL discovery. Stale-child reaping MUST verify the mode-scoped marker, UID, exact patch argument, process group, and Linux process start ticks. Startup and shutdown MUST be bounded, and an ownership record MUST clear only after confirmed exit. The launcher and daemon MUST hold per-mode OS locks for their lifetimes; direct concurrent prompt requests MUST be rejected.

#### Scenario: partial startup output
- **WHEN** a child never finishes its URL announcement
- **THEN** startup times out, the owned process group is stopped, and no fallback starts

#### Scenario: stale child reaped safely
- **WHEN** a recorded PID is alive but its identity does not match
- **THEN** no signal is sent to that process

#### Scenario: competing launcher
- **WHEN** another prompt is still active
- **THEN** a second launcher is rejected while UI URL retrieval remains available

#### Scenario: environment-independent owner
- **WHEN** prompt and UI callers share a canonical state file but have different XDG_RUNTIME_DIR values
- **THEN** default socket and lock identity are identical

#### Scenario: custom sockets cannot split state ownership
- **WHEN** a second daemon targets an owned state file through a different socket
- **THEN** it is rejected before reading or writing session state or cleaning up a runtime

#### Scenario: legacy owner remains untouched
- **WHEN** a live controller PID is recorded by a version that lacks the state owner lock
- **THEN** a new controller refuses startup before modifying state or signalling the old process and reports the required deliberate migration
