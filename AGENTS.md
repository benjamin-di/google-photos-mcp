# Development

Run `make bootstrap`, then `make check`. Bootstrap installs the locked dependencies (all
extras) into `.venv` with uv. `make check` runs ruff, the format check, the
lock-consistency check (`requirements-runtime.lock` must equal `uv export` of `uv.lock`;
`make lock` after a dependency change) and pytest, and keeps a source-bound receipt under
`.artifacts/`.

Tests never reach Google: transport tests script HTTP responses,
`tests/test_curl_sender.py` uses a local server, and `photos-mcp-session`'s browser
commands are tested through their pure parts. `tests/test_safety.py` is the read-only
boundary: a new RPC id, host or payload builder is a deliberate change there, never a side
effect.

The session snapshot format (README "Session snapshot", `src/photos_mcp/session.py`,
`tests/fixtures/session/`) is an interface other tools write: keep it backward compatible
and bump `schema` for an incompatible change.

Never point a development run at a real account's session: the snapshot carries full
Google account authority.

## Everything in this tree is published

Code, comments, docs, fixtures and commit messages must not name people, accounts, e-mail
addresses, hosts, networks, internal services or deployments, and must not carry real data
from a photo library. Recorded fixtures go through the capture anonymiser and
`tests/test_fixture_privacy.py`; samples use `example.com` addresses and `fake-` values.
Describe behaviour, not where it runs.
