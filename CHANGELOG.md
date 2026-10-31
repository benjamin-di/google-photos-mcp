# Changelog

All notable changes are recorded here. This project follows
Semantic Versioning.

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
