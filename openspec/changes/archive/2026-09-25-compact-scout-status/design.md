## Status source

The CLI reads one controller state JSON file. It does not connect to the controller socket, invoke DSH, or open the UI. The default record is one line of JSON with `mode`, `session_key`, `phase`, `active`, `controller`, `runtime`, `turn` and the last event sequence. `phase` distinguishes `running`, `completed`, `action_required`, `failed`, `context_lost`, `stale`, `idle`, `missing`, and `unknown`. `active=true` means only that the controller recorded a running turn and the expected controller and, for web mode, runtime processes still match their identities. It does not prove forward progress or correctness.

For Linux liveness, inspect `/proc/<pid>/cmdline` and match the expected controller arguments (`--serve`, exact mode, cwd and state-file) and web child patch path (`--patch` under that mode's state directory). Do not use `kill -0` alone: a reused PID is not the owner. A missing/mismatched PID makes a recorded running turn `stale`; a terminal state remains terminal with offline liveness shown separately. If process identity cannot be verified, report `unknown`, never healthy. State read/parsing failures fail closed with a bounded error state.

The optional `--details` output adds only bounded metadata (backend, timestamps, event kind and verification state). It never includes prompt, answer, repository content, provider configuration, session ID, runtime URL, auth token, logs or other sessions. The snapshot is observational and racy; terminal event stream remains the authoritative transition record. A single terminal watcher should be the normal unattended completion channel.

## Operating flow

Dispatch once and keep the launcher alive. Wait for one terminal event via `watch_dsh_events.py --terminal-only`; do not poll the DSH transcript. For an interim user status request or reconnect, run `scout_status.py --mode ... --session-key ...`. Use `--details` or the authenticated DSH UI only when the compact state is insufficient to diagnose a failure/action request or verify the completed handoff. The status query itself consumes no provider tokens.

## Risks and boundaries

No filesystem snapshot can prove the model is making progress. The CLI reports recorded activity and process identity, not semantic progress. The status file can change between read and process checks; its output is a point-in-time observation. It does not change the one-writer/one-reader limit, cancellation, persistence, or verification obligations.
