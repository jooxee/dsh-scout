# Issue #7: portable harness installation

Date: 2026-09-25. Checkout: `/home/alex/projects/dsh-scout`, `main`.

## Scope and source evidence

One Agent Skills package and the existing local CLI serve Codex, Claude Code,
OpenCode, Pi, Oh My Pi and Cursor. Arbitrary skill directories permit other
local shell-capable callers. This is Linux runtime portability between harnesses;
it does not add a remote service or Windows/macOS reader sandbox.

Native discovery references inspected:

- [Claude Code](https://code.claude.com/docs/en/skills): user skills under `.claude/skills`.
- [OpenCode](https://opencode.ai/docs/skills/): user skills under `.config/opencode/skills`, plus compatible directories.
- [Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md): portable SKILL.md and relative bundled files. Its [configuration source](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/config.ts) defines the `.pi/agent` default.
- [Oh My Pi](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md): native and compatible skill discovery. Its [native discovery source](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/discovery/builtin.ts) scans `.omp/agent/skills`.
- [Cursor](https://cursor.com/docs/skills): local user skills under `.cursor/skills`; remote/cloud skill synchronization alone does not supply a local DSH runtime.
- Codex: the previously installed package and accepted Issue #6 evidence establish the current installation layout.

## Verification record

Before implementation, all 87 existing tests passed. The single implementation
scout was observed running in its own ordinary DSH UI, grouped under the exact
repository workspace. The existing tab received the Issue #7 running session.

The scout used an explicitly selected `opencode-go-anthropic / minimax-m3`
route. After the owner raised token-cost concerns, the orchestrator stopped
the launch request. Event `write:56` recorded `turn_failed/client-disconnected`;
both launch PID 4104798 and runtime PID 3846060 exited. The supervision
monitor was deleted. Partial edits were preserved and completed/reviewed by
the orchestrator without further model calls to DSH. The user DSH PID 1744504
retained its original start time and command.

Independent checks:

- All 95 tests pass, including 8 new installation tests with subcases for all
  six host presets, default and custom roots, paths with spaces, environment
  overrides, complete byte-identical package contents, executable CLI help,
  invalid/missing/conflicting options, overwrite refusal and forced update.
- Two installed copies (Cursor and Pi) reject competing writer and reader
  requests with exit 75 while the shared lock is held. No daemon socket/state
  is created and no provider is called.
- Existing runtime, isolation, supervision and failure tests remain unchanged.
- Python compilation, Bash syntax, ShellCheck, Ruff F checks for the new tests,
  `git diff --check`, and strict OpenSpec validation pass.
- New Python test methods have Radon cyclomatic complexity at most 4. Bash
  cognitive/cyclomatic complexity was not measured by an automated metric;
  installer functions were manually reviewed and ShellCheck passes.

The model-approval rule is an instruction contract in the canonical skill,
not a runtime allowlist or enforced spending limit. CLI defaults are preserved
for compatibility and do not grant model-selection authority.

## Delivery

Implementation `8e74be1` is committed and pushed to `origin/main`.
[GitHub Actions](https://github.com/jooxee/dsh-scout/actions/runs/36159523398)
passed. The current Codex skill installation was updated with `--force`;
all 18 payload files match the repository byte-for-byte and both installed
CLI help commands succeed. Other host presets were installed only in isolated
test homes; no user's other harness configuration was modified. No deployment
or new paid scout acceptance run occurred.

## Acceptance boundary

Native documentation establishes supported discovery locations. Isolated
installation and CLI tests establish package behavior. These do not establish
that each host's configured model selected and executed the skill correctly.
Separate model-driven end-to-end runs in all six hosts and owner acceptance are
not claimed. Runtime behavior and browser streaming remain covered by
[Issue #6 evidence](issue-6.md); this change does not modify that runtime.
