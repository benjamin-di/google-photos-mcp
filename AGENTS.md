# Development

Run `make bootstrap`, then `make check`. Bootstrap installs the locked dependencies (all
extras) into `.venv` with uv. `make check` runs ruff, the format check, the
lock-consistency check (`requirements-runtime.lock` must equal `uv export` of `uv.lock`;
`make lock` after a dependency change) and pytest, and keeps a source-bound receipt under
`.artifacts/`.

Tests never reach Google: transport tests script HTTP responses,
`tests/test_curl_sender.py` uses a local server, and `photos-mcp-session`'s browser
commands are tested through their pure parts. `tests/test_safety.py` is the boundary of what
photos-mcp may do to a library (reads, and album changes, uploads and the trash through `GoogleTransport.write`
and `upload_bytes` only, never emptying the trash; the one non-Google request is the guarded link fetch in `fetch.py`):
a new RPC id, host or payload builder is a deliberate change there, never a side effect.
`connections_auth.py` owns the separate OIDC control plane through Authlib and a fixed
configured issuer; it never adds identity-provider calls to the Google data transport.

The session snapshot format (README "Session snapshot", `src/photos_mcp/session.py`,
`tests/fixtures/session/`) is an interface other tools write: keep it backward compatible
and bump `schema` for an incompatible change.

Never point a development run at a real account's session: the snapshot carries full
Google account authority.

The login service (`src/photos_login`) shows Google's sign-in page to a person and automates
nothing on it: no click, keystroke or fill exists there (`tests/test_login_service.py` checks
it statically). It never returns a session over its API. It holds every further account's
profile and snapshot, so its window must stay locked down: signin-window's locked-down browser
(`signin_window.browser`: kiosk mode, its managed Chromium policy; DevTools stays
allowed in the policy because `DeveloperToolsAvailability=2` also closes the pipe the window is
driven through), the top-level page allowlist, and the live view only in state `waiting`. `scripts/login_e2e.py` checks the policy in the image. The windows' service, the sign-in
links' relay and the paste add-on come from [signin-window](https://github.com/benjamin-di/signin-window): fix them
there; this repository holds Google's window, the runner and what a sign-in binds.
Browser-level behaviour is covered by `scripts/login_e2e.py`, which the release runs in the
login image (a stand-in page, never Google).

## Everything in this tree is published

Code, comments, docs, fixtures and commit messages must not name people, accounts, e-mail
addresses, hosts, networks, internal services or deployments, and must not carry real data
from a photo library. Recorded fixtures go through the capture anonymiser and
`tests/test_fixture_privacy.py`; samples use `example.com` addresses and `fake-` values.
Describe behaviour, not where it runs.

## Optional maintenance boundary

`src/photos_maintenance` owns explicit Linux credential maintenance, independently of
the human window and reader. Preserve positive signed-out evidence, attempt
accounting, durable holds, account checks and snapshot compatibility. Generic
mechanics belong in signin-window; runtime credentials and planner identities
belong in deployment configuration. See [Account maintenance](docs/MAINTENANCE.md).
