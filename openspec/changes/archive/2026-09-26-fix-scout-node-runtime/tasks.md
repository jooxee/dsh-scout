## 1. Implement and verify
- [x] 1.1 Add red regression tests at prerequisite and public launcher seams.
- [x] 1.2 Implement bounded selection, propagate environment, install helper and document configuration.
- [x] 1.3 Run tests and real no-prompt startup; validate/sync/archive specs.

Delivery: commit/push and Issue #16 status are tracked in GitHub.

## Verification evidence
2026-09-26: 123 unittest tests passed. After import formatting and an explicit list type annotation, all 7 runtime regression tests passed again; Ruff check/format and C901 <=10 passed for new files, basedpyright reported 0 errors/warnings for the helper, bash syntax passed. Cognitive complexity was not measured. Existing controller formatting was preserved.

Original Node 18 dsh --help failed on parseEnv. Automatic selection chose installed Node 24.19.0; real DSH web startup announced its URL and was terminated without sending a prompt. The installed skill was updated and the same startup succeeded within the actual bubblewrap read-only project mounts. No model call, failed-session retry, provider/config change or global PATH change.
