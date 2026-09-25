## MODIFIED Requirements

### Requirement: Normal history and conservative restart
Normal follow-up turns MUST reuse the owned live session and preserve its history, including after action_required. The controller MUST open a bounded follow tail sufficient to establish the session cursor and observe the next turn without requiring the full prior transcript to fit in one WebSocket frame. After failure, runtime death or controller restart, continuation MUST fail before backend dispatch until the operator explicitly supplies --new-session with a fresh delegation packet. Historical records MUST remain in DSH. --new-session MUST NOT replace a healthy live session, and --rotate MUST NOT bypass lost-context acknowledgement. Provider cache hit rate is not guaranteed.

#### Scenario: follow-up retains live history
- **WHEN** a turn ends with action_required and a second prompt uses the same key and live owner
- **THEN** both turns have the same session ID and runtime origin and the UI lookup returns that runtime

#### Scenario: lost context fails closed
- **WHEN** a prompt targets a failed or restarted session without --new-session
- **THEN** it fails with a lost-context explanation before creating a session, starting a turn or calling the provider

#### Scenario: failure starts fresh live context
- **WHEN** the operator acknowledges lost context using --new-session and a new packet
- **THEN** a fresh session is created and the restart is reported without deleting history

#### Scenario: healthy context cannot be reset accidentally
- **WHEN** --new-session targets a healthy live session
- **THEN** the request is rejected and the session is preserved

#### Scenario: A long completed session receives a follow-up
- **WHEN** prior durable history is larger than the bounded WebSocket frame but the last message fits
- **THEN** the controller opens a small history tail, retains the same session identity, receives the new turn's durable events and completes without requiring a fresh session

#### Scenario: The most recent frame itself exceeds the bound
- **WHEN** even the bounded opening frame exceeds the reader's safety limit
- **THEN** the follow-up fails with a bounded cause naming the frame limit, and the controller does not silently create a new session or dispatch a prompt
