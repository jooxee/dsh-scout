# portable-scout-installation Specification

## Purpose
Provide one complete local DSH Scout package across shell-capable harnesses, preserving shared execution ownership and requiring explicit approved model selection before delegation.

## Requirements

### Requirement: Complete portable installation
The installer SHALL support Codex, Claude Code, OpenCode, Pi, Oh My Pi and Cursor presets plus an explicit destination, installing the same runtime, skill and referenced documents without editing host settings.

#### Scenario: Select a host
- **WHEN** a caller selects a supported host
- **THEN** the complete package is installed in its documented user skill location and scripts execute independently of the source checkout

#### Scenario: Preserve existing default
- **WHEN** the installer receives no destination options
- **THEN** it uses the existing CODEX_HOME-based Codex destination

#### Scenario: Reject unsafe or ambiguous input
- **WHEN** arguments are invalid, host and destination conflict, or an existing installation would be overwritten without force
- **THEN** installation fails explicitly before replacing files

### Requirement: Host-independent orchestration
The skill SHALL resolve bundled commands from its own directory, retain exact task cwd and shared runtime ownership, and describe actionable supervision for shell-capable hosts.

#### Scenario: Multiple installed copies
- **WHEN** callers use different installed copies with the same runtime environment
- **THEN** they share the existing writer/reader lock domain rather than creating per-host writers

#### Scenario: Host lacks asynchronous wakeups
- **WHEN** a host cannot receive background completion notifications
- **THEN** it keeps the request in the foreground and waits for completion rather than promising automatic background notification

#### Scenario: Compatibility evidence
- **WHEN** host compatibility is documented
- **THEN** native skill discovery documentation, isolated package tests, and actual host/model end-to-end checks are distinguished

### Requirement: Explicit approved model choice
The skill SHALL require the orchestrator to disclose the exact provider/model before dispatch and use an owner-approved route; a running controller or previous successful check SHALL NOT count as approval for another model.

#### Scenario: Approved route fails
- **WHEN** the approved provider/model fails
- **THEN** the orchestrator reports the failure and obtains agreement before substituting another route, without automatic model retry or substitution

#### Scenario: No approved model
- **WHEN** no approved route is known
- **THEN** the orchestrator requests a model choice before starting a scout and may continue independent local work
