"""More than one Google account on one server (README "Accounts").

The default account is the server's original single-account setup: the snapshot in ``PHOTOS_SESSION_DIR`` (written
by whatever the operator runs), ``GOOGLE_PHOTOS_EXPECTED_EMAIL``, and its state right in ``PHOTOS_STATE_DIR``. Every
further account has ``PHOTOS_STATE_DIR/accounts/<id>/account.json`` (its Google address once it is bound, and its
share of the daily budgets), its own state, thumbnail cache and cursors there, and its snapshot in
``PHOTOS_ACCOUNT_SESSIONS_DIR/<id>``, which the login service (``photos_login``) writes after the person signed in
through a sign-in link and keeps fresh.

Budgets are split, not added: ``PHOTOS_DAILY_RPC_BUDGET`` and ``PHOTOS_DAILY_THUMB_BUDGET`` stay the server's total
for the day, each further account has its own share, and the default account gets what is left. Google's pacing and
the breaker after pushback stay shared: Google sees one client, whichever account is calling.

An account's Google address is bound the first time someone signs in through its link (or given when the account is
added); a sign-in as another Google account, or as one another account here already has, is refused and never
replaces it.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from signin_window.state import exclusive_lock

from .config import ACCOUNT_ID, Settings
from .errors import PhotosError

ACCOUNTS_DIR = "accounts"
ACCOUNT_FILE = "account.json"
# Written by the login service in each account's snapshot directory: what its browser last saw.
KEEPER_FILE = "keeper.json"


class AccountConfigError(Exception):
    """A bad account id, address or budget share given to the admin commands."""


@dataclass(frozen=True)
class Account:
    id: str
    email: str | None  # the Google address it serves; None until the first sign-in binds one
    rpc_budget: int
    thumb_budget: int
    state_dir: Path
    session_dir: Path
    linked: bool  # further-account storage/binding; the default retains its existing native layout
    created_at: int = 0  # tells a re-added account from the removed one of the same name


def valid_id(account_id: object) -> bool:
    return isinstance(account_id, str) and bool(ACCOUNT_ID.match(account_id))


def check_id(account_id: str) -> str:
    if not valid_id(account_id):
        raise AccountConfigError("an account id is 1-32 lower-case letters, digits or dashes, starting with a letter")
    return account_id


def _folder(settings: Settings, account_id: str) -> Path:
    if not valid_id(account_id) or account_id == settings.default_account:
        raise AccountConfigError("not a separate account")
    return settings.state_dir / ACCOUNTS_DIR / account_id


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _email(raw: object) -> str | None:
    email = raw.strip().lower() if isinstance(raw, str) else ""
    return email if "@" in email and len(email) <= 254 and not any(c.isspace() for c in email) else None


def _share(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def configured(settings: Settings) -> list[str]:
    """The further accounts, by name (the default account is always there and not listed)."""
    root = settings.state_dir / ACCOUNTS_DIR
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if valid_id(p.name) and p.name != settings.default_account and (p / ACCOUNT_FILE).is_file()
    )


def _record(settings: Settings, account_id: str) -> dict[str, Any] | None:
    data = _read(settings.state_dir / ACCOUNTS_DIR / account_id / ACCOUNT_FILE)
    if data is None:
        return None
    rpc, thumb = _share(data.get("daily_rpc_budget")), _share(data.get("daily_thumb_budget"))
    email = data.get("email")
    if rpc is None or thumb is None or (email is not None and _email(email) is None):
        return None  # unreadable: the account is unusable until fixed, never served with other values
    return data


def _raw_shares(settings: Settings) -> dict[str, tuple[int, int]]:
    out = {}
    for account_id in configured(settings):
        data = _record(settings, account_id)
        if data is not None:
            out[account_id] = (data["daily_rpc_budget"], data["daily_thumb_budget"])
    return out


def shares(settings: Settings) -> dict[str, tuple[int, int]]:
    """Each further account's (rpc, thumb) budget share in force; unreadable accounts count as nothing. If the
    totals were lowered below the shares, every share shrinks in proportion, so the server never spends more than
    its totals."""
    raw = _raw_shares(settings)
    rpc_sum, thumb_sum = sum(r for r, _ in raw.values()), sum(t for _, t in raw.values())
    rpc_scale = min(1.0, settings.daily_rpc_budget / rpc_sum) if rpc_sum else 1.0
    thumb_scale = min(1.0, settings.daily_thumb_budget / thumb_sum) if thumb_sum else 1.0
    return {k: (int(r * rpc_scale), int(t * thumb_scale)) for k, (r, t) in raw.items()}


def default(settings: Settings, others: dict[str, tuple[int, int]] | None = None) -> Account:
    """The default account: what the further accounts' shares leave of the daily budgets."""
    others = shares(settings) if others is None else others
    return Account(
        id=settings.default_account,
        email=settings.expected_email,
        rpc_budget=max(0, settings.daily_rpc_budget - sum(rpc for rpc, _ in others.values())),
        thumb_budget=max(0, settings.daily_thumb_budget - sum(thumb for _, thumb in others.values())),
        state_dir=settings.state_dir,
        session_dir=settings.session_dir,
        linked=False,
    )


def load(settings: Settings, account_id: str) -> Account:
    """A configured account; anything else is ``account_not_connected``, never another account's."""
    if account_id == settings.default_account:
        return default(settings)
    if not valid_id(account_id):
        raise PhotosError("account_not_connected", log_detail="invalid account id")
    data = _record(settings, account_id)
    if data is None:
        raise PhotosError("account_not_connected", log_detail="account not configured or unreadable")
    rpc, thumb = shares(settings).get(account_id, (data["daily_rpc_budget"], data["daily_thumb_budget"]))
    created = data.get("created_at")
    return Account(
        id=account_id,
        email=_email(data.get("email")),
        rpc_budget=rpc,
        thumb_budget=thumb,
        state_dir=settings.state_dir / ACCOUNTS_DIR / account_id,
        session_dir=settings.account_sessions_dir / account_id,
        linked=True,
        created_at=created if isinstance(created, int) else 0,
    )


def taken_emails(settings: Settings, except_id: str) -> list[str]:
    """The Google addresses other accounts here serve: a sign-in for ``except_id`` must not take one of them."""
    out = {settings.expected_email} if except_id != settings.default_account else set()
    for account_id in configured(settings):
        if account_id != except_id and (data := _record(settings, account_id)) and (email := _email(data.get("email"))):
            out.add(email)
    return sorted(out)


def _check_shares(settings: Settings, account_id: str, rpc: int, thumb: int) -> None:
    if rpc < 0 or thumb < 0:
        raise AccountConfigError("budget shares cannot be negative")
    others = {k: v for k, v in _raw_shares(settings).items() if k != account_id}
    rpc_left = settings.daily_rpc_budget - sum(r for r, _ in others.values())
    thumb_left = settings.daily_thumb_budget - sum(t for _, t in others.values())
    if rpc > rpc_left or thumb > thumb_left:
        raise AccountConfigError(
            f"the shares exceed the daily totals: at most {rpc_left} RPCs and {thumb_left} thumbnails are left "
            "(PHOTOS_DAILY_RPC_BUDGET and PHOTOS_DAILY_THUMB_BUDGET are the server's totals)"
        )


def _serialized_update(operation):
    """Account uniqueness and budget shares must be checked and written together."""

    @functools.wraps(operation)
    def update(settings, *args, **kwargs):
        with exclusive_lock(settings.state_dir / "accounts.lock", timeout_seconds=30):
            return operation(settings, *args, **kwargs)

    return update


@_serialized_update
def add(settings: Settings, account_id: str, *, rpc_budget: int, thumb_budget: int, email: str | None = None) -> Path:
    """Create a further account with its budget shares (taken from the default account's). Refuses an existing one,
    shares the totals cannot cover and an address another account serves."""
    folder = _folder(settings, check_id(account_id))
    if (folder / ACCOUNT_FILE).exists():
        raise AccountConfigError(f"account {account_id} exists (change its shares with `admin account budget`)")
    if folder.exists():
        raise AccountConfigError(f"{folder} is left over from a removed account: delete it first")
    bound = None
    if email is not None:
        bound = _email(email)
        if bound is None:
            raise AccountConfigError("--email must be a Google account's address")
        if bound in taken_emails(settings, account_id):
            raise AccountConfigError("another account on this server already serves that Google account")
    _check_shares(settings, account_id, rpc_budget, thumb_budget)
    record = {
        "created_at": time.time_ns(),
        "email": bound,
        "daily_rpc_budget": rpc_budget,
        "daily_thumb_budget": thumb_budget,
    }
    _write(folder / ACCOUNT_FILE, record)
    return folder


@_serialized_update
def set_budget(settings: Settings, account_id: str, *, rpc_budget: int, thumb_budget: int) -> None:
    """Replace an account's shares; the server applies them (and the default account's rest) on its next call."""
    folder = _folder(settings, check_id(account_id))
    data = _read(folder / ACCOUNT_FILE)
    if data is None:
        raise AccountConfigError(f"no account {account_id}")
    _check_shares(settings, account_id, rpc_budget, thumb_budget)
    _write(folder / ACCOUNT_FILE, {**data, "daily_rpc_budget": rpc_budget, "daily_thumb_budget": thumb_budget})


@_serialized_update
def bind_email(settings: Settings, account_id: str, email: str) -> bool:
    """Bind the account to the Google address its first sign-in proved. True when it is (now) that address."""
    folder = _folder(settings, account_id)
    data = _record(settings, account_id)
    bound = _email(email)
    if data is None or bound is None:
        return False
    current = _email(data.get("email"))
    if current is not None:
        return current == bound
    if bound in taken_emails(settings, account_id):
        return False
    _write(folder / ACCOUNT_FILE, {**data, "email": bound})
    return True


def bind_from_keeper(settings: Settings, account: Account) -> bool:
    """Bind an unbound account to the address the login service recorded for it (keeper.json, written only after it
    checked that sign-in against the same rules), when the person's page did not stay open to see it through.
    True when the account is now bound."""
    if not account.linked or account.email is not None:
        return False
    data = _read(account.session_dir / KEEPER_FILE)
    if not data or data.get("state") != "signed_in" or (email := _email(data.get("email"))) is None:
        return False
    return bind_email(settings, account.id, email)


@_serialized_update
def remove(settings: Settings, account_id: str) -> None:
    """Delete an account's record and state (cache and cursors included). Its browser profile and snapshot belong to
    the login service, which the admin command asks to delete them."""
    folder = _folder(settings, check_id(account_id))
    if not (folder / ACCOUNT_FILE).exists():
        raise AccountConfigError(f"no account {account_id}")
    (folder / ACCOUNT_FILE).unlink()  # first: the server stops serving it at once
    shutil.rmtree(folder, ignore_errors=True)


def keeper_status(account: Account) -> dict[str, Any] | None:
    """What this account's custodian last observed, bound to its snapshot generation."""
    data = _read(account.session_dir / KEEPER_FILE)
    if data is None:
        return None
    checked = data.get("checked_at")
    generation = data.get("generation")
    return {
        "state": data.get("state") if isinstance(data.get("state"), str) else None,
        "generation": generation if type(generation) is int and generation >= 0 else None,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked))
        if isinstance(checked, int | float)
        else None,
        "error": data.get("error") if isinstance(data.get("error"), str) else None,
    }


def mask_email(email: str | None) -> str | None:
    """``s•••@example.com``: enough for a person to recognise their own account on a page anyone with the link sees."""
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"
