# Changelog

## [0.6.0] - 2026-11-24

### Security

- Enforce durable rider binding on session checkout and durably invalidate generations on proven rider mismatch across processes and restarts.
- Bind cookie write-back and invalidation atomically to checkout identity (`generation`, rider UUID, directory inode), and serialize account creation/removal with credential adoption.
- Strip `UBER_LOGIN_TOKEN` from the login browser environment via `signin_window.browser.browser_environment()` and disable redirects on proof requests.
- Use strict directory fsync on connection control writes and generation-bound private caches for places and account data.
- Enforce cross-account rider uniqueness in CLI login independent of booking-cap parsing, and reject Business-only rider profiles.

### Changed

- Trim MCP server `INSTRUCTIONS` (~2.5 KB to ~0.9 KB) to reduce aggregate context size while preserving two-step preview/confirm money safety, Reserve rules, and `uber_connect` guidance.

### Added

- Document all Reserve tool parameters (`uber_reserve_ride`, `uber_reservation`, `uber_cancel_reservation`) and pin an MCP tool-surface size budget.
- Surface free-text place-match alternatives on Reserve options and previews, proposed fare on `over_cap`, old/new prices on on-demand `price_rose`, and provider readback / terminal status on Reserve cancellation.

### Fixed

- Initialize newly created private staging directories during hosted sign-in adoption so `CONNECTIONS_REQUIRE_CONTROLS=1` works for Connect and Reconnect.
- Require verifiable cancellation fees on on-demand rides (preserving explicit `chargeFee=false` and comparing fee type and value strictly).
- Reject fixed-to-metered pricing basis changes at confirmation, enforce quote expiry immediately before dispatch, and fail closed on incomplete `GetStatus` or `Activities` responses.
- Atomically re-check freeze, holds, and caps when recording Reserve submission after constructing and validating mutation variables; preserve original attempt timestamp on operator resolution and keep wrong-kind tokens intact in `take_pending`.
- Eliminate unnecessary `booking.json` rewrites on read-only `caps()` and idle reservation monitoring, skip unchanged cookie disk writes with delta merges, publish 429 and challenge pauses inside the transport lock, and move Connections viewer authorization off the ASGI event loop.
- Fix Connections portal handling for unreadable sessions (`session_invalid`), expired POSTs (401 recovery HTML), rate-limited starts (429), mobile sign-in help text, and stale `not_signed_in`/`session_expired`/`confirm_invalid` guidance.
- Keep `uber-mcp admin freeze` resilient to unreadable named-account configs and route CLI recovery commands through the shared `Accounts` transport and budget.
- Accept equatorial latitude `0.0` in place resolution and preserve explicit past quote expiry timestamps.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.7; saved sessions, connection controls, and booking/audit storage formats are compatible. Quiesce money-tool calls and let all pending previews (ride and cancellation) expire (10 min) or clear them via freeze/unfreeze before starting 0.5.7 so 0.5.7 does not evaluate them under its older pricing-basis and fee comparisons.

## [0.5.7] - 2026-11-23

### Changed

- Move the two-image release to native `mainCandidate` promotion (6ff1ae0).

### Fixed

- Count unknown on-demand ride outcomes toward fare caps and fence cancellations (ee8aef9).
- Mark `uber_connect` read-only and idempotent (50efcc3).

### Upgrade

- Action required: none.
- Rollback: do not roll back to 0.5.6 while unresolved on-demand `action_attempts` holds or cancellation fences exist in `booking.json`; 0.5.6 ignores those records. Quiesce older money writers before upgrading and preserve current `booking.json` and `audit.jsonl` (see `docs/booking-recovery.md`).

## [0.5.6] - 2026-11-15

### Fixed

- Keep disposable-download Connections acceptance working with the Bash 3.2 shipped on macOS (089f5eb).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.5; saved sessions, connection controls and storage formats are unchanged.

## [0.5.5] - 2026-11-15

### Fixed

- Correct editable-checkout file URLs on Windows while retaining installed-package identity validation (924786c).
- Make synthetic CI report checks independent of GNU timeout and platform line endings (924786c).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.4; saved sessions, connection controls and storage formats are unchanged.

## [0.5.4] - 2026-11-15

### Fixed

- Use the compact mobile sign-in viewer with native keyboard input, Paste and Zoom (21653b9)
- Preserve editor focus while allowing touch controls to activate on WebKit (21653b9)

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.3; saved sessions, connection controls and storage formats are unchanged.

## [0.5.3] - 2026-11-14

### Fixed

- Require signin-window 0.8.2 for package installations, so an existing 0.8.1 installation cannot miss the mobile remote-input fix.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.2; saved sessions, account controls and storage formats are unchanged.

## [0.5.2] - 2026-11-14

### Security

- Refuse HTTP requests without the configured account header instead of selecting the default account. Explicit account selection, standalone HTTP without an account-header configuration and stdio behavior are preserved (#7).

### Fixed

- Use signin-window 0.8.2 so phone users can open a keyboard in remote sign-in and edits remain bound to their original viewer.

### Upgrade

- Action required: multi-user HTTP callers must send their configured account header on every request.
- Rollback: compatible with 0.5.1 saved state. Preserve credentials, owner/rider bindings, disconnect controls and booking history. Rolling back restores the previous missing-header fallback.

## [0.5.1] - 2026-11-12

_Publishes the changes since the last public release, which the public mirror did not receive; the package is unchanged from 0.5.0._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.0.

[0.5.1]: https://github.com/benjamin-di/uber-mcp-server/releases/tag/v0.5.1

## [0.5.0] - 2026-11-12

### Added

- Show the serving application's version and source revision on owned Connections pages and retained diagnostics; native MCP initialization advertises the same product version (#3)
- Stamp both images and published Python distributions before installation, and verify their installed identity offline before promotion (#3)

### Fixed

- Attribute connection-control initialization diagnostics without exposing rejected values (#3)
- Preserve enclosing Rich console capture and recording while attributing diagnostics, and retain native sink-error handling and terminal rendering (#3)

### Upgrade

- Action required: none for published images. Non-editable source installations require the publisher's build stamp; editable development runs report an unknown source revision.
- Rollback: compatible with 0.4.9; preserve current credentials, connection controls and owner bindings.

## [0.4.9] - 2026-11-03

### Fixed

- Use signin-window 0.7.1 for consistent mobile action widths, readable long labels and accessible checkbox touch targets.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.8; preserve current credentials, connection controls and owner bindings.

[0.4.9]: https://github.com/benjamin-di/uber-mcp-server/releases/tag/v0.4.9
