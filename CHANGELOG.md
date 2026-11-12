# Changelog

All notable changes are recorded here. This project follows
Semantic Versioning.

## [0.14.1] - 2026-11-12

_Publishes the changes since the last public release, which the public mirror did not receive; the package is unchanged from 0.14.0._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.14.0.

[0.14.1]: https://github.com/benjamin-di/google-photos-mcp/releases/tag/v0.14.1

## [0.14.0] - 2026-11-12

### Added

- Identify the Photos build on owned pages and retained diagnostics, and forward it to the shared sign-in and connection pages using signin-window 0.8 (#3)

### Fixed

- Keep a disconnected Photos account closed until a new sign-in publishes (#4)
- Retain fixed startup guidance and preserve the native process role alongside product identity (#3)
- Preserve enclosing Rich console capture and recording while attributing diagnostics, and retain native sink-error handling and terminal rendering (#3)

### Upgrade

- Action required: none.
- Rollback: compatible with 0.13.1; preserve current credentials, connection controls and owner bindings.

## [0.13.1] - 2026-11-03

### Fixed

- Use signin-window 0.7.1 for consistent mobile action widths, readable long labels and accessible checkbox touch targets.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.13.0; preserve current credentials, connection controls and owner bindings.

[0.13.1]: https://github.com/benjamin-di/google-photos-mcp/releases/tag/v0.13.1

## [0.13.0] - 2026-11-02

### Added

- Add optional `expected_connection` to `photos_list` album reads and `photos_album_remove`, with opaque `connection_epoch` tokens for supported managed accounts ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).
- Add optional `expected_connection` to `photos_get` and `photos_download` so bound metadata reads and retained JPEG copies come from the captured connection, returning the same `connection_epoch` on success ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).
- Report removal invocation `mutation.phase` and `mutation.definitely_unsent` so clients distinguish pre-send refusals from attempted writes and failed read-back ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).

### Fixed

- Fence conditional album operations against replaced, pending or unaccepted credential publications, including replacements with equal snapshot generation numbers ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).
- Reject malformed conditional album pages before returning binding or coverage evidence, and forbid automatic removal retries after an attempted write ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).
- Reject missing, mismatched or conflicting provider media IDs in conditional `photos_get`, `photos_download` and album `details=true` responses before mixing metadata, enriching or issuing a usable link ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).
- Bypass the shared thumbnail cache for conditional image reads so a replaced connection cannot serve or populate cached bytes ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.12...v0.13.0)).

### Upgrade

- Action required: existing unbound clients need no changes; keep conditional automation disabled until all snapshot publishers and readers implement [verified publication custody](docs/conditional-album-cleanup.md), a normal publication establishes an accepted receipt, and the consumer retains its original binding. Never obtain a new token to repair an old capture.
- Rollback: compatible with 0.12.12 while conditional automation is disabled; disable it before reverting any publisher or reader, preserve controls, holds and retained work, and never replay uncertain removals.

## [0.12.12] - 2026-10-31

_No runtime changes from 0.12.11; add maintained release notes for publication. Earlier releases were not recorded here; see their tags._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.12.11.

## [0.12.11] - 2026-10-31

### Changed

- Explain connection state, next steps and disconnect effects in plain language; retain browser-profile promises only where profiles are actually kept ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.8...v0.12.12)).

### Fixed

- Preserve unsent form choices and keyboard focus during status updates, and keep sign-in recovery on the configured login route ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.8...v0.12.12)).
- Document application-owned private deployment roots and the required records before HTTP startup, while preserving existing records during upgrades ([public changes](https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.8...v0.12.12)).

### Upgrade

- Action required: follow [deployment provisioning and handoff](docs/DEPLOYMENT.md) before first HTTP startup or replacement; keep existing account data, controls, holds and work tickets intact.
- Rollback: retain deployment-protocol-aware readers and writers; never use an older reader that ignores holds or restore records independently of current sessions and revocations.

[0.12.12]: https://github.com/benjamin-di/google-photos-mcp/releases/tag/v0.12.12
[0.12.11]: https://github.com/benjamin-di/google-photos-mcp/compare/v0.12.8...v0.12.12