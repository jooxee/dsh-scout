# scout-web-runtime Specification

## Purpose

Define how the skill runs scout turns on a scout-managed ordinary DSH web runtime — configuration, lifecycle, authentication, single ownership, failure and cancellation semantics — so execution and UI events stay in one owner process with no hidden backend fallback.

## Requirements

### Requirement: Backend configuration and default
The controller MUST select its execution backend from `DSH_SCOUT_BACKEND` with `web` as the documented default and `sdk` as an explicit opt-in. Any other value MUST be rejected with a clear error before any process is started or any prompt is dispatched.

#### Scenario: default backend is web
- **WHEN** `DSH_SCOUT_BACKEND` is unset
- **THEN** the controller starts a scout-managed web runtime and dispatches turns through its Remote API

#### Scenario: sdk backend is explicit opt-in
- **WHEN** `DSH_SCOUT_BACKEND=sdk`
- **THEN** the controller starts the legacy DSH SDK process and never starts a web runtime

#### Scenario: invalid backend value is rejected
- **WHEN** `DSH_SCOUT_BACKEND=bogus`
- **THEN** the controller exits with an error naming the variable and the valid values, starting no process

### Requirement: No hidden backend fallback
A failure of the configured backend MUST fail the daemon or turn explicitly (with the reason recorded in state or as a `turn_failed` supervision event) and MUST NOT silently start the other backend.

#### Scenario: web runtime fails to start
- **WHEN** the web runtime does not print its authenticated URL within the bounded startup window or its process exits first
- **THEN** the daemon reports a startup failure pointing at the bounded log tail and no SDK process is ever started

#### Scenario: sdk fails in sdk mode
- **WHEN** the SDK process fails while `DSH_SCOUT_BACKEND=sdk`
- **THEN** the turn fails with the existing SDK failure semantics and no web runtime is started

#### Scenario: web runtime fails before or during a prompt
- **WHEN** the web backend cannot start (or restart) when a prompt is dispatched
- **THEN** the request fails explicitly with a bounded error before any dispatch, and no SDK process is ever started as a fallback

### Requirement: Runtime lifecycle and URL surfacing
The web runtime MUST bind loopback only, MUST NOT auto-open a browser, MUST be started with an ephemeral port unless `DSH_SCOUT_WEB_PORT` sets one, and MUST run with the same permission environment as the SDK backend (`danger-full-access` approval `never`). The authenticated launch URL (containing the per-activation token) MUST be shown to the operator on each runtime generation and on demand, MUST be usable for an already-open browser session, and the token MUST be held only in controller memory — never persisted to the state file and never written to logs (log lines are redacted).

#### Scenario: runtime boots and announces its URL
- **WHEN** a mode's daemon starts the web backend successfully
- **THEN** the next prompt response carries the authenticated loopback URL once for that generation so the operator can open or keep open the UI

#### Scenario: token is never persisted
- **WHEN** the daemon writes state, supervision events, or log files
- **THEN** no launch token appears in any of them, and the state records only the loopless origin, pid, and generation

#### Scenario: URL is retrievable on demand
- **WHEN** the operator requests the UI URL while the daemon is running
- **THEN** the current authenticated URL is returned from controller memory without starting a prompt

#### Scenario: URL is retrievable during the first active turn
- **WHEN** a `ui-url` request arrives while a prompt turn is still running in the serve loop
- **THEN** it is served concurrently from controller memory — never blocked by the serial serve loop or the one-writer lock — and no token is written to state, events, or logs

#### Scenario: backend identity is checked when reusing a daemon
- **WHEN** the launcher pings a running daemon whose recorded backend differs from the requested `DSH_SCOUT_BACKEND`
- **THEN** the daemon is not reused as if it matched; the mismatched daemon is stopped and replaced so an old `sdk` daemon is never silently kept for a requested `web` backend (or vice versa)

### Requirement: Single execution owner
For web-backend sessions the controller MUST send prompts and cancellations only to its own runtime process, MUST NOT run an SDK process for those sessions, and MUST NOT prompt any other DSH host. Adoption of an existing session MUST happen only inside the controller's own runtime.

#### Scenario: no SDK process exists in web mode
- **WHEN** a turn completes on the web backend
- **THEN** no `dsh --profile sdk` process was started by the controller for that turn

#### Scenario: no external host is prompted
- **WHEN** the controller presents or executes a session
- **THEN** it addresses only the runtime it started, never an operator-hosted DSH web host

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

### Requirement: Cancellation and failure semantics
Closing the requesting launcher MUST attempt cancellation through `session/cancel` and MUST then wait a bounded interval for the admitted turn's durable `turn/end`. Cancellation counts as successful only when that turn end (or an equivalent proven idle state with no writer ever possible) is confirmed. On confirmed cancellation the controller MUST emit `turn_failed` with reason `client-disconnected` and MUST terminate its owned runtime before the failure event. On cancel rejection, confirmation timeout, broken transport, ambiguous turn ownership, or any dispatch/protocol/agent failure without a proven terminal state, the controller MUST terminate its own runtime process group BEFORE emitting the terminal `turn_failed` event, MUST record the session as lost, and MUST then permit a fresh safe restart only after explicit --new-session acknowledgement. A possibly-active writer MUST NEVER be kept after a reported failure. Failed prompts MUST NOT be retried automatically, and the next prompt MUST NOT queue behind an uncancellable zombie turn. Connection startup, WebSocket handshake and cancellation confirmation MAY retain bounded failure-detection windows; those windows MUST NOT limit an admitted model turn.

#### Scenario: legacy turn deadline is rejected
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

### Requirement: Normal history and conservative restart
Normal follow-up turns MUST reuse the owned live session and preserve its history, including after action_required. The controller MUST open a bounded follow tail sufficient to establish the session cursor and observe the next turn without requiring the full prior transcript to fit in one WebSocket frame. After failure, runtime death or controller restart, continuation MUST fail before backend dispatch until the operator explicitly supplies --new-session with a fresh delegation packet. Historical records MUST remain in DSH. --new-session MUST NOT replace a healthy live session, and --rotate MUST NOT bypass lost-context acknowledgement. Provider cache hit rate is not guaranteed.

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

#### Scenario: A long completed session receives a follow-up
- **WHEN** prior durable history is larger than the bounded WebSocket frame but the last message fits
- **THEN** the controller opens a small history tail, retains the same session identity, receives the new turn's durable events and completes without requiring a fresh session

#### Scenario: The most recent frame itself exceeds the bound
- **WHEN** even the bounded opening frame exceeds the reader's safety limit
- **THEN** the follow-up fails with a bounded cause naming the frame limit, and the controller does not silently create a new session or dispatch a prompt

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

### Requirement: Settings isolation
The scout runtime MUST run against a scout-owned settings file seeded from the operator's `$DSH_HOME/settings.yaml` with `agent-default-model` replaced by `DSH_SCOUT_PROVIDER`/`DSH_SCOUT_MODEL`, passed to the runtime as a profile patch. The scout runtime MUST NOT write the operator's settings file, and the private local snapshot MUST NOT be logged or published, since it may contain sensitive configuration.

#### Scenario: model knobs honored without touching user settings
- **WHEN** `DSH_SCOUT_PROVIDER` and `DSH_SCOUT_MODEL` are configured and the runtime executes a turn
- **THEN** the turn uses that provider/model and the operator's `settings.yaml` is byte-identical afterwards

#### Scenario: user provider routes are inherited
- **WHEN** the scout settings file is seeded
- **THEN** it preserves the operator's provider configuration sections while overriding only `agent-default-model`, and it is written owner-only

### Requirement: Reader isolation is unchanged
In read mode the controller and its web runtime MUST run inside the same outer bubblewrap sandbox as today, with the configured project roots mounted read-only; the reader MUST never be executed by an unrestricted host.

#### Scenario: reader runtime executes inside the sandbox
- **WHEN** a read-mode daemon starts the web backend
- **THEN** the runtime is a child of the bubblewrapped controller process and project roots remain read-only inside it

### Requirement: Compact observational Scout status
The installed skill MUST provide a read-only command that reports one exact mode/session key in a compact machine-readable snapshot without starting a controller or runtime, dispatching a prompt, contacting the provider, changing state, or reading the full transcript. It MUST distinguish recorded running, completed, action-required, failed, missing, lost-context and stale states. It MUST verify process identity before describing a recorded running turn as active; a live PID alone is insufficient. It MUST describe liveness as observational, not proof of model progress or repository correctness. A separate opt-in details view MUST remain bounded and exclude prompts, responses, credentials, authenticated UI URLs, repository contents and other sessions.

#### Scenario: Running owner remains visible without transcript inspection
- **WHEN** an exact session is recorded running and its controller and web runtime identities match
- **THEN** a short status call reports `running` and `active=true` without any provider or UI request

#### Scenario: Recorded running state outlives its owner
- **WHEN** a state file says running but the controller or web runtime PID is absent or belongs to another process
- **THEN** status reports `stale` with `active=false`, never a healthy running turn

#### Scenario: Terminal event is visible after owner exit
- **WHEN** the last persisted event is `turn_completed`, `action_required` or `turn_failed`
- **THEN** status reports the corresponding terminal phase and shows controller liveness separately

#### Scenario: Details are requested explicitly
- **WHEN** the caller adds `--details`
- **THEN** only bounded diagnostic metadata for the selected session is added, without full model output or secrets

#### Scenario: No state exists
- **WHEN** the selected session or state file does not exist or is malformed
- **THEN** status reports a bounded missing/unknown result without starting or repairing anything

### Requirement: Supervision observer wait is independent of the Scout turn
The lifecycle event watcher MUST wait indefinitely by default. Its optional finite timeout MUST bound only the observer command, return the documented timeout code without fabricating an event, and MUST NOT cancel, restart or change the Scout turn.

#### Scenario: Observer times out during a healthy turn
- **WHEN** a caller explicitly gives the watcher a short timeout while the Scout turn is still active
- **THEN** the watcher exits with code 42 and no event, while the Scout session remains active and can later complete

### Requirement: Compatible Node prerequisite
Before starting a new controller, the launcher SHALL select an existing Node >=22 exposing util.parseEnv. It SHALL honor an absolute DSH_SCOUT_NODE executable named node without fallback on invalid override; otherwise prefer compatible PATH node, then installed NVM versions in descending numeric order. It SHALL prepend the selected bin directory to inherited PATH for both backends and helpers. It SHALL NOT install software, change provider/model, expose probe output, or retry a failed session.

#### Scenario: Desktop has old system Node
- **WHEN** PATH Node is incompatible and NVM contains a compatible Node
- **THEN** the launcher selects the highest compatible installed numeric NVM version before starting DSH

#### Scenario: Explicit invalid runtime
- **WHEN** DSH_SCOUT_NODE is invalid, incompatible or its bounded probe fails
- **THEN** startup stops before controller/state creation with a safe actionable error and no fallback

#### Scenario: Compatible current runtime
- **WHEN** PATH already resolves a compatible Node and no override is provided
- **THEN** it takes precedence over NVM versions

#### Scenario: No compatible runtime
- **WHEN** no discovered candidate passes the bounded probe
- **THEN** startup stops with a fixed diagnostic naming Node 22+ and DSH_SCOUT_NODE without exposing child output
