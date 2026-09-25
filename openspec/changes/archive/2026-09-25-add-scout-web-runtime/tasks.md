## Implementation

- [x] Own ordinary web runtime per mode; explicit SDK opt-in and no fallback.
- [x] Exact-cwd workspace, pinned title, follow snapshot before dispatch.
- [x] Match turn start/end and reason; preserve normal conversation history.
- [x] Bounded startup, cancellation, disconnect and verified child cleanup.
- [x] Lifetime launcher/daemon locks and responsive UI URL retrieval.
- [x] Private settings snapshot; token-free routine logs; unchanged reader OS boundary.
- [x] Preserve exact context pressure, rotation and durable supervision contracts.

## Independent verification

- [x] Socket/controller regression tests, syntax, lint, complexity and strict OpenSpec validation.
- [x] Real Chrome activity and completion in an already-open session without reload.
- [x] Real timeout, disconnect, competing launcher, auth failure and occupied-port failure.
- [x] Real reader attempt plus independent mount/cwd verification.
- [x] Update requirements and remove superseded claims from the draft.

## Release

- [x] Scoped commit and push on main (46b9cff, CI green).
- [x] Install verified files and verify installed launcher/process provenance; repeat live Chrome acceptance.
- [x] Prepare verified requirements and release evidence for spec sync/archive and Issue closure.

Evidence: `docs/verification/issue-6.md`. No deployment or owner acceptance claim.
