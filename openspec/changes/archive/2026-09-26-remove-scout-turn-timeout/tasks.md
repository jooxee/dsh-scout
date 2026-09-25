# Tasks

- [x] Remove implicit and public launcher turn deadlines across SDK, web, and client socket.
- [x] Make the supervision watcher indefinite by default while preserving its optional observer-only timeout.
- [x] Update the canonical specification, skill, CLI usage and README.
- [x] Verify simulated long turn, disconnect, finite observer timeout, focused regression suite and installed package.

Verification: 116 offline tests passed; Python compilation, shell syntax, strict OpenSpec validation and `git diff --check` passed. A new no-deadline web turn reused its session, the SDK reader waited past a synthetic delay, the launcher sent no timeout and waited without a socket deadline, and a legacy timeout request was rejected before dispatch. The installer was exercised at a temporary destination and installed into the Codex skill directory; installed skill, controller and driver compare byte-for-byte with the source. No provider call was made. The failed, inactive writer controller was stopped before installation. An idle reader controller remains alive with its pre-install code to preserve its existing DSH context; the installed files take effect for that mode only after a deliberate controller restart.
