# Remove Scout turn time limits

## Why

Issue #14: the launcher ended an active Hermes MiMo Pro turn after its one-hour deadline, losing the live session and provider cache. The owner requires turns to run until DSH completes, the launcher disconnects, or a real transport/runtime failure occurs.

## What changes

- Remove the public launcher turn-timeout option and its implicit one-hour default.
- Keep the controller, SDK reader, web follow, and launcher socket open for the entire turn without a wall-clock deadline.
- Keep bounded connection startup, handshake, cancellation confirmation, and explicit observer wait windows. An observer timeout never cancels a Scout.
- Document and verify the distinction with tests; install the corrected skill package after settling existing turns.

## Scope

No provider/model switch, prompt retry, cache reset, or change to one-writer/one-reader ownership.
