# Connections setup and recovery

Photos keeps three decisions separate: the identity provider authenticates the person,
the MCP gateway admits the client or workload, and Photos selects that person's existing
backend account. Connecting Google Photos grants no new agent access. A persistent service
keeps its own workload identity and reviewed API grants; it does not inherit a human's
browser session or gain a different backend account from a sign-in link.

## Permission boundaries

| Layer | Authority it grants | Authority it does not grant |
| --- | --- | --- |
| Human browser OIDC | Manage the existing account selected by a reviewed subject binding or signed account claim, after the required group check | Agent access, tool permissions or a choice of another backend account |
| MCP gateway, such as Agentgateway | Authenticate a human client or independent workload and enforce its issuer, audience, granted scopes and tool ceiling | A Google sign-in or permission to change the account's retained owner |
| Photos and its native custodian | Use the verified Google session for the bound account, within Photos' host/operation allowlists, budgets and write rules | Broader gateway grants or provider-wide revocation |

Pocket ID is one compatible OIDC provider, not a required application dependency.
The browser client uses the configured provider's standard OIDC claims. Give each
agent or persistent workload its own reviewed gateway grant; never copy browser
tokens, Google cookies or the private backend bearer into an agent configuration.
The gateway's backend credential and trusted account header are a privileged
application boundary, not credentials to hand to an MCP caller. Connecting or
reconnecting Google Photos does not widen an agent's audience, scopes or tool ceiling.

All hosted owners use the same policy. The original default account keeps its
native profile with the maintenance custodian; further accounts keep separate
profiles in the login service. Neither storage layout creates an administrator
exception to owner authentication. Local snapshot/CLI administration is privileged
operator access and must not be exposed as a public connection-login route.

Provider binding is a Google email address, not a stable numeric Google subject or
proof that Google and OIDC identify the same person. The default verifies its fixed
expected email; an unbound further account adopts the owner's chosen unused email
after native Photos verification. Later reconnects must retain it. The browser host
and its privileged operators are trusted with the profile, session and entered login
data; kiosk restrictions constrain the viewer, not the host operator's access.

### Logout, group changes and revocation

Browser claims, including group membership, are cached for at most 15 minutes
(or an earlier token expiry). Removing the required group blocks new browser
sign-ins; an existing session can remain valid until that bound. Signing out
ends that local browser session and viewer. Use Disconnect for an immediate
durable account-admission barrier; calls already admitted may finish while it drains.
Agentgateway grant/token revocation and Google session revocation are separate
operations with their own lifetimes. Neither browser consent nor a saved Google
session authorizes an agent, and the 15-minute browser bound is not an agent-token
or Google-revocation guarantee. Keep current controls when removing UI configuration
or rolling back; changing configuration cannot undo a disconnect.

The reusable `signin-window` package owns attempt presentation, expiry, cancellation and
viewer transport. Photos supplies its authorization callback, OIDC integration, account
bindings, connection status and storage. The library imports no identity provider or
Photos module and requires no permission-directory service.

## Configure before upgrading

When `PHOTOS_PUBLIC_URL`, `PHOTOS_LOGIN_URL` and `PHOTOS_LOGIN_TOKEN` enable browser sign-in,
all connections routes require OIDC. There is no compatibility mode that makes a link
sufficient authority. Every configured account, including the original default account,
can Connect and Reconnect through its authenticated owner's browser. The default keeps
its existing account id, expected Google address, snapshot format, state and budgets.
Configure `PHOTOS_DEFAULT_LOGIN_URL` for its existing native profile custodian; hosted
startup refuses a missing default backend. The reader still mounts snapshots read-only.

The default custodian runs `python -m photos_maintenance.human` in the same container,
hostname and PID namespace as native maintenance. Use the existing `PHOTOS_LOGIN_TOKEN`
and these fixed environment variables:

| Variable | Meaning |
| --- | --- |
| `PHOTOS_DEFAULT_LOGIN_ACCOUNT` | The existing default Photos account id |
| `PHOTOS_DEFAULT_LOGIN_ROOT` | Existing native maintenance root, for example `/maintenance` |
| `PHOTOS_DEFAULT_LOGIN_EXPORT_DIR` | Existing snapshot mount, for example `/export` |
| `PHOTOS_DEFAULT_LOGIN_CONTROLS_DIR` | The reader's initialized connections controls |
| `PHOTOS_DEFAULT_LOGIN_EXPECTED_EMAIL` | The immutable expected Google account |
| `PHOTOS_DEFAULT_LOGIN_PORT` | Private HTTP port, default `8081` |
| `NOVNC_DIR` | Installed noVNC assets, default `/opt/novnc` |

The human process needs browser/display runtime settings and the service token, without
credential-reader, provider, or planner secrets. `--recover-only` repairs an interrupted
commit and exits before opening an HTTP listener. Normal startup performs the same
recovery before `/health` becomes available. Run native maintenance and the human service
under a supervisor that drains both when either exits. Start the human service and wait
for its healthy response before starting native maintenance.

The native profile remains at `browser-profiles/google/default` below the same absolute
maintenance root, protected by its existing durable profile lease. A human attempt first
proves that profile is cold, then works on a private candidate. A wrong Google account,
cancelled attempt or failed verification leaves accepted credentials and accounting
untouched. After independent account verification and browser closure, a captured control
epoch fences promotion. A small journal makes profile exchange and the fixed snapshot
files recoverable even when the snapshot directory is a bind mount. A pending marker
blocks reader admission during the short commit and after an interrupted commit; startup
and each native keep repair it before warming. Unknown markers fail closed. Only a matching
captured hold can clear after successful manual proof; consumed attempts and cooldowns
remain. Disconnect stops server access and retains the default profile and saved snapshot.
Hold/generation accounting commits with the verified snapshot timestamp before the
COMMITTED marker. Recovery of a committed attempt only validates and removes journal
bookkeeping; it never changes a newer hold, control epoch, snapshot or verification time.

1. Register a public OIDC client, require S256 PKCE, and register exactly
   `https://example.com/photos/connections/callback`. Keep consent enabled. No client
   secret, provider administrator credential or new per-agent secret belongs in Photos.
2. Set `PHOTOS_CONNECTIONS_ISSUER`, `PHOTOS_CONNECTIONS_CLIENT_ID` and
   `PHOTOS_CONNECTIONS_GROUP`. The provider must include the allowed group in its signed
   ID token and publish normal OIDC discovery and signing keys.
3. Select either a private `PHOTOS_CONNECTIONS_BINDINGS_FILE`, such as
   `{"example-subject":"second"}`, or `PHOTOS_CONNECTIONS_ACCOUNT_CLAIM`, naming an
   administrator-controlled signed claim containing `second`. Bind by immutable subject
   or reviewed account alias, never email, display name, browser input or a forwarded
   header. Account aliases must be unique per owner. Provision the Photos account and
   budget shares through the existing admin command first.
4. Forward only the exact browser routes listed in the README. Strip Authorization and
   untrusted identity headers, preserve cookies and Origin, and keep MCP admission in
   force for `/mcp`. `PHOTOS_PUBLIC_URL` is fixed configuration, never inferred from a
   request Host header. Apply the public path prefix consistently to links and callback.
5. Keep `PHOTOS_STATE_DIR` writable by the application. Its private `connections/` folder
   (0700) and SQLite database (0600) retain opaque browser sessions and disconnect barriers.
   Never restore an older database to undo a disconnect. A missing database expires browser
   sessions; restore disconnect controls only from current trusted state.

OIDC uses the standard Authlib client: authorization code, state, S256 PKCE, nonce,
signature, issuer, audience and expiry checks. The cookie contains a random opaque reference;
the database temporarily holds protocol state and subsequently only minimal verified
identity claims. Access, refresh and ID tokens are discarded after callback validation.
Rollback must retain the owner-aware application checks and current connections database.
Earlier capability-only releases ignore disconnect barriers and are not a compatible
rollback after activation. Do not restore an older state database.

Browser sessions last at most 15 minutes, including group membership; a new sign-in obtains
current claims. Signing out deletes that browser session and terminates its viewer.
The first explicit connection-management action pins its issuer and subject as the
owner. Reassigning an account alias cannot silently transfer that ownership.

For an independently maintained default account, give its maintenance worker the
existing private controls directory in `PHOTOS_CONNECTIONS_CONTROLS_DIR` and the
matching account id in `PHOTOS_CONNECTIONS_ACCOUNT`. Missing configured control
state fails closed. Admission and final snapshot publication consult the same
durable revision, including a disconnect followed by a new owner start during verification.
Intentional suspension reports `disconnected` and `maintenance_suspended`, rather
than claiming a fresh publication or paging for an unused ageing snapshot.

## What users see

My connections shows only the signed-in person's account, a masked Google address and the
last verified snapshot time. “Connection saved” means a verified snapshot exists; Google
access is checked when the application uses it. A keeper error says attention is needed
without discarding the accepted snapshot. A confirmed provider sign-out asks for sign-in.

An active attempt offers Continue sign-in. Reloading resumes the same window. During
saving, the live view disappears, the connection screen refreshes, and closing the page
cannot lose the result. A restart recovers status from the existing snapshot and keeper
record; temporary viewers and attempts are recreated when needed.

A wrong-account reconnect never changes the bound Google address or accepted snapshot.
The default custodian discards only its candidate; its accepted native profile remains.
The further-account service removes a refused login profile. Disconnect immediately
blocks new account calls and invalidates old attempt revisions, then drains running
work. It deletes a further account's login profile/snapshot and retains the default
account's native profile/snapshot behind the barrier. If confirmation fails,
access stays blocked across restart until Retry disconnect succeeds. Removing the browser
UI configuration also retains that block; account controls apply independently of OIDC.
Signing out of the UI does not disconnect Google. Disconnecting this server does not sign out other devices.
Calls already admitted before disconnect may finish; the application is not a provider-wide
revocation service.

## Verification

`make check` covers the OIDC protocol, callback replay, cross-browser state, forwarded
links, header spoofing, exact Origin, wrong-owner access, expiry and durable disconnect.
The library independently tests authorization changes during an existing WebSocket.

`scripts/connections_e2e.py` runs a checksum-pinned disposable Pocket ID release, enrolls
three synthetic users with browser-generated virtual passkeys, performs real OIDC exchanges
and exercises the real Photos window, Xvfb, x11vnc and noVNC. It checks desktop/mobile
layout, owner isolation, reload, page closure during saving, snapshot restart recovery and
disconnect, including recovery of an expired original default account through its native
custodian. The Google page and session are synthetic, with all browser requests intercepted;
no real Google account or photo library is used. Set `POCKET_ID_BIN`, `PHOTOS_TEST_CHROME`
and `PHOTOS_LOGIN_NOVNC` to installed test dependencies. The CI job contains the pinned
binary and viewer checksums. `scripts/login_e2e.py` additionally checks paste, managed browser
restrictions and MCP account selection in the candidate login image.


An assistant's `photos_connect` request returns the permanent Connections URL
with `expires_at: null`. It does not create or replace an attempt, resume access,
or pause maintenance. Only the authenticated owner's Connect action creates an
expiring sign-in window. Callers must accept a null expiry for this entry URL.
The same entry remains available after disconnect so the owner can recover.

Disconnect first closes admission durably, then drains already admitted provider
operations before confirming completion. A timeout leaves access blocked and
shows Retry disconnect. Staged uploads recheck admission and account ownership
at publication. Keep the controls directory and its sibling initialization
marker together; losing the controls database fails closed.


Set `PHOTOS_CONNECTIONS_PUBLIC_URL` to a dedicated browser origin when account
management is hosted separately. `PHOTOS_PUBLIC_URL` continues to own upload and
download links; it need not share the browser origin. Register the exact
`/connections/callback` on the browser origin with the identity provider.


Connection forms include the current opaque control revision. The application
checks it while holding a per-account lifecycle lock, so duplicate or delayed
Start/Disconnect requests cannot override a newer owner action. Upload/download
capabilities retain their issuing revision and never revive after reconnect.
Disconnect drains a download through its actual response body transmission.
First account adoption is serialized across all accounts, including keeper
recovery, so concurrent sign-ins cannot claim the same Google identity twice.

## Remaining project work

Implementation and portable qualification cover owner-bound OIDC, default/further
custody, staged verification, generation-bound health, crash recovery, disconnect
and unchanged credential-attempt accounting. The remaining release checks are:

- Accept genuine tagged source checks, image smoke and source/version/digest provenance.
- Exercise the installed reader and custodian together, including anonymous and
  wrong-owner requests, group expiry, interrupted save/recovery and retained disconnect.
- Review the exact public export and qualify its standalone release artifacts.

Runtime destinations, account mappings, rollout order and production acceptance
records belong in the operator's private infrastructure plan.

## Required controls for hosted adoption

Set `CONNECTIONS_REQUIRE_CONTROLS=1` for hosted readers and credential writers.
Missing control records then refuse access, including after a state directory
is replaced. Existing standalone behavior remains available for unadopted state;
saved initialization evidence still prevents recreating a lost adopted record.

Before starting matching control-aware roles, explicitly initialize each reviewed
account in its existing private state, using the same runtime user and paths:

```sh
python -m photos_mcp.connections_auth --initialize --folder <configured-controls-folder>
```

This local operation makes no provider request. It preserves existing valid
controls and refuses a missing record beside retained initialization evidence.
It must not run automatically to repair a damaged or missing adopted volume.
Keep markers with current controls, bindings, credentials and accounting; check
actual mounted state before startup and recover on disposable copies. An older
backup or an image that ignores controls is not a safe way to clear a disconnect.
