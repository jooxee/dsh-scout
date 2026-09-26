## Why
Desktop PATH resolves Node 18, making installed DSH fail on node:util.parseEnv before its URL announcement (Issue #16).

## What Changes
Select an existing compatible Node before controller startup; allow an explicit override and deterministic NVM fallback. Emit safe actionable prerequisite errors.

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- `scout-web-runtime`: runtime prerequisite selection before dispatch, shared by web and SDK.

## Impact
Internal implementation in launcher/runtime prerequisite helper and installation payload. No model route, state identity, session recovery, or provider changes. No dependency installation.
