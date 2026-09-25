## Context
Two running read controllers were observed with the same state file and different /tmp and /run/user/1000 socket roots. Both delegated turns are now terminal. Existing installed controllers are preserved during this repository fix.

## Decisions
Use a short private /tmp runtime directory derived from UID and a hash of resolved state-file identity, keeping Unix socket paths bounded. Request serialization and daemon ownership locks live alongside the resolved state file, so custom socket paths cannot bypass ownership. Acquire the owner lease before SessionDaemon construction. Check the recorded live legacy owner PID before touching its state or runtime; fail safely with an actionable migration message, never automatically signal a process from a PID record.

Normal live follow-ups retain the same session ID. Lost/restarted records reject prompts before any turn_started event or backend dispatch. --new-session acknowledges loss for that key only, clears its live record and sends the full new packet. Reject this option for healthy live sessions; use --rotate for a deliberate live handoff. Old history is preserved; provider cache retention itself cannot be guaranteed.

## Risks and migration
Old controllers do not hold the new state lock. A live PID in existing state blocks migration conservatively, including uncertain PID reuse. Operator must settle old turns and stop the old controller deliberately before upgrading. Existing duplicate controllers require explicit cleanup; this patch does not repair or terminate them automatically. Use the same HOME/CODEX_HOME/state identity across harnesses. Different XDG_RUNTIME_DIR no longer changes the endpoint.

## Verification
Deterministic tests vary XDG_RUNTIME_DIR, compete on one state with different custom sockets, guard old owner state before cleanup, exercise action_required followed by a second turn and UI lookup, and reject context loss before dispatch until explicit reset. No real provider spending or production acceptance claims.
