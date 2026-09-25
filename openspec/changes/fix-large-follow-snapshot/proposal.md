## Why
Issue #12 reproduces a cache-destroying continuation failure after a long web Scout turn: the next same-session follow exits with `follow reader failed` before the prompt starts. The affected durable session is about 3 MiB uncompressed, while the WebSocket reader permits a 1 MiB frame. The driver requests DSH's default 50-message opening history even though it needs only the session header and new turn events.

## What Changes
- Ask `session/follow` for one message of opening history, keeping its cursor and subsequent durable events intact.
- Preserve the existing WebSocket frame limit and make an oversized or broken follow failure diagnostically distinguishable without leaking content.
- Prove repeated same-session turns across a simulated long history over the real loopback protocol.

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- scout-web-runtime: bounded follow opening and healthy long-session continuation.

## Impact
Internal web driver, focused tests, specification and skill documentation. No provider calls, new dependency, schema change, hidden backend fallback or automatic fresh session. https://github.com/jooxee/dsh-scout/issues/12
