## Why
Issue #8 exposed two reader controllers sharing state but using different XDG-derived sockets. Their runtime ownership and session history diverged, defeating persistent context reuse.

## What Changes
- Derive default sockets and request locks from canonical state identity, independent of XDG_RUNTIME_DIR.
- Hold a state-scoped owner lock before state loading, stale-runtime cleanup or startup; refuse a still-running legacy owner rather than killing it.
- **BREAKING**: refuse continuation after live history loss unless --new-session explicitly authorizes fresh context. --rotate remains live handoff only.
- Preserve same-key action_required follow-ups, including UI URL ownership.

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- scout-web-runtime: stable single owner and explicit recovery from lost context.

## Impact
Internal controller implementation, CLI, tests and skill documentation. No new plugin, dependency, provider calls or installed-runtime changes. https://github.com/jooxee/dsh-scout/issues/8
