## Context

Installed DSH 0.1.5-rc.3 delivers `session/event` and
`agent/assistant-stream` within its owning process. Shared storage alone
cannot make a detached SDK turn live in another host's UI.

## Classification and scope

Internal implementation of the existing dsh-scout skill integration. No new
platform core, plugin lifecycle, vendor patch, or storage mutation. The
operator's DSH, settings, and Hermes remain untouched.

## Decisions

- Own an ordinary `dsh web` runtime per writer/reader mode and execute through
  its supported Remote API. Keep `sdk` as explicit opt-in; never fall back.
- Establish exact-cwd workspace membership and a pinned session-key title
  before prompting. Receive the follow snapshot before sending the prompt.
- Observe the admitted turn's start and end. Only `reason.kind=completed`
  succeeds; provider/error/blocked/aborted/interrupted endings fail.
- Keep normal follow-up history in one session. After any failure or runtime
  restart, retain persisted history for viewing but start a fresh live
  session, report restart, and require a full packet. This conservative rule
  supersedes the draft proposal to re-adopt sessions after cancellation.
- On timeout/disconnect, attempt cancellation with a bounded confirmation
  window, then terminate the owned process group before a failure event.
  On unconfirmed exit, retain the ownership record and refuse a new owner.
- Hold launcher and daemon flock locks for their entire lifetimes. Reject
  concurrent direct prompt requests. Serve UI URL retrieval concurrently.
- Write a private local settings snapshot with only the default model
  overridden; never change operator configuration. Credentials or other
  sensitive settings in the snapshot remain local and are not logged.
- Track child identity immediately after spawn using marker, exact patch
  path, UID, process-group identity and Linux start ticks. Drain stdout
  continuously; publish the authenticated URL only via `--show-ui-url`.
- Reader runtime inherits the controller's existing bubblewrap boundary.
  Both backends read exact context pressure through the existing projection
  reader; billed tokens never drive rotation.

## Alternatives

Renaming a detached SDK session in the user's host cannot deliver live
updates and risks activating it in another owner. A bridge that fabricates
vendor events adds unsupported lifecycle and ownership behavior. Driving
the user's host would require authority over its session process and cannot
preserve the reader's OS boundary. The owned ordinary web runtime avoids
these constraints while using the native UI and API.

## Trade-offs

Each mode has its own URL. Restart changes the port/token and starts a fresh
live context. Shared historical sessions can appear in another DSH host,
but their live events stay with the owner. Startup errors expose bounded
categories instead of raw provider diagnostics. The small stdlib WebSocket
client supports bounded text/continuation/control frames and rejects invalid
frames. Regression tests and live Chrome acceptance cover this boundary.

## Verification and release

See `docs/verification/issue-6.md`. Validate OpenSpec and tests, commit/push
only scoped changes on main, install verified files, and verify an installed
launcher turn before closing Issue #6. No deployment or user-DSH restart.
