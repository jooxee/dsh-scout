## ADDED Requirements

### Requirement: Compact observational Scout status
The installed skill MUST provide a read-only command that reports one exact mode/session key in a compact machine-readable snapshot without starting a controller or runtime, dispatching a prompt, contacting the provider, changing state, or reading the full transcript. It MUST distinguish recorded running, completed, action-required, failed, missing, lost-context and stale states. It MUST verify process identity before describing a recorded running turn as active; a live PID alone is insufficient. It MUST describe liveness as observational, not proof of model progress or repository correctness. A separate opt-in details view MUST remain bounded and exclude prompts, responses, credentials, authenticated UI URLs, repository contents and other sessions.

#### Scenario: Running owner remains visible without transcript inspection
- **WHEN** an exact session is recorded running and its controller and web runtime identities match
- **THEN** a short status call reports `running` and `active=true` without any provider or UI request

#### Scenario: Recorded running state outlives its owner
- **WHEN** a state file says running but the controller or web runtime PID is absent or belongs to another process
- **THEN** status reports `stale` with `active=false`, never a healthy running turn

#### Scenario: Terminal event is visible after owner exit
- **WHEN** the last persisted event is `turn_completed`, `action_required` or `turn_failed`
- **THEN** status reports the corresponding terminal phase and shows controller liveness separately

#### Scenario: Details are requested explicitly
- **WHEN** the caller adds `--details`
- **THEN** only bounded diagnostic metadata for the selected session is added, without full model output or secrets

#### Scenario: No state exists
- **WHEN** the selected session or state file does not exist or is malformed
- **THEN** status reports a bounded missing/unknown result without starting or repairing anything
