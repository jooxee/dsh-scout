# dsh-scout

`dsh-scout` is a portable Agent Skill and local command interface for delegating repository work to a [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness) agent. Codex, Claude Code, OpenCode, Pi, Oh My Pi, Cursor and other shell-capable local harnesses can use the same runtime.

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

The CLI compatibility defaults use `opencode-go` and `glm-5.3-flash`. Before a paid dispatch, the orchestrator must disclose and explicitly select a user-approved provider/model. Defaults and historical successful runs are not approval; failed routes must not be replaced without agreement. This instruction contract is not a runtime billing limit. OpenCode Go also needs stable request affinity in the SDK profile:

```bash
dsh plugin --profile sdk add @gausszhou/dsh-opencode-session-id
```

## Install

Clone the repository and install the skill package into the host that should use it:

```bash
git clone https://github.com/jooxee/dsh-scout.git
cd dsh-scout
./scripts/install.sh                              # Codex preset (default)
./scripts/install.sh --host claude-code           # Claude Code
./scripts/install.sh --host opencode              # OpenCode (respects XDG_CONFIG_HOME)
./scripts/install.sh --host pi                    # Pi Agent Skills
./scripts/install.sh --host omp                   # Oh My Pi
./scripts/install.sh --host cursor                # Cursor
./scripts/install.sh --destination /path/to/dsh-scout   # arbitrary custom root
```

Without `--host` or `--destination` the installer uses the Codex preset under `${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout`. `--host` and `--destination` are mutually exclusive. Every install path requires the final directory basename to be `dsh-scout` so portable discovery stays consistent. Use `--force` to overwrite an existing installation at the same destination; without it, an existing installation aborts with exit status 17. The installer never edits host settings or global host instructions; it only copies the skill package, runtime scripts, and the bundled reference documents under it. Run `./scripts/install.sh --help` for the full option list.

The set of supported host presets is:

| Host preset | Skill directory |
|---|---|
| `codex` | `${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout` |
| `claude-code` | `$HOME/.claude/skills/dsh-scout` |
| `opencode` | `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/dsh-scout` |
| `pi` | `$HOME/.pi/agent/skills/dsh-scout` |
| `omp` | `$HOME/.omp/agent/skills/dsh-scout` |
| `cursor` | `$HOME/.cursor/skills/dsh-scout` |

For hosts that need a non-default destination, use `--destination PATH`. See [`docs/integrations.md`](docs/integrations.md) for the compatibility matrix, CLI fallback, and the verified limits.

Add a short instruction to your global or project `AGENTS.md` when you want agents to use the skill automatically:

```markdown
For repository work benefiting from a DSH scout, read the installed dsh-scout SKILL.md. Disclose the exact provider/model before dispatch and use only a user-approved route. Use one writer and at most one OS-isolated reader; supervise completion and independently verify the handoff.
```

Set `DSH_SCOUT_HOME` to the absolute directory containing the loaded `SKILL.md`, or use the absolute launcher path directly. This is a shell convenience variable, not automatically set by installation. The launcher resolves its dependencies from its own location; `--cwd` remains the exact target repository directory.

## Run

Create the first self-contained delegation packet outside the target repository, then run the host-specific launcher with the same arguments:

```bash
DSH_SCOUT_HOME=/absolute/installed/skill/dsh-scout
export DSH_SCOUT_PROVIDER=approved-provider
export DSH_SCOUT_MODEL=approved-model
"${DSH_SCOUT_HOME}/scripts/run-dsh-agent.sh" \
  --mode write \
  --cwd /absolute/path/to/project \
  --session-key project:issue-123:writer \
  --prompt-file /absolute/path/to/prompt.txt
```

Replace the example provider/model with the approved route. Reuse that same environment for follow-up and UI commands. Hosts must preserve the same `HOME` and `CODEX_HOME` to share canonical state identity. Default sockets are independent of `XDG_RUNTIME_DIR`; request and owner locks live beside state, even when custom sockets are used. The historical `.codex` state directory is a compatibility location, not a dependency on Codex.

Reuse the same session key for follow-up turns under the same Issue, OpenSpec change, or pull request. Use a new key when the objective or authority changes. Add `--rotate` to create a handoff and continue in a fresh session immediately.

The optional reader uses the same command with `--mode read` and a key such as `project:issue-123:reader`.

### Surfacing the scout UI URL

On the default web backend, the prompt response includes the token-free UI
origin. Before or during the turn, fetch the authenticated launch URL with
`--show-ui-url` using the same mode, cwd, provider and model:

```bash
"${DSH_SCOUT_HOME}/scripts/run-dsh-agent.sh" \
  --mode write --cwd /absolute/path/to/project --show-ui-url
```

Only `--show-ui-url` prints the authenticated launch URL. Normal turn output
prints the token-free origin. Keep authenticated URLs out of saved logs and
GitHub handoffs; state and supervision events contain no launch token.

### Live UI visibility — limits

The scout runtime streams its own turn's events to any browser tab connected to
its URL (tool activity, assistant text, completion). The same browser tab may
also have been opened before the first turn. Process-local delivery means a
foreign already-running DSH host (e.g. the operator's `127.0.0.1:3080`) shows
no live scout turn; the documentation directs the operator to the scout
runtime's URL instead. Do not activate an active scout session from a foreign host; DSH
uses a session lease to reject a second owner, and this skill never attempts it.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `DSH_SCOUT_BACKEND` | `web` | `web` (scout-owned `dsh web` runtime, live UI) or `sdk` (legacy detached SDK, explicit opt-in) |
| `DSH_SCOUT_WEB_PORT` | ephemeral | Bind port for the scout web runtime; loopback host is fixed |
| `DSH_SCOUT_PROVIDER` | `opencode-go` | DSH provider route |
| `DSH_SCOUT_MODEL` | `glm-5.3-flash` | Model ID |
| `DSH_SCOUT_SOFT_CONTEXT_TOKENS` | `250000` | Warn and rotate at a coherent boundary |
| `DSH_SCOUT_HARD_CONTEXT_TOKENS` | `400000` | Automatic handoff and rotation |
| `DSH_SCOUT_READONLY_ROOTS` | common project roots | Colon-separated roots mounted read-only for the reader |
| `CODEX_HOME` | `~/.codex` | Skill installation and controller state root |
| `DSH_HOME` | `~/.dsh` | DSH configuration and session store |

The soft context threshold must be below the hard threshold. `DSH_SCOUT_BACKEND` accepts only `web` or `sdk`; any other value is rejected before any process is started.

## How persistence works

The default execution backend is a scout-managed ordinary `dsh web` runtime per mode (Issue #6): the controller boots one `dsh web` process with loopback host, ephemeral port, and `--no-open`, and drives turns over its supported Remote API (`workspace/create`, `session/create`, `session/rename`, `session/prompt`, `session/cancel`) plus the `/api/remote.mux` follow stream. Because execution and UI events live in the same owner process, an already-open browser tab on the scout runtime shows live tool/assistant streaming and completion without reload.

The legacy DSH SDK backend (`DSH_SCOUT_BACKEND=sdk`) remains available as an explicit opt-in for callers that prefer the older detached-SDK model; there is no hidden fallback in either direction — a startup failure fails the daemon or turn explicitly.

Within a live session, the request history remains append-only, which preserves the provider's reusable KV-cache prefix. Cache reuse reduces repeated computation; it does not reduce context-window occupancy. Context thresholds use DSH's persisted `contextPressure.pressureTokens`, not cumulative billed usage.

If live context is lost after a failure, runtime death or controller restart, the next prompt is rejected **before dispatch**. After explicitly accepting that loss, supply `--new-session` and a complete fresh packet for the same key. Healthy sessions reject this flag; use normal follow-ups to retain history or `--rotate` for a live handoff. Failed prompts are never retried automatically. Retaining session history supports prefix reuse but cannot guarantee the provider's cache hit rate.

Upgrading from XDG-based addressing: settle all old turns and deliberately stop the old controller(s) first. A live legacy PID in state blocks a new controller before state writes or runtime cleanup. The launcher never automatically kills a PID to resolve this migration. Existing running installations are not upgraded by a Git push.

The controller state file records the session ID, `status: running`, active turn, and start time before it dispatches a prompt. This makes a long first turn distinguishable from a stalled or missing controller even before the model returns its final response. On the web backend the state also records the runtime origin/pid/generation — never the launch token.

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
"${DSH_SCOUT_HOME}/scripts/watch_dsh_events.py" \
  --mode write \
  --session-key repo:issue-123:writer \
  --after-sequence 0 \
  --timeout-seconds 3600 \
  --terminal-only
```

`--after-sequence` is an exclusive cursor; `--terminal-only` restricts events to terminal kinds; `--state-root` overrides the controller state root for tests. The timeout is bounded to at most 86,400 seconds (one day); non-finite, non-positive, or above-maximum values are rejected before waiting. The watcher waits for the first matching event, prints exactly one JSON object to stdout, and exits 0. Timeout exits with code 42 and no stdout. A corrupt or truncated final JSONL record is ignored until the writer completes it; earlier valid events stay readable. Delivery is at-least-once: persist the greatest processed sequence and deduplicate by `event_id`. Events never contain prompt or response bodies, file contents, or secrets.

The blocking launcher remains compatible and additionally prints the emitted lifecycle event (`event=... kind=... sequence=...`) to stderr so existing callers see the terminal outcome. While the prompt runs, the controller watches the launcher's Unix socket; closing the launcher cancels the active turn and prevents an orphaned writer. Prompt timeouts use failure reason `sdk-timeout` (SDK backend) or `web-timeout` (web backend), launcher disconnects use `client-disconnected`, and other prompt failures use `prompt-failed`. When an action envelope is valid and terminal, the control block is delivered only inside the supervision event; the launcher prints the cleaned assistant text, with oversized valid values clipped to their documented bounds. Malformed or non-terminal envelopes remain ordinary stdout verbatim. Session keys are bounded to at most 200 characters and rejected, never clipped, at prompt input and in the watcher.

On the web backend, cancellation success requires a confirmed `turn/end` (or proven idle with no writer ever possible). When cancel is rejected, the confirmation window expires, the transport breaks, or ownership is ambiguous, the owned runtime process group is terminated BEFORE the terminal `turn_failed` event is appended and a fresh runtime/session requires an explicit `--new-session` acknowledgement — a possibly-active writer is never kept after a reported failure. Persisted sessions are reused only when both ownership and terminal state are proven.

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
python3 -m py_compile scripts/run_dsh_session.py scripts/watch_dsh_events.py scripts/dsh_web/*.py
bash -n scripts/run-dsh-agent.sh scripts/install.sh
```

Live verification additionally requires configured provider credentials. Before the first public release, the implementation was exercised against `glm-5.3-flash` for cross-call memory, provider cache reads, forced handoff rotation, writer permissions, read-only filesystem enforcement, and writer-lock rejection.

## Project status

The first public release is tracked in [Issue #1](https://github.com/jooxee/dsh-scout/issues/1).

## License

[MIT](LICENSE)
