## Why

Issue #7: the standalone scout launcher works outside Codex, but installation and instructions assume Codex paths. Users need the same scout from Claude Code, OpenCode, Pi, Oh My Pi, Cursor, or another local shell-capable harness.

## What Changes

- Provide explicit host installation presets and an arbitrary destination, retaining the current Codex default and overwrite protection.
- Make the single skill resolve scripts from its own location and describe host-neutral supervision.
- Bundle referenced documentation and document compatibility boundaries and verified installation behavior.
- Preserve runtime behavior, shared lock/state paths, reader isolation, and explicit backend selection.
- Require disclosure and owner approval of the exact model route before delegation, including across harnesses; historical success is not model approval.

## Capabilities

### New Capabilities
- `portable-scout-installation`: install and use one complete skill package across local harnesses.

### Modified Capabilities
None.

## Impact

Existing integration change; no new plugin or core mechanism. Changes affect installer, skill, README, integration reference and installation tests. Linux runtime requirements remain. Native MCP and remote/cloud-only callers are outside this slice.
