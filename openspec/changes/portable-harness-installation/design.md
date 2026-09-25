## Context

The Bash/Python CLI already runs independently of Codex. The runtime defaults use legacy Codex-named directories but require no Codex process. Changing these paths would risk splitting locks and owners.

## Decisions

- Keep a single portable Agent Skills package. `agents/openai.yaml` is optional host metadata, not runtime authority.
- Installer accepts `--host codex|claude-code|opencode|pi|omp|cursor` or `--destination PATH`, mutually exclusive, and `--force`. No options preserves Codex installation. Explicit destination is the final skill directory, whose basename must be dsh-scout for portable discovery.
- Destinations: Codex uses CODEX_HOME fallback ~/.codex; Claude Code ~/.claude/skills/dsh-scout; OpenCode XDG_CONFIG_HOME fallback ~/.config plus opencode/skills/dsh-scout; Pi ~/.pi/agent/skills/dsh-scout; OMP ~/.omp/agent/skills/dsh-scout; Cursor ~/.cursor/skills/dsh-scout. Custom host locations use --destination.
- Never modify host settings or global instructions automatically. Install the runtime and linked references with the skill; document host discovery and duplicate-name precedence.
- Commands resolve relative to the loaded SKILL.md location, without changing the task cwd. The CLI remains the universal fallback for any caller able to run local commands and keep a request alive.
- Keep shared legacy state/lock paths for compatibility. Hosts must share HOME, CODEX_HOME and XDG_RUNTIME_DIR to share the controller/lock domain; installation location does not create a new domain.
- Supervise with the host's background execution/completion mechanism or keep the launcher in the foreground. Durable watcher events recover cursors; a filesystem event stream alone cannot wake an idle host.

## Risks and verification

Owner steering: model choice can consume a constrained token allowance. Skill instructions must disclose the exact provider/model before dispatch and use only an owner-approved route. A working runtime or a historical test is not approval to select its model. Missing approval blocks scout dispatch, not independent local work. This is an orchestration instruction contract, not a billing cap or runtime-enforced allowlist.

Test installations in temporary homes, all presets/custom paths (including spaces), bundled references, executable launch/watcher help, invalid options and conflicting destinations, overwrite refusal and force update. Verify different installed copies retain shared locking without a model call. Run existing runtime suite. Record native documentation support separately from actual host-driven model runs. No migration, daemon restart or new backend is needed for packaging changes.
