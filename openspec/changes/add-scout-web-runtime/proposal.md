# Proposal: add-scout-web-runtime

## Why

Issue #6: scout sessions appear in the DSH workspace UI only as `MODE: WRITE/READ_ONLY` prompt-prefix titles under *Ungrouped*, and — because execution runs in a separate SDK process — the already-open DSH UI never live-delivers a scout turn's tool activity, assistant stream, or completion. The previously drafted settled-boundary rename/adopt presentation (unverified `scripts/dsh_ui.py`) talks to an external host and may instantiate a host agent for an SDK-owned session, creating two owners, so it does not satisfy the live requirement.

## What Changes

- Add a **scout-managed ordinary DSH web runtime** as the default execution backend: each mode's controller boots its own `dsh web` runtime (loopback, `--no-open`, scout-owned `--patch` settings overlay) and drives turns exclusively through that runtime's supported Remote API (`session/create`, `session/rename`, `session/prompt`, `session/cancel`, `session/follow`). Execution and UI events then live in the same owner process, so an already-open browser tab on the scout runtime shows live tool/assistant streaming and completion without reload.
- **BREAKING (default behavior):** `DSH_SCOUT_BACKEND` defaults to `web`; the previous `sdk` backend becomes an explicit opt-in (`DSH_SCOUT_BACKEND=sdk`) with no live UI. There is no hidden fallback in either direction; an unstartable configured backend fails the turn/daemon explicitly.
- Readable names and exact-cwd workspace grouping happen at session creation inside the scout-owned runtime (`workspace/create` + `session/create {workspaceId}` + user-pinned `session/rename`) — no external-host adoption, no second owner.
- Isolate the scout runtime's settings: a scout-owned projection of the user's `$DSH_HOME/settings.yaml` with `agent-default-model` set to `DSH_SCOUT_PROVIDER`/`DSH_SCOUT_MODEL` is written under the skill state root and passed via `--patch`, so the runtime never writes the user's private settings (model choice honored without mutating user configuration). The private local snapshot may contain sensitive configuration and is never logged or published.
- Remove the unverified external-host presentation layer (`scripts/dsh_ui.py` and its controller wiring; outside-repo backup saved) and replace `docs/specifications/scout-ui-presentation.md` with a truthful specification (its claimed tests do not exist today).
- Keep unchanged: one-writer/one-reader locks, exact execution cwd, persistent historical records and fresh live context after restart, context-pressure rotation, durable supervision events, action-required envelope, and the reader's outer bubblewrap isolation (the whole controller, including its web runtime, stays inside the sandbox).
- Document and test backend configuration/lifecycle, startup errors, no-fallback, disconnect/cancel/timeout, authentication and protocol errors, ownership exclusivity, and read isolation.

## Capabilities

### New Capabilities
- `scout-web-runtime`: scout-managed ordinary DSH web runtime as an execution backend — configuration and defaults, lifecycle (start, URL/token surfacing, restart, shutdown, stale reaping), Remote API usage and authentication, single-owner guarantee, explicit failure modes with no hidden backend fallback, cancellation/timeout semantics, settings isolation, and preserved reader sandboxing.
- `scout-ui-presentation`: identifiability of scout sessions in the DSH workspace UI — readable session names, exact-cwd workspace grouping, live streaming and completion visibility without reload in the scout runtime's UI, and the documented live-progress limits (shared session store, external hosts, cross-process ownership).

### Modified Capabilities
<!-- none: openspec/specs contains no prior capabilities in this repository -->

## Impact

- `scripts/run_dsh_session.py` — backend dispatch, daemon lifecycle, failure/cancel paths, state fields, new `ui-url`/`--show-ui-url` surfacing.
- `scripts/dsh_web/` (new package) — runtime process manager, Remote RPC client, minimal WebSocket mux client, `session/follow` turn observer, settings seeding.
- `scripts/dsh_ui.py` (removed), `docs/specifications/scout-ui-presentation.md` (rewritten), `docs/specifications/supervision-events.md` (failure-reason deltas), `README.md`, `SKILL.md`, `scripts/install.sh`, `.github/workflows/validate.yml`.
- `tests/` — new web-backend tests; existing tests keep covering the explicit `sdk` backend.
- Shares the user's DSH session store and credentials exactly as the SDK backend already did; adds no new external dependency (Python stdlib only).
