# Contributing

Issues and pull requests are welcome, especially when Google changes the Photos web app and a
tool starts failing.

## Before you open a pull request

```sh
make bootstrap   # uv sync --locked, all extras
make check       # ruff, format, lock consistency, tests (offline)
```

[AGENTS.md](AGENTS.md) has the rules: tests never reach Google, `tests/test_safety.py` is the
boundary of what it may change (album changes only), the session snapshot format stays backward compatible, and nothing from a
real photo library or account goes into code, docs or fixtures (recorded fixtures pass the
capture anonymiser and `tests/test_fixture_privacy.py`). Never point a development run at a
real account's session.

Security problems: see [SECURITY.md](SECURITY.md) instead of opening an issue.

## How changes land

Each release of this repository is exported from a private working repository as one
`Release X.Y.Z` commit, so pull requests are not merged here: the next release would replace
them. When a change fits, the maintainer applies it there, credits you in the release notes
and closes the pull request with a link to the release that contains it. Issues are welcome;
answers are best effort.
