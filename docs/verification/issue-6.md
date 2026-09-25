# Issue #6 acceptance evidence

Date: 2026-09-25. Repository: `/home/alex/projects/dsh-scout`, branch `main`.
Installed DSH: `0.1.5-rc.3`. Acceptance model:
`opencode-go-anthropic / minimax-m3` (explicit route; no fallback).

## Live browser acceptance

Chrome tab 888873824 on the scout-owned ordinary DSH UI, port 42001:

1. Opened `dsh-scout:issue-6:live-check` under the `dsh-scout` workspace.
2. With that session already open, dispatched another launcher turn.
3. Without reload or navigation, the same tab showed the new prompt,
   `Running` in the session row, `Running Bash`, and `Stop generating`.
4. The tab subsequently showed `LIVE_SECOND_COMPLETE`; `Running` disappeared
   and the composer returned to `Send message`, again without reload.
5. Expanded the completed tool output: exact cwd was
   `/home/alex/projects/dsh-scout`, followed by `LIVE_SECOND_STARTED` and
   `LIVE_SECOND_FINISHED`. Two turns remained in the same conversation.

Controller evidence: session
`session-dsh-scout-ef31eeb1-c6ee-4c65-aafe-5b2b11593741`, successful terminal
events `write:44` (turn 1) and `write:46` (turn 2). Exact context pressure was
17,996/262,144 after turn 1, distinct from the UI's 36.2K billed tokens.
This is agent-observed browser acceptance; separate owner acceptance is not
claimed. Port/token can change after runtime restart.

## Real failure and isolation probes

- Unauthenticated RPC to the **owned** runtime returned HTTP 401.
- Starting a DSH runtime on an occupied test port failed within 13 seconds
  including cleanup. The child exited; no SDK fallback started.
- Real turn deadline (5 seconds, a 60-second shell task) requested cancellation,
  observed terminal cancellation, then stopped the owned runtime. Durable event
  `write:48` was `turn_failed`, reason `web-timeout`.
- Terminating a requesting launcher produced `write:50`, reason
  `client-disconnected`. The recorded runtime PID no longer existed before the
  terminal result was accepted by the verifier.
- During that turn a competing launcher exited 75. `--show-ui-url` remained
  responsive while the first turn ran.
- Real reader session `session-dsh-scout-1ed4231d-b485-4d88-9d45-0d5df91c7bc6`
  read README and attempted creating a disposable path inside the repository:
  EROFS (30), no file created. Independent `/proc/<reader-runtime>/mountinfo`
  inspection showed both `/home/alex/projects` and the exact repository mounted
  `ro`; the runtime cwd matched the repository.
- The operator's DSH PID 1744504 retained its original start time and command
  on port 3080. User `settings.yaml` checksum remained unchanged across the
  later failure probes. No user DSH restart, private configuration write,
  storage edit, deployment, or Hermes work occurred.

## Repeatable local checks

- 87 unit/integration tests pass, including real-socket WebSocket deadlines,
  fragmentation/ping, actual Remote wire envelopes, error turn-end reasons,
  startup cleanup, stale PID/start-tick checks, cancellation confirmation,
  admission, normal session reuse and rotation, durable supervision, and
  pending-verification semantics.
- Python compilation, shell syntax, Ruff F checks, and `git diff --check`.
- Strict OpenSpec validation for `add-scout-web-runtime`.
- Radon cyclomatic and cognitive-complexity checks: changed/new production
  functions meet the 10/15 targets. Existing unchanged SDK `_read_frame` and
  `prompt`, and the unchanged action-envelope parser retain their earlier
  complexity; this task does not raise it.

## Release state

Implementation commit `46b9cff` was pushed to `origin/main` and passed
[GitHub Actions](https://github.com/jooxee/dsh-scout/actions/runs/36156116240).
`scripts/install.sh --force` installed the update; all 14 installed files
were byte-identical to the repository (skill, agent metadata, launchers and
nine backend modules).

Installed controller PID 3845998 ran
`/home/alex/.codex/skills/dsh-scout/scripts/run_dsh_session.py` with exact cwd.
Its child PID 3846060 served port 46029 using the scout-owned patch. The
operator's original PID 1744504 still served 3080, unchanged.

Two installed-launcher turns completed in session
`session-dsh-scout-6754911b-1904-4f91-8e4c-ac2b1f91a034` (`write:52`, `write:54`).
Before turn 2 the Chrome tab was already open on that session. It showed the
new Running Bash, then LIVE_SECOND_COMPLETE and the idle composer, without
navigation or reload. Normal launcher output contained no authentication
token. User settings checksum still matched.

The supervision monitor was deleted after the final terminal event. The
installed writer runtime remains idle for inspection in Chrome; the reader
and earlier test runtimes were stopped. This is a local skill installation,
not a deployment. Owner acceptance remains a separate, unclaimed fact.
