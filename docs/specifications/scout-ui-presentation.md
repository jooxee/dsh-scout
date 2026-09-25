# Scout UI presentation

The OpenSpec capabilities `scout-web-runtime` and `scout-ui-presentation` define
requirements. This document explains the chosen integration for Issue #6.

## Execution and display

The default `web` backend starts one ordinary DSH web runtime per mode on
loopback, with an ephemeral port and `--no-open`. That process both executes
scout turns and delivers the UI's native live events. Use `--show-ui-url` with
the same cwd/mode/provider/model to obtain the authenticated URL before or
during a turn. Ordinary turn output includes only the token-free origin.

The controller uses supported Remote endpoints: `workspace/create` for the
exact canonical execution cwd, `session/create` in that workspace, and
`session/rename` with a normalized title derived from the session key (80 UTF-8
bytes). The session header's cwd is checked before dispatch. Opening
`session/follow` on `/api/remote.mux` and receiving its initial snapshot precede
`session/prompt`. A turn succeeds only after its own `turn/start` and
`turn/end` with `reason.kind=completed`. Durable assistant messages supply the
answer; an empty answer is valid. Context pressure comes from the existing
DSH projection reader, never text length or cumulative billed usage.

## Ownership and failure

The controller never prompts, renames, or adopts sessions in a foreign host.
Normal follow-up turns reuse the same owned session. Launcher and daemon OS
locks enforce one writer and at most one reader; direct concurrent prompt
requests are rejected. UI URL retrieval remains available during execution.

Timeout or launcher disconnect requests `session/cancel` and waits for a
matching durable turn end. Regardless of whether cancellation is confirmed,
the controller terminates its owned process group before publishing
`turn_failed`. Failed turns are not retried. Restart or failure gives the next
prompt a fresh live session and a restart notice; historical sessions remain
in the shared DSH store. The packet must re-establish working context.

Runtime records are written immediately after spawn, before URL discovery.
Stale reaping checks the mode-scoped marker, UID, exact patch argument,
process group, and Linux PID start ticks. Records clear only after confirmed
exit. Startup, auth, malformed protocol, provider error, timeout, and
disconnection fail explicitly. `sdk` is an explicit legacy choice, never a
fallback.

## Settings and reader boundary

A private local settings snapshot (0700 directory, 0600 files) overrides only
`agent-default-model`. `--patch` precedes profile flags. The operator's
settings are read, never written; the snapshot may itself contain sensitive
configuration and must not be logged or published. Launch tokens are kept
in memory and returned only on explicit URL retrieval. Child startup output
is drained without logging it.

The reader's controller and its child runtime inherit bubblewrap read-only
mounts for project roots and exact cwd. Moving execution to an unrestricted
host to achieve grouping is forbidden.

## Visibility limits

Live display is in the scout-owned ordinary DSH UI. An unrelated DSH host
(such as port 3080) can list shared persisted history, but cannot deliver
another process's live activity. DSH holds a per-session lease; this skill
never tries to activate an active session in another host. Each runtime
restart changes its URL/token. Writer and reader have separate runtime URLs.

## Verification

See `docs/verification/issue-6.md` for measured release evidence, including
Chrome observations without reload, real failure probes and reader mounts.
Unit regressions cover real socket framing/deadlines, Remote envelopes,
turn-end reasons, cancellation, startup cleanup, supervision and admission.
No vendor code or DSH storage is directly edited.
