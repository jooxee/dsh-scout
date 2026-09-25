# Design

The launcher sends a prompt without a `timeout` field. The controller rejects any legacy request containing that field before dispatch and uses no turn deadline. The SDK frame reader selects without a deadline while still watching the requesting socket. The web follow reader and driver wait without a turn deadline while checking cancellation every short local poll. Closing the launcher remains an explicit cancellation signal. Bounded setup and cancel-confirmation windows are failure detection, not limits on model work.

The watcher defaults to waiting for a lifecycle event without a deadline. Its optional `--timeout-seconds` bounds only that observer process and returns code 42 without touching the controller or Scout. This maintains compatibility for bounded polling callers.

Tests use fake SDK/web processes and synthetic delays; they do not call a provider. A failure while updating an installed copy must leave the repository source authoritative and be reported separately.
