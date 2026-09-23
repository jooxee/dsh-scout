# dsh-scout

`dsh-scout` is a Codex skill for delegating repository work to a local [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness) agent without copying prompts between tools.

It keeps one writer session alive across related turns so the model can reuse conversation context and provider KV cache. An optional second scout runs with project roots mounted read-only.

## What it provides

- one persistent full-access writer;
- one optional read-only scout alongside the writer;
- stable session keys for an Issue, OpenSpec change, or pull request;
- context-pressure tracking from the DSH session store;
- a 250,000-token soft rotation warning;
- automatic handoff and rotation at 400,000 tokens;
- enforced one-writer/one-reader concurrency locks;
- explicit reporting when a controller restart loses live session history.

## Requirements

- Linux with Python 3.10 or newer;
- DeepSeek Harness with the `sdk` profile;
- `bubblewrap` (`bwrap`) for read-only scouts;
- a configured DSH provider and model.

The defaults use `opencode-go` and `glm-5.3-flash`. OpenCode Go also needs stable request affinity in the SDK profile:

```bash
dsh plugin --profile sdk add @gausszhou/dsh-opencode-session-id
```

## Install

Clone the repository and copy the skill into Codex:

```bash
git clone https://github.com/jooxee/dsh-scout.git
cd dsh-scout
./scripts/install.sh
```

The default destination is `$CODEX_HOME/skills/dsh-scout`, falling back to `~/.codex/skills/dsh-scout`. Use `./scripts/install.sh --force` to replace an existing installation.

Add a short instruction to your global or project `AGENTS.md` when you want agents to use the skill automatically:

```markdown
For substantial repository work that benefits from an external DeepSeek pass, or whenever the user requests DSH, GLM, or a scout, read `$CODEX_HOME/skills/dsh-scout/SKILL.md`. Use one full-access writer, optionally accompanied by one read-only scout; never run multiple writers.
```

## Run

Create the first self-contained delegation packet outside the target repository, then run:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout/scripts/run-dsh-agent.sh" \
  --mode write \
  --cwd /absolute/path/to/project \
  --session-key project:issue-123:writer \
  --prompt-file /absolute/path/to/prompt.txt
```

Reuse the same session key for follow-up turns under the same Issue, OpenSpec change, or pull request. Use a new key when the objective or authority changes. Add `--rotate` to create a handoff and continue in a fresh session immediately.

The optional reader uses the same command with `--mode read` and a key such as `project:issue-123:reader`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `DSH_SCOUT_PROVIDER` | `opencode-go` | DSH provider route |
| `DSH_SCOUT_MODEL` | `glm-5.3-flash` | Model ID |
| `DSH_SCOUT_SOFT_CONTEXT_TOKENS` | `250000` | Warn and rotate at a coherent boundary |
| `DSH_SCOUT_HARD_CONTEXT_TOKENS` | `400000` | Automatic handoff and rotation |
| `DSH_SCOUT_READONLY_ROOTS` | common project roots | Colon-separated roots mounted read-only for the reader |
| `CODEX_HOME` | `~/.codex` | Skill installation and controller state root |
| `DSH_HOME` | `~/.dsh` | DSH configuration and session store |

The soft context threshold must be below the hard threshold.

## How persistence works

The current DSH SDK creates sessions but cannot reopen an existing persisted session after its SDK process exits. The launcher therefore keeps one detached SDK controller alive for each mode and sends later prompts over a user-only Unix socket.

Within a live session, the request history remains append-only, which preserves the provider's reusable KV-cache prefix. Cache reuse reduces repeated computation; it does not reduce context-window occupancy. Context thresholds use DSH's persisted `contextPressure.pressureTokens`, not cumulative billed usage.

If the controller exits or the machine restarts, the next call starts a fresh live session and reports the loss of retained history. The next delegation packet must re-establish authoritative state.

## Security boundary

The writer starts DSH with `danger-full-access` and `approval=never`; it inherits the authority in the user's task and repository instructions.

The reader also uses DSH's unconfined inner shell to avoid unsupported nested sandboxing, but its entire controller runs inside bubblewrap. The configured project roots and selected working directory are mounted read-only by the outer operating-system boundary. Other host paths are not made read-only, so the reader prompt must still forbid external side effects.

Only one request per mode can run at once. File locks permit one writer and one reader concurrently and reject a second writer or second reader with exit code `75`.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run_dsh_session.py
bash -n scripts/run-dsh-agent.sh scripts/install.sh
```

Live verification additionally requires configured provider credentials. Before the first public release, the implementation was exercised against `glm-5.3-flash` for cross-call memory, provider cache reads, forced handoff rotation, writer permissions, read-only filesystem enforcement, and writer-lock rejection.

## Project status

The first public release is tracked in [Issue #1](https://github.com/jooxee/dsh-scout/issues/1).

## License

[MIT](LICENSE)
