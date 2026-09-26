## Context
DSH uses env node. Desktop PATH picks Node 18; Node 22.23.1 successfully runs the same dsh --help. Arbitrary child logs are intentionally discarded for privacy.

## Goals / Non-Goals
Use an existing Node >=22 with util.parseEnv; avoid emitting child output. No shell profile sourcing, runtime installation, global PATH edits, model substitution, or failed-session retry.

## Decisions
After the existing request/owner guards and before new controller startup, select explicit absolute DSH_SCOUT_NODE (basename node), else the first PATH node if compatible, else installed NVM versions under NVM_DIR or ~/.nvm in descending numeric version order. Explicit invalid override fails without fallback. A bounded local capability probe runs each candidate without provider requests and discards output. Prepend selected bin directory to child PATH so env-node DSH and YAML helpers share it. Preserve HOME/CODEX_HOME and all other settings. Help/status remain read-only; a live daemon is reused without a new runtime probe. Prerequisite failure can leave empty lock/directories but no controller state JSON or process. Missing compatible runtime fails before controller/state creation with a fixed diagnostic naming DSH_SCOUT_NODE and Node 22+.

## Risks / Trade-offs
Only NVM fallback is discovered; other managers use PATH or explicit override. Already running daemons keep their environment. No promise that this validates every future DSH dependency. Revert commit to roll back; installed package must be updated separately.

## Verification
Regression tests cover incompatible PATH, numerical ordering, valid PATH precedence, explicit override refusal, missing executable, timeout, inherited child PATH and no controller dispatch on failure. Run full local unittest suite and actual dsh help/runtime startup with no model prompt.
