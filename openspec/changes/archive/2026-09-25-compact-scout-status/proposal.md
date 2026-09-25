## Why

The orchestrator currently opens the full DSH transcript repeatedly to learn whether a Scout is still running. The existing event watcher announces terminal transitions, but it does not answer a compact current-state query after a reconnect or between events. This wastes orchestrator context and encourages unverified claims about activity.

## What Changes

- Add a read-only status CLI for one exact mode and session key, with a compact JSON snapshot by default.
- Derive lifecycle from controller state and its last event; verify controller and web-runtime process identity before calling a recorded turn active.
- Add a bounded `--details` view for diagnosis. Neither view starts a daemon, dispatches a prompt, reads a model transcript, or prints credentials/UI tokens.
- Make the skill prefer terminal-event waiting and a compact status snapshot; reserve the DSH UI for failures, action requests, and evidence review.

## Capabilities

### Modified Capabilities

- `scout-web-runtime`: expose honest liveness alongside existing lifecycle events.

## Impact

`scripts/scout_status.py`, installer, skill, integration/supervision docs and focused tests. No running-controller restart or backend protocol change. [Issue #10](https://github.com/jooxee/dsh-scout/issues/10).
