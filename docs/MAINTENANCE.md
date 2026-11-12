# Optional account maintenance

The reader and human sign-in window work without credential automation.
`photos-mcp-maintenance` is a separate, explicit Linux worker for one operator-owned
Google Photos account. It starts no scheduler. Run it in the login-browser image
with a private writable profile/state root, the existing snapshot export directory
and narrowly scoped credential access. Do not give these mounts or credentials
to a reader container.

Credential automation is optional; hosted default-account owner recovery is available
without it. The owner starts Connect or Reconnect from the authenticated My connections
page, including when maintenance has positively detected a signed-out profile. Human
OIDC authorizes that account-management action only: MCP client/workload permission
still comes from the gateway, and native Google authority comes from the verified
session. Neither manual recovery nor credential automation widens an agent's grant.
See [permission boundaries and remaining project checks](CONNECTIONS.md).

Before running maintenance or its human HTTP service, provision the private
`/maintenance` root with scope `maintenance` following
[first-time deployment setup](DEPLOYMENT.md#first-time-http-setup). The root must
be owned by the application UID with mode `0700`; its four deployment records
must have mode `0600`. Keep existing records, holds and in-flight tickets when
upgrading. Creating an empty directory alone is insufficient. A predecessor
without this protocol needs a separately qualified adoption procedure.

```sh
photos-mcp-maintenance keep \
  --root /maintenance --export-dir /exports/default \
  --expected-email owner@example.com
```

This warms and independently verifies the existing session. Credential sign-in
requires `--allow-credential-mint` and a configured `PHOTOS_MAINTENANCE_CREDENTIAL_REFERENCE`
(`op://Example/Account`), an installed 1Password CLI and its explicitly supplied
`OP_SERVICE_ACCOUNT_TOKEN`. Keep the account value and reference in private
runtime configuration; pass no password in arguments. The images do not bundle
the proprietary 1Password CLI. Operators supply their separately installed CLI
at runtime. An optional timezone is set with `--timezone` or `PHOTOS_MAINTENANCE_TIMEZONE`;
UTC is the default.

A failed or inconclusive warm-up never submits credentials. Only positive
signed-out evidence permits one sign-in, then a new independent verification.
The rolling 24-hour attempt is durably reserved before dispatch. Interrupted
sign-ins leave a hold. Site pushback retains escalating cooldowns; account
mismatch requires an operator. A completed sign-in alone never proves access.

`photos-mcp-maintenance status --root /maintenance` reads stored health without
opening a browser or reading credentials. `acknowledge --hold-id HOLD` clears
only the exact current hold while holding the profile lease. It never refunds
an attempt. Commands emit closed JSON outcomes and fail with a nonzero status
when a hold or stale snapshot needs attention.

## Upgrades and ownership

Keep the whole root private. It holds `identity-targets/google.json`,
`session-keeper/google.json`, `browser-profiles/google/default` and `locks/`.
The snapshot remains in the application's documented format. Profiles and
snapshots carry full account authority.

Stop and wait for the previous writer before adopting its current state. Retain
holds, cooldowns, generation watermarks and consumed attempts. Use one writer
per account, and the same absolute profile and lease paths in every process
sharing it. Never copy a profile while Chromium is running. Rehearse adoption
and rollback with synthetic state; rollback must preserve the latest accounting.
The human window's own further-account keeper remains independent and never
submits credentials.

Hosted default-account recovery runs `photos_maintenance.human` alongside this worker
in the same custodian container and PID namespace. It holds the same profile lease,
verifies a cold candidate, and promotes only after independent manual proof. Both
startup and each keep recover its journal before warming; the shared pending marker
keeps readers closed until profile and snapshot are consistent. See
[Connections setup](CONNECTIONS.md) for the fixed private HTTP contract. Manual recovery
does not refund credential attempts or reset cooldowns, and clears only the matching
hold captured when that human attempt began.

## Verification

Run `make check` for policy, state, account and snapshot regressions. Release
acceptance also exercises the human window against a synthetic site in the
installed browser image. No CI check reaches a real account.

## Shared maintenance contracts

The existing state JSON, hold ids, cooldowns, generation watermarks and rolling
attempt ledger remain compatible. Durable state operations now use
signin-window's supported `MaintenanceStateStore`; this application still owns
its reason enums, verification, snapshot schema and cooldown policy. No state
reset, profile copy or budget refund accompanies this upgrade.

The default credential reader remains 1Password. To use another store, set
`PHOTOS_MAINTENANCE_CREDENTIAL_READER=example_backend:create_reader` to an
explicit trusted factory implementing signin-window's `CredentialReader`.
Configure an opaque reference with `PHOTOS_MAINTENANCE_CREDENTIAL_REFERENCE`.
The backend's `available` property must not read secrets or contact a provider.
Only the requested username, password or generated TOTP is resolved just in time.
Origin checks, error redaction, durable budgets and holds apply to all backends.
A selected reader's failure never falls back to the ambient 1Password identity.


## Publication repair

A mismatched, missing or unverifiable native publication journal reports
recovery_required with a nonzero exit status and no browser or credential I/O.
Agents remain refused. Normal recovery cannot clear this held outcome.

With the existing controls and account configured in PHOTOS_CONNECTIONS_CONTROLS_DIR
and PHOTOS_CONNECTIONS_ACCOUNT, run in the custodian's existing PID namespace:

```sh
photos-mcp-maintenance recover-publication \
  --root /maintenance --export-dir /exports/default
```

This command participates in the deployment gate, takes the native profile lease
and account lifecycle lock, revokes old publication epochs, and drains admitted
operations. It durably retains the marker, recovery hold and entire staging/journal
directory under private publication-quarantine/<operation-id>/, then leaves the
account Disconnected. It never accepts the retained profile/snapshot, restores an
old grant, clears a credential hold, or refunds a consumed attempt. A failed repair
keeps access closed; rerunning it safely retains any remaining evidence.
The recovery marker remains in place until the staging directory and its parent
directories are synced in quarantine. Repair copies both markers first, removes
the pending marker after that move, and removes the recovery marker last. An
interrupted repair cannot turn uncertain evidence back into ordinary recovery.

Inspect quarantined evidence only as privileged local state: it can contain full
account authority. Keep it private and outside source control. Do not delete a
marker by hand or restore an older controls database. After repair, the owner must
start a new sign-in and independently verify a fresh snapshot before access returns.

Storage compatibility alone does not make an older binary a safe rollback. The
rollback target must preserve suspended reconnects, keeper admission and recovery
holds. Otherwise keep the affected writers and Start disabled, even after repair
has removed the recovery marker. Preserve the current controls, epochs and holds.
