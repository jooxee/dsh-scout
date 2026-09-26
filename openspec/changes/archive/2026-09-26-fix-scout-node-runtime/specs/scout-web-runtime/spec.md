## ADDED Requirements

### Requirement: Compatible Node prerequisite
Before starting a new controller, the launcher SHALL select an existing Node >=22 exposing util.parseEnv. It SHALL honor an absolute DSH_SCOUT_NODE executable named node without fallback on invalid override; otherwise prefer compatible PATH node, then installed NVM versions in descending numeric order. It SHALL prepend the selected bin directory to inherited PATH for both backends and helpers. It SHALL NOT install software, change provider/model, expose probe output, or retry a failed session.

#### Scenario: Desktop has old system Node
- **WHEN** PATH Node is incompatible and NVM contains a compatible Node
- **THEN** the launcher selects the highest compatible installed numeric NVM version before starting DSH

#### Scenario: Explicit invalid runtime
- **WHEN** DSH_SCOUT_NODE is invalid, incompatible or its bounded probe fails
- **THEN** startup stops before controller/state creation with a safe actionable error and no fallback

#### Scenario: Compatible current runtime
- **WHEN** PATH already resolves a compatible Node and no override is provided
- **THEN** it takes precedence over NVM versions

#### Scenario: No compatible runtime
- **WHEN** no discovered candidate passes the bounded probe
- **THEN** startup stops with a fixed diagnostic naming Node 22+ and DSH_SCOUT_NODE without exposing child output
