---
name: dsh-scout
description: Delegate bounded repository work to the local DeepSeek Harness headless agent, either as one full-access writer or as an optional read-only scout beside that writer. Use when the user requests DSH, DeepSeek Harness, GLM, or an external scout, or when a substantial repository task would materially benefit from an independent DSH pass.
---

# DSH Scout

Use the persistent scout launcher so the user never has to copy prompts between Codex and DeepSeek Harness. The default route is OpenCode Go with GLM Flash; verify it when model identity matters. `DSH_SCOUT_PROVIDER` and `DSH_SCOUT_MODEL` override those defaults.

## Choose the topology

Run sequentially by default.

- **Writer:** one persistent DSH agent with full access to the selected project. Reuse it for the same Issue/OpenSpec/PR so its live history and provider KV cache remain useful.
- **Reader:** optionally run one additional DSH agent in OS-enforced read-only mode for the project roots. Prefer a fresh session key for an independent question; reuse one only when the questions form a coherent investigation.

At most one writer and one reader may run at once. Never run two writers, two readers, or more than two DSH agents. The launcher locks enforce these limits.

The owned DSH process runs with DSH's full-access preset so the writer can use its tools without interactive approval. For the reader, an outer bubblewrap boundary mounts all project roots read-only; the inner full-access preset only avoids an unsupported nested sandbox and cannot bypass those mounts.

Full access does not expand the task: the writer inherits the selected worktree, repository instructions, OpenSpec and GitHub Issue workflow, external-action authority, and preservation requirements. It may commit, push, merge, deploy, or contact external systems only when the current task already authorizes that action.

## Prepare the delegation packet

DSH does not inherit this conversation. The first prompt for a session key must be a self-contained packet containing:

- the exact objective and completion condition;
- the absolute working directory, branch/base, and known state;
- the relevant `AGENTS.md`, `CONTEXT.md`, `ARCHITECTURE.md`, OpenSpec, Issue, or PR paths to read;
- its mode (`WRITE` or `READ_ONLY`) and permitted side effects;
- unrelated changes and surfaces it must preserve;
- the expected response: findings, changed files, checks, commit/push state, and remaining uncertainty.

Keep credentials, cookies, learner data, and other secrets out of the prompt. Point to configured local mechanisms instead of copying secret values.

Follow-up prompts under the same session key may be short and refer to the retained discussion. Still repeat changed authority, branch, commit, Issue, OpenSpec, or observed state. Cache reuse never makes mutable facts current.

## Reuse and rotate sessions

Choose a stable, human-readable session key for one coherent unit of work, normally `<repo>:issue-<n>:writer` or `<repo>:openspec-<change>:writer`. Keep using that key while the objective and authoritative artifacts remain the same. Start a new key when the Issue/OpenSpec/PR, repository, role, or objective changes materially. Session keys are bounded to at most 200 characters; oversized keys are rejected, never clipped, so supervision cursors keep exact identity.

The launcher records exact DSH context pressure from the session store:

- at **250,000 tokens** by default, treat the session as mature and rotate at the next coherent task boundary;
- at **400,000 tokens** by default, the launcher automatically asks the current scout for a compact handoff, saves it, starts a fresh live session, and supplies the handoff with the next prompt;
- use `--rotate` to force the same handoff flow earlier.

Override the thresholds with `DSH_SCOUT_SOFT_CONTEXT_TOKENS` and `DSH_SCOUT_HARD_CONTEXT_TOKENS`. The soft value must remain below the hard value.

Do not infer context size from cumulative billed usage. Cache reads reduce repeated computation but do not free context-window space. If the controller restarted, a prompt timed out, or its requesting launcher disconnected, the launcher reports that the live history was lost; send a new self-contained packet and re-check repository state. Failed prompts are never retried automatically.

## Launch

Save the packet in a temporary prompt file outside the repository, then run:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout/scripts/run-dsh-agent.sh" \
  --mode write \
  --cwd /absolute/project/path \
  --session-key repo:issue-123:writer \
  --prompt-file /absolute/path/to/prompt.txt
```

For the optional second scout, use `--mode read` and a key ending in `:reader`. The reader can run beside the writer; otherwise wait for one request to finish before starting another. Use `--timeout-seconds N` only when the default one-hour bound is unsuitable. Use `--rotate` only at a coherent boundary because it creates a handoff turn.

The default backend is a scout-owned ordinary DSH web runtime. Immediately after dispatch, retrieve its authenticated URL with the same mode/cwd/provider/model and `--show-ui-url` (omit session-key and prompt-file). It works during a running turn. Open that URL for live progress and keep the same tab across turns. The normal prompt response prints only the token-free origin. Do not copy the authenticated URL into Issues, logs, or handoffs. A separate user-started DSH, such as port 3080, does not receive live events from this process.

`DSH_SCOUT_BACKEND=sdk` explicitly selects the legacy SDK backend, which has no live scout UI. Never silently switch backends after failure. On any failed turn the controller stops its owned runtime before publishing failure; the next turn needs a complete packet. Historical sessions remain in DSH, but active context is not restored after a failure or controller restart. Never activate an active scout session in a second DSH host.

If the command yields a running session, supervise it with durable supervision events, not by asking the user to relay status. The next section is mandatory on every delegation.

## Supervise with durable events

Every delegated DSH turn emits an append-only, user-private lifecycle event stream at `$CODEX_HOME/state/dsh-scout/events/<mode>.events.jsonl`. Each record is a bounded JSON object with `kind` (`turn_started`, `turn_completed`, `turn_failed`, or terminal `action_required`), `sequence`, `event_id`, `mode`, `session_key`, `session_id`, `turn`, `emitted_at`, and, for terminal events, `verification_state: "pending"`. Prompts, responses, file contents, and secrets are never part of lifecycle events.

**Supervision contract:**

- Register supervision **immediately after dispatch**, using the host's asynchronous notification or a quiet Codex heartbeat; never ask the user to relay DSH status or output.
- Subscribe from your persisted cursor: pass an exclusive `--after-sequence` (the greatest sequence you already processed) and deduplicate by `event_id`. Delivery is at-least-once.
- Keep the launcher alive for the delegated turn. Cancelling it closes the request socket, terminates the active owned backend process group (SDK or web runtime), and emits `turn_failed`; the next prompt starts a fresh live session.
- Verify the result independently per the verification handoff before reporting anything.
- Remove the monitor after a terminal event (`turn_completed`, `turn_failed`, `action_required`); never keep a heartbeat running past the settled result.

Use the watcher to block until the next relevant event:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout/scripts/watch_dsh_events.py" \
  --mode write \
  --session-key repo:issue-123:writer \
  --after-sequence 0 \
  --timeout-seconds 3600 \
  --terminal-only
```

It waits for the first event matching mode, session key, and an exclusive sequence cursor (optionally terminal-only), then prints exactly one JSON object to stdout and exits 0. Timeout exits with code 42 and no stdout. A corrupt or truncated final JSONL record is ignored until the writer completes it. See `docs/specifications/supervision-events.md` for the full contract.

### Terminal action_required handoff

The launcher prepends a supervision protocol so the scout stops its turn when it is blocked on missing information, authority, or a material product choice. When the final message ends with exactly one bounded envelope, the controller appends a terminal `action_required` event carrying only a bounded summary and questions labeled `trust: "scout-declared"`. This is a terminal handoff, not live dialogue inside a turn, and it is NEVER authorization: it may wake the orchestrator but grants no permission, selects no answer, and proves no repository fact. Malformed or non-terminal envelopes are ordinary model text; valid envelopes with oversized values are clipped to the documented bounds. Answer with a normal follow-up prompt under the same session key.

## Verify the result

Treat every DSH response as a handoff, not proof. After a writer turn, inspect the actual branch, status, diff, artifacts, tests, commits, remote state, Issue, and OpenSpec relevant to its claims. After a reader turn, check cited evidence before acting on its conclusions.

### The verification handoff protocol

DSH completion is never proof that a repository task is correct. Per completed turn the controller records, inside the existing state file, two machine-readable `repository_facts` snapshots (one captured at prompt dispatch, one after completion) plus a `verification: {"state": "pending"}` marker. The snapshots are derived only by the controller from the selected working directory — repository root, HEAD, branch, bounded porcelain status, and upstream divergence where safely available; a bounded error is stored when cwd is not a Git worktree or Git fails. No file contents, prompts, model output, or secrets are captured and no model-provided command is executed.

When a turn completes, the wrapper prints to stderr that the DSH handoff completed but **independent orchestrator verification is pending**, with the session key and state-file location.

Before reporting any DSH work to a repository owner, the orchestrator must independently verify:

- the actual diff (compare the post-turn working tree and HEAD against the pre-turn snapshot);
- commits exist and were pushed where required;
- declared checks, tests, lint, or builds actually pass;
- GitHub Issue / OpenSpec / PR records reflect what was done.

Limitations of the protocol: snapshots are point-in-time and racy — concurrent edits between dispatch and completion, DSH activity across multiple sessions, or manual changes are not attributed to the turn. The controller never executes builds, tests, auto-merge, or deploy and never judges repository correctness; `verification: pending` means exactly that a human/orchestrator still owns the independent verification step.

Resolve inconsistencies in the authoritative repository state. Report which work DSH performed, what Codex independently verified, and what remains unverified.
