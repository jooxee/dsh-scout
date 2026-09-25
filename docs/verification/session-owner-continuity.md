# Session owner continuity — Issue #8

2026-09-25, local repository verification. No installation or live-provider acceptance is claimed.

## Reproduction and result
The observed incident had two read controllers with distinct XDG-derived sockets (/tmp and /run/user/1000), sharing read.json and runtime ownership metadata. The same-key follow-up created another session and UI lookup reached a different runtime. No secret/token or task payload is retained here.

Before the fix, the new regression lane failed: XDG changed endpoint identity, lost-context prompts dispatched silently, healthy-session reset was accepted, and no state ownership lease existed. After the fix, 102 tests pass (`python3 -m unittest discover -s tests -v`).

- `test_session_ownership.py`: XDG-independent endpoints, competing subprocess refusal, main entrypoint lease before daemon construction, legacy owner preservation, lost-context rejection before dispatch, explicit reset and same-session action_required follow-up.
- `test_install.py`: two installed package copies use one request lock despite different XDG_RUNTIME_DIR values, in both modes.
- `test_web_backend.py`: real controller and loopback Remote API/follow-stream double exercise action_required, UI URL retrieval, same session on follow-up, and explicit live rotation.
- Python compile, shell syntax and `git diff --check` pass. New test file passes Ruff check/format.

Full-file Ruff retains seven pre-existing findings in the controller; the baseline comparison adds no findings. Basedpyright retains the same ten pre-existing errors (no new error messages); strict warning counts are 393 vs baseline 384, largely the existing argparse/Any boundaries extended by the new option. No suppressions or configuration exclusions were added. C901 reports only two unchanged legacy SDK functions (11 and 13); changed/new functions are within 10. Cognitive complexity was not measured.

## Operational limits
This fixes repository code and documents migration, not existing installed processes. Settle all old turns and deliberately stop the legacy controllers before installing the new package. The new owner refuses to overwrite state while a legacy PID remains alive; it does not signal a PID from state. The actual user must retain shared HOME/CODEX_HOME/state identity across harnesses; XDG_RUNTIME_DIR may differ.

The existing provider cache cannot be reconstructed by this patch. Same live session/history supports reuse; no cache hit percentage or billing result is guaranteed. No paid-model continuation or browser reconnect acceptance test was performed after this code change. The local integration lane uses a loopback protocol double.
