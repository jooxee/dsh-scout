# Harness integration

Install one complete `dsh-scout` Agent Skills directory. The skill instructs
the calling agent; the Bash/Python CLI owns execution. `agents/openai.yaml`
is optional Codex presentation metadata, not a runtime dependency.

## Discovery

| Installer preset | Default user skill directory | Native reference |
|---|---|---|
| `codex` | `~/.codex/skills/dsh-scout` (respects `CODEX_HOME`) | Existing Codex installation |
| `claude-code` | `~/.claude/skills/dsh-scout` | [Claude Code](https://code.claude.com/docs/en/skills) |
| `opencode` | `~/.config/opencode/skills/dsh-scout` (respects `XDG_CONFIG_HOME`) | [OpenCode](https://opencode.ai/docs/skills/) |
| `pi` | `~/.pi/agent/skills/dsh-scout` | [Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md) |
| `omp` | `~/.omp/agent/skills/dsh-scout` | [Oh My Pi](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md) |
| `cursor` | `~/.cursor/skills/dsh-scout` | [Cursor](https://cursor.com/docs/skills) |

From a repository checkout, run `./scripts/install.sh --host HOST`.
For a project installation or a customized host root, use
`./scripts/install.sh --destination /absolute/skills/dsh-scout` instead.
The explicit path is the final skill directory, not its parent. The installer
does not modify host settings, instructions, model credentials or permissions.
Use `--force` to update an existing directory; preserve any local edits first.

Reload skills or start a fresh host session as its documentation requires.
Ensure `dsh-scout` is visible and load its `SKILL.md`. Some hosts also discover
compatible directories; duplicate installations can cause a stale copy to win
by precedence. Check the actual loaded path, and keep one selected copy per host.
Custom Pi/OMP/Claude roots should be supplied through `--destination`; presets
do not infer every host-specific environment override.

## Any shell-capable caller

Agent Skills discovery is optional. A caller can read `SKILL.md` directly and
invoke its absolute `scripts/run-dsh-agent.sh` path with `--mode`, `--cwd`,
`--session-key` and `--prompt-file`. The launcher writes the scout answer to
stdout and diagnostic/verification information to stderr. It remains alive
until the turn ends; exit 75 means a competing request holds the mode lock.
Use `--help` for supported flags. No host SDK or host-specific model is used:
the selected DSH provider/model performs the delegated work.

State stays under `${CODEX_HOME:-$HOME/.codex}/state/dsh-scout`; mode locks and
sockets stay under `${XDG_RUNTIME_DIR:-/tmp}/codex-dsh-agent-<uid>`.
Share these environment settings and the same user identity across hosts.
Keep default socket/state paths; do not create per-host runtime roots to evade
contention. Different installation locations still share the same lock domain.
The legacy names do not require Codex to be installed or running.

## Completion and live viewing

Use the host's asynchronous command completion mechanism when available. Keep
the launch request alive, register supervision immediately, and consume durable
events using `scripts/watch_dsh_events.py` with a session key and exclusive
sequence cursor. It outputs one JSON event; timeout is exit 42 with no stdout.
A watcher reporting `action_required` is not permission or verification.
See [the event contract](specifications/supervision-events.md).

If the host cannot wake from a background operation, keep the launch request
in the foreground and wait. Merely starting a detached watcher does not make
an idle agent resume. Remove any monitor when the turn terminates; independently
verify the repository before claiming success.

Retrieve `--show-ui-url` using the same mode, cwd and approved provider/model.
That scout-owned DSH UI receives live activity. Keep its authentication URL
private. A separately started DSH host only shares persisted history and must
not activate the active scout session.

## Limits and evidence

The package requires local Linux, Bash, Python 3.10+, configured DSH and
bubblewrap for read-only execution. A cloud agent or remote machine needs its
own properly configured execution environment; syncing a skill does not expose
your local DSH to it. Native MCP transport is not part of this integration.

Installation tests verify all six presets, arbitrary paths, bundled references,
CLI execution and cross-copy locking without model calls. Native references
support discovery locations; they do not prove model-driven execution in every
host. All-six-host model end-to-end acceptance is not claimed.

Before every dispatch, follow the model-approval section in `../SKILL.md`:
announce the exact approved route, explicitly select it, and obtain agreement
before any replacement. Runtime defaults are not cost approval or a spending cap.
