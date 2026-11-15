# Changelog

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
