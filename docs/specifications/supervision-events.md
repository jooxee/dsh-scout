# DSH Scout supervision events

Status: accepted; implemented on branch `codex/supervision-events`

## Problem

`dsh-scout` currently keeps a DSH turn alive and records its state, but the
orchestrator must reopen or poll the state to learn that the turn completed,
failed, or stopped for a user decision. That makes unattended delegation look
stalled even when DSH has already produced a result.

The DSH SDK already streams authoritative `session.status` and `session.event`
notifications. The supervision layer must turn those notifications into a
small durable event contract that a host can subscribe to. A Codex host can
then wake the originating task through its asynchronous notification or
heartbeat mechanism instead of relying on a person to ask for status.

## Goals

- Publish durable, machine-readable lifecycle events for every delegated turn.
- Let a subscriber wait from a cursor and wake only for a relevant event.
- Distinguish successful DSH completion from independent repository
  verification.
- Surface a scout-declared need for user or orchestrator action without
  granting that action or treating model text as trusted evidence.
- Preserve the existing one-writer/one-reader limits, persistent sessions,
  context rotation, read-only sandbox and blocking CLI behavior.

## Non-goals

- Judging repository correctness, running arbitrary verification commands,
  merging, deploying or authorizing side effects.
- Inventing an interactive SDK capability. The current DSH SDK has no active
  server-to-client request flow for answering questions during a running turn.
- Sending prompts, model responses, file contents, credentials or learner data
  through the supervision channel.
- Guaranteeing exactly-once delivery. Subscribers must deduplicate by event id.

## Event contract

Each controller mode owns one append-only event stream under the existing
state root. Events use this versioned shape:

```json
{
  "version": 1,
  "sequence": 42,
  "event_id": "write:42",
  "kind": "turn_completed",
  "mode": "write",
  "session_key": "repo:issue-17:writer",
  "session_id": "session-dsh-scout-...",
  "turn": 3,
  "emitted_at": 1790290000,
  "verification_state": "pending"
}
```

`kind` is one of:

- `turn_started`: the state was durably marked running before dispatch;
- `action_required`: the scout ended the turn with a bounded structured action
  request;
- `turn_completed`: DSH became idle after producing a result;
- `turn_failed`: dispatch, SDK execution, timeout, or agent execution failed.

Events are controller-derived except for the optional payload on
`action_required`. An action request is explicitly untrusted, scout-declared
text. It may wake the orchestrator, but it never grants permission, selects an
answer, changes repository state or proves a blocker.

The controller stores the latest event and sequence in the corresponding
session state as a convenience. The append-only stream remains the cursor
authority so a subscriber cannot miss a fast start-to-terminal transition.

For a cheap point-in-time question, `scripts/scout_status.py --mode MODE
--session-key KEY` prints one compact JSON snapshot. It reads only local
state and `/proc` identity, never DSH or its transcript. A recorded running
turn is marked active only when the expected controller and web runtime
processes match; that is liveness, not proof of model progress. `--details`
adds bounded diagnostic metadata on demand. Terminal events remain the
authoritative transition stream and independent verification remains pending.

## Action-required contract

The delegation preamble tells the scout to stop its current turn when it needs
information, authority or a material product choice that is unavailable. Its
final response may end with exactly one bounded envelope:

```text
<dsh-scout-action-required-v1>
{"summary":"One short reason","questions":["One concrete question"]}
</dsh-scout-action-required-v1>
```

The controller accepts the envelope only at the end of the final assistant
message, parses it as JSON, validates a strict schema, and clips the summary,
question count and question lengths. Invalid envelopes are ordinary model
text. The emitted event includes only the bounded action payload and labels it
`trust: "scout-declared"`.

This is a terminal handoff, not live dialogue inside the same DSH turn. That
limitation follows the current SDK protocol: server-to-client requests are a
documented dead capability. A later orchestrator turn may answer with a normal
follow-up prompt using the same session key.

## Subscription and delivery

Provide a watcher command that accepts:

- mode and session key;
- an exclusive `after_sequence` cursor;
- a bounded, finite timeout (at most 86,400 seconds; non-finite, zero or
  negative values are rejected before waiting);
- optional terminal-only filtering.

It waits for the first matching event, writes exactly one JSON object to
stdout and exits successfully. Timeout exits with a distinct documented code
and no fabricated event. A corrupt/truncated final JSONL line is ignored until
the writer completes it; earlier valid events remain readable.

Delivery is at least once. Subscribers persist the greatest processed
sequence and deduplicate by `event_id`. Event files are user-private, created
with restrictive permissions, and contain no model answer or repository
content.

The blocking launcher remains compatible and prints a concise terminal event
line for existing callers. Hosts with asynchronous execution should keep the
launcher or watcher promise alive, yield control, and notify the originating
task when it resolves. In Codex Desktop, the skill must register a thread
heartbeat or use the host's asynchronous `notify` facility immediately after
dispatch; the monitor stays quiet while the event cursor has not advanced and
removes itself after a terminal event.

While a prompt is active, the controller also watches the requesting launcher's
Unix socket. End-of-file means the launcher stopped waiting, so the controller
must terminate that DSH turn instead of allowing an orphaned writer to continue.
On the web backend each accepted connection runs on its own dispatch thread so a
`ui-url` request is served concurrently with an active prompt — the writer
lock and serial accept loop never block UI URL retrieval.

## State transitions

```text
dispatch accepted
  -> persist running state
  -> append turn_started
  -> DSH SDK runs
     -> valid terminal action envelope
        -> persist idle + verification pending
        -> append action_required
     -> ordinary idle result
        -> persist idle + verification pending
        -> append turn_completed
     -> error / timeout / requesting client disconnect
        -> persist error
        -> terminate the DSH SDK process group
        -> append turn_failed
        -> invalidate every live session owned by that SDK
```

A terminal lifecycle event is emitted only after its corresponding state has
been persisted. Repository facts from the verification-handoff protocol are
captured before `turn_completed` or `action_required`; both remain
`verification_state: "pending"` until the orchestrator checks the live branch,
diff, tests and external records.

## Failure and restart semantics

- Controller restart keeps prior events and continues the sequence above the
  greatest valid record.
- A prompt timeout emits `turn_failed` with reason `sdk-timeout` on the SDK
  backend or `web-timeout` on the web backend; a requesting client disconnect
  emits it with reason `client-disconnected`; any other prompt failure uses
  reason `prompt-failed`. On the web backend cancellation success requires a
  confirmed `turn/end` (or proven idle with no writer ever possible): cancel
  rejection, confirmation timeout, broken transport, or ambiguous ownership
  terminates the owned runtime process group BEFORE the terminal event is
  published, records `live_session_lost`, and an explicitly acknowledged `--new-session` prompt starts a fresh
  runtime generation. Persisted sessions are reused only when ownership and
  terminal state are both proven.
- The controller stays available after invalidation. Only an explicitly acknowledged `--new-session` prompt
  starts a fresh backend process, creates a fresh live session, and reports
  `restarted: true`; it never retries the failed prompt automatically.
- If the controller died while a session was recorded as running, startup
  emits one `turn_failed` recovery event identifying controller restart; it
  does not claim DSH completion. Recovery emission is deduplicated durably
  and per abandoned turn: an existing controller-restart recovery record is
  evidence only when its ``(session_key, session_id, turn)`` identity matches
  the recorded running session exactly, so a crash between append and state
  persistence cannot emit a duplicate and a later fresh session reusing the
  same session key still gets its own recovery event.
- Event append failure must not be hidden. The prompt request fails and the
  state records a bounded supervision error because silent completion would
  recreate the original problem.
- Subscriber failure does not affect DSH execution or repository state.
- Multiple subscribers may read the same stream independently.

## Security requirements

- Never invoke a shell string supplied by a model, prompt or event payload.
- Never treat an `action_required` payload as authorization.
- Bound every copied string and list before persistence or display.
- Do not store prompt or response bodies in lifecycle events.
- Resolve event paths below the controller-owned state directory; session keys
  are data fields, not path components. A session key is bounded to at most
  200 characters and rejected, never clipped, at prompt input and in the
  watcher CLI, because cursors and subscribers need exact session identity.
- Read-only scouts remain inside their existing outer filesystem sandbox.

## Verification

Unit tests must cover:

- ordered start/completed and start/failed events;
- timeout and requesting-client disconnect abort the SDK, emit their specific
  failure reasons, and require explicit `--new-session` acknowledgement before a fresh session;
- terminal action-envelope recognition and strict rejection of malformed,
  oversized or non-terminal lookalikes;
- cursor filtering, terminal filtering, timeout and duplicate-safe event ids;
- restart sequence recovery and stale-running recovery failure;
- state-before-event ordering and verification remaining pending;
- bounded fields, private file permissions and absence of prompt/response
  bodies;
- existing controller, rotation, writer lock and reader sandbox tests.

A local integration test may use a fake SDK stream to prove that a watcher
blocked before dispatch exits on completion without polling the controller
state. Live provider credentials are not required for acceptance.
