"""``photos-mcp admin``: the operator's commands for accounts (README "Accounts"). They work on the state directory
the server uses, so run them where it runs (``docker exec <container> photos-mcp admin ...``); the server applies a
change on its next call.

  photos-mcp admin account list
  photos-mcp admin account add ID --rpc-budget N --thumb-budget N [--email ADDRESS]
  photos-mcp admin account budget ID --rpc-budget N --thumb-budget N
  photos-mcp admin account remove ID
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import accounts
from .config import ConfigError, Settings
from .login_client import LoginClient, LoginUnavailable


def _list(settings: Settings) -> list[dict[str, Any]]:
    from .server import Accounts

    registry = Accounts.from_settings(settings)
    out = []
    for account_id in [settings.default_account, *accounts.configured(settings)]:
        try:
            service = registry.get(account_id)
        except Exception as exc:  # an unreadable account.json: say so, never guess
            out.append({"id": account_id, "error": getattr(exc, "code", type(exc).__name__)})
            continue
        account, status = service.account, service.status()
        assert account is not None
        out.append(
            {
                "id": account.id,
                "default": not account.linked,
                "email": account.email,
                "daily_rpc_budget": account.rpc_budget,
                "daily_thumb_budget": account.thumb_budget,
                "session": status["session"]["state"],
                "keeper": status.get("keeper"),
            }
        )
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="photos-mcp admin", description="Accounts on this server.")
    groups = parser.add_subparsers(dest="group", required=True)
    account = groups.add_parser("account", help="the further accounts beside the default one").add_subparsers(
        dest="action", required=True
    )
    account.add_parser("list", help="every account: address, budget shares, session and keeper state")
    for name, text in (("add", "add an account"), ("budget", "change an account's budget shares")):
        action = account.add_parser(name, help=text)
        action.add_argument("id", help="1-32 lower-case letters, digits or dashes (what the proxy's header says)")
        action.add_argument("--rpc-budget", type=int, required=True, help="its share of PHOTOS_DAILY_RPC_BUDGET")
        action.add_argument("--thumb-budget", type=int, required=True, help="its share of PHOTOS_DAILY_THUMB_BUDGET")
        if name == "add":
            action.add_argument("--email", help="bind it to this Google address now (else the first sign-in does)")
    remove = account.add_parser("remove", help="delete an account, its state, browser profile and snapshot")
    remove.add_argument("id")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.action == "list":
            result: Any = _list(settings)
        elif args.action == "add":
            accounts.check_id(args.id)
            if args.id not in accounts.configured(settings) and settings.login_url and settings.login_token:
                # A new account starts with no browser profile or snapshot, whatever an earlier one left.
                LoginClient(settings.login_url, settings.login_token).forget(args.id)
            accounts.add(
                settings, args.id, rpc_budget=args.rpc_budget, thumb_budget=args.thumb_budget, email=args.email
            )
            result = {"added": args.id, "default_account_left": _rest(settings)}
        elif args.action == "budget":
            accounts.set_budget(settings, args.id, rpc_budget=args.rpc_budget, thumb_budget=args.thumb_budget)
            result = {"changed": args.id, "default_account_left": _rest(settings)}
        else:
            accounts.check_id(args.id)
            if args.id not in accounts.configured(settings):
                raise accounts.AccountConfigError(f"no account {args.id}")
            if settings.login_url and settings.login_token:
                # First the Google sign-in (the browser profile) and the snapshot; then the account here.
                LoginClient(settings.login_url, settings.login_token).forget(args.id)
            accounts.remove(settings, args.id)
            result = {"removed": args.id}
    except (ConfigError, accounts.AccountConfigError) as exc:
        print("photos-mcp admin:", *exc.args, file=sys.stderr)
        return 1
    except LoginUnavailable:
        print(
            "photos-mcp admin: the login service did not delete the account's browser profile and snapshot; "
            "nothing was changed. Start it and try again.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _rest(settings: Settings) -> dict[str, int]:
    rest = accounts.default(settings)
    return {"daily_rpc_budget": rest.rpc_budget, "daily_thumb_budget": rest.thumb_budget}
