## MODIFIED Requirements

### Requirement: Turn lifecycle over the supported Remote API
Before the first prompt of a session the controller MUST create the session inside its runtime attached to a workspace for the exact cwd and MUST pin a readable user-owned title. Completion MUST be observed on the supported `session/follow` stream: the controller waits for the `turn/start` of its admitted turn and finishes on that turn's `turn/end`, taking the final answer from durable assistant messages of that turn. A completed turn with no assistant text MUST still complete with an empty answer rather than hang. A normal prompt MUST have no wall-clock execution deadline in the launcher, controller, web follow or SDK reader. The public launcher MUST NOT accept a turn-timeout option.

#### Scenario: title pinned before first prompt
- **WHEN** a session record is first created for a session key
- **THEN** the session carries a user-pinned readable title derived from the session key before any model output exists

#### Scenario: completion observed from the follow stream
- **WHEN** the admitted prompt reaches `turn/end`
- **THEN** the controller returns the turn's final assistant text, persists idle state, and emits `turn_completed` or `action_required`

#### Scenario: Long turn completes without a time limit
- **WHEN** an admitted Scout turn remains active beyond the former one-hour limit and later reaches `turn/end`
- **THEN** its launcher and controller remain attached, the same live session completes, and no time-triggered cancellation or `turn_failed` is emitted

### Requirement: Cancellation, timeout, and failure semantics
Closing the requesting launcher MUST attempt cancellation through `session/cancel` and MUST then wait a bounded interval for the admitted turn's durable `turn/end`. Cancellation counts as successful only when that turn end (or an equivalent proven idle state with no writer ever possible) is confirmed. On confirmed cancellation the controller MUST emit `turn_failed` with reason `client-disconnected` and MUST terminate its owned runtime before the failure event. On cancel rejection, confirmation timeout, broken transport, ambiguous turn ownership, or any dispatch/protocol/agent failure without a proven terminal state, the controller MUST terminate its own runtime process group BEFORE emitting the terminal `turn_failed` event, MUST record the session as lost, and MUST then permit a fresh safe restart only after explicit --new-session acknowledgement. A possibly-active writer MUST NEVER be kept after a reported failure. Failed prompts MUST NOT be retried automatically, and the next prompt MUST NOT queue behind an uncancellable zombie turn. Connection startup, WebSocket handshake and cancellation confirmation MAY retain bounded failure-detection windows; those windows MUST NOT limit an admitted model turn.

#### Scenario: timeout with confirmed cancel still stops the runtime
- **WHEN** a legacy launcher attempts to supply a prompt deadline
- **THEN** the controller rejects the request before dispatch; no timed cancellation or terminal failure is created

#### Scenario: unconfirmed cancel terminates the runtime before the terminal event
- **WHEN** `session/cancel` is rejected, the confirmation window expires, or the transport breaks while cancelling
- **THEN** the owned runtime process group is terminated before `turn_failed` is appended, the session is recorded as lost, and the next explicitly acknowledged --new-session prompt starts a fresh runtime and session

#### Scenario: launcher disconnect cancels the turn
- **WHEN** the requesting launcher socket closes mid-turn
- **THEN** cancellation is attempted with the same confirmation rule, `turn_failed` carries reason `client-disconnected`, and no possibly-active writer survives the reported failure

#### Scenario: remote error becomes prompt-failed
- **WHEN** the runtime answers a dispatch with a Remote error (for example authentication or model-unavailable)
- **THEN** the turn fails with reason `prompt-failed`, a bounded scrubbed detail is recorded, the owned runtime is terminated before the terminal supervision event, and no fallback starts

## ADDED Requirements

### Requirement: Supervision observer wait is independent of the Scout turn
The lifecycle event watcher MUST wait indefinitely by default. Its optional finite timeout MUST bound only the observer command, return the documented timeout code without fabricating an event, and MUST NOT cancel, restart or change the Scout turn.

#### Scenario: Observer times out during a healthy turn
- **WHEN** a caller explicitly gives the watcher a short timeout while the Scout turn is still active
- **THEN** the watcher exits with code 42 and no event, while the Scout session remains active and can later complete
