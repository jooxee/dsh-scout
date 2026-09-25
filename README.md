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
- durable, append-only lifecycle supervision events with a cursor-based watcher;
- one recovery `turn_failed` per previously running session after a controller restart;
- controller-derived Git facts (pre/post prompt) plus a pending-verification
  marker per completed turn, so DSH completion is never treated as proof.

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

The controller state file records the session ID, `status: running`, active turn, and start time before it dispatches a prompt. This makes a long first turn distinguishable from a stalled or missing controller even before the model returns its final response.

On controller restart, any session persisted with `status: running` emits exactly one recovery `turn_failed` supervision event identifying the restart; it never claims a DSH result. The event stream sequence continues above the greatest valid prior record.

## Supervision events

Every delegated turn also emits lifecycle events to one append-only, user-private (`0600`) JSONL stream per controller mode at `$CODEX_HOME/state/dsh-scout/events/<mode>.events.jsonl`. Events carry `version`, `sequence`, `event_id`, `kind`, `mode`, `session_key`, `session_id`, `turn`, `emitted_at`, and — for terminal kinds — `verification_state: "pending"`. Kinds:

- `turn_started` — the state was durably marked running before dispatch;
- `turn_completed` — DSH became idle after producing a result;
- `turn_failed` — dispatch, SDK execution, timeout, or agent execution failed (or the controller restarted while a session was running);
- `action_required` — the scout ended its turn with a bounded terminal action handoff.

Each terminal event is appended only after its corresponding state (idle/error, plus repository facts) has been persisted. The stream is the cursor authority so a subscriber cannot miss a fast start-to-terminal transition; the state file also records the latest event (`last_event`) as a convenience. An event append failure is never hidden: the prompt request fails and the state records a bounded supervision error.

Subscribe with the standalone watcher:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout/scripts/watch_dsh_events.py" \
  --mode write \
  --session-key repo:issue-123:writer \
  --after-sequence 0 \
  --timeout-seconds 3600 \
  --terminal-only
```

`--after-sequence` is an exclusive cursor; `--terminal-only` restricts events to terminal kinds; `--state-root` overrides the controller state root for tests. The timeout is bounded to at most 86,400 seconds (one day); non-finite, non-positive, or above-maximum values are rejected before waiting. The watcher waits for the first matching event, prints exactly one JSON object to stdout, and exits 0. Timeout exits with code 42 and no stdout. A corrupt or truncated final JSONL record is ignored until the writer completes it; earlier valid events stay readable. Delivery is at-least-once: persist the greatest processed sequence and deduplicate by `event_id`. Events never contain prompt or response bodies, file contents, or secrets.

The blocking launcher remains compatible and additionally prints the emitted lifecycle event (`event=... kind=... sequence=...`) to stderr so existing callers see the terminal outcome. When an action envelope is valid and terminal, the control block is delivered only inside the supervision event; the launcher prints the cleaned assistant text, with oversized valid values clipped to their documented bounds. Malformed or non-terminal envelopes remain ordinary stdout verbatim. Session keys are bounded to at most 200 characters and rejected, never clipped, at prompt input and in the watcher.

The detailed contract lives in [`docs/specifications/supervision-events.md`](docs/specifications/supervision-events.md).

## Verification handoff

Completion of a DSH turn is not proof that a repository task is correct. Per completed turn the controller writes machine-readable, controller-derived facts into the state file under the session:

- `handoff_pre_facts` — Git snapshot captured at dispatch;
- `handoff_post_facts` — Git snapshot captured after the turn finished;
- `verification_facts` — `{pre_prompt, post_prompt}` combined view;
- `verification` — `{"state": "pending", ...}`.

The snapshots contain only facts the controller itself derives from the selected working directory: resolved repository root, HEAD commit, current branch, bounded `git status --porcelain=v1` lines (at most 200 lines, 240 characters each), and upstream ahead/behind divergence when it can be resolved safely. If the directory is not a Git worktree, Git fails, or the snapshot times out, a bounded error record is stored instead and dispatch proceeds. Snapshots never capture file contents, prompts, model responses, secrets, or anything a DSH model asked for, and they never execute model-provided commands.

The client wrapper then prints to stderr that the DSH handoff completed but independent orchestrator verification is still pending, along with the session key and state-file location.

**Orchestrator contract:** before reporting the task as done to a repository owner, an orchestrator must independently inspect the actual state: the real diff between the pre- and post-visit commit/status, whether commits exist and were pushed, whether the declared checks and tests actually pass, and Issue/OpenSpec/PR updates that should carry the work. The recorded facts are a snapshot aid, not an audit trail: they are captured before and after each turn, so concurrent edits between those points, in-flight DSH judges, or uncommitted transient files are still possible; treat the facts as a consistency signal, then verify against the live repository.

## Security boundary

The writer starts DSH with `danger-full-access` and `approval=never`; it inherits the authority in the user's task and repository instructions.

The reader also uses DSH's unconfined inner shell to avoid unsupported nested sandboxing, but its entire controller runs inside bubblewrap. The configured project roots and selected working directory are mounted read-only by the outer operating-system boundary. Other host paths are not made read-only, so the reader prompt must still forbid external side effects.

Only one request per mode can run at once. File locks permit one writer and one reader concurrently and reject a second writer or second reader with exit code `75`.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run_dsh_session.py scripts/watch_dsh_events.py
bash -n scripts/run-dsh-agent.sh scripts/install.sh
```

Live verification additionally requires configured provider credentials. Before the first public release, the implementation was exercised against `glm-5.3-flash` for cross-call memory, provider cache reads, forced handoff rotation, writer permissions, read-only filesystem enforcement, and writer-lock rejection.

## Project status

The first public release is tracked in [Issue #1](https://github.com/jooxee/dsh-scout/issues/1).

## License

[MIT](LICENSE)
