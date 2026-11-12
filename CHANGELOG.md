# Changelog

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
