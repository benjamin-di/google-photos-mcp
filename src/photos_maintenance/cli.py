"""Bounded operator commands for one independently owned account.

This is separate from the human sign-in service. The caller supplies private
paths, account identity, references and permission; no scheduler starts here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from signin_window.leases import LeaseKind, ordered_file_lease
from signin_window.sessions import AttemptBudget

from .browser import open_google_browser
from .config import load_identity_target
from .keeper import IdentityKeeper, read_identity_status
from .models import PersistenceError, ProviderReadiness, Requirement
from .state import TargetStateStore

TARGET = "google"
PREFIX = "PHOTOS_MAINTENANCE"


def stores(root: Path):
    def clock():
        return datetime.now(UTC)

    state = TargetStateStore(root / "identity-targets", utc_now=clock)
    budget = AttemptBudget(
        root / "session-keeper", utc_now=clock, error_type=PersistenceError, label="credential budget"
    )
    return state, budget


def lease(root: Path):
    profile = root / "browser-profiles" / TARGET / "default"
    identifier = hashlib.sha256(str(profile.expanduser().resolve()).encode()).hexdigest()[:16]
    return ordered_file_lease(
        root / "locks" / f"profile-{identifier}.lock", kind=LeaseKind.DURABLE_PROFILE, timeout_seconds=5
    )


def build_keeper(root: Path, export_dir: Path, expected_email: str, reference: str, *, timezone_id: str | None = None):
    if not expected_email or "@" not in expected_email or any(x in expected_email for x in ("\n", "\r")):
        raise ValueError("configure the expected account")
    spec = load_identity_target(TARGET, credential_reference=reference, timezone_id=timezone_id)
    state, budget = stores(root)
    profile = root / "browser-profiles" / TARGET / "default"

    def readiness():
        missing = []
        if not reference:
            missing.append(Requirement.CREDENTIAL_REFERENCE)
        if shutil.which("op") is None:
            missing.append(Requirement.OP_CLI)
        if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip():
            missing.append(Requirement.OP_SERVICE_TOKEN)
        return ProviderReadiness(missing=tuple(missing))

    return IdentityKeeper(
        target=TARGET,
        expected_email=expected_email,
        export_dir=export_dir,
        state=state,
        budget=budget,
        readiness=readiness,
        open_browser=lambda: open_google_browser(spec, profile=profile, expected_email=expected_email),
        lease=lambda: lease(root),
        utc_now=lambda: datetime.now(UTC),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("keep", "status", "acknowledge"))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get(PREFIX + "_ROOT", "/maintenance")))
    parser.add_argument("--export-dir", type=Path, default=os.environ.get(PREFIX + "_EXPORT_DIR"))
    parser.add_argument("--expected-email", default=os.environ.get(PREFIX + "_EXPECTED_EMAIL", ""))
    parser.add_argument("--credential-reference", default=os.environ.get(PREFIX + "_CREDENTIAL_REFERENCE", ""))
    parser.add_argument("--timezone", default=os.environ.get(PREFIX + "_TIMEZONE"))
    parser.add_argument("--allow-credential-mint", action="store_true")
    parser.add_argument("--hold-id")
    args = parser.parse_args(argv)
    try:
        state, budget = stores(args.root)
        if args.action == "keep":
            if args.export_dir is None:
                raise ValueError("configure the snapshot export directory")
            result = build_keeper(
                args.root, args.export_dir, args.expected_email, args.credential_reference, timezone_id=args.timezone
            ).run(allow_credential_mint=args.allow_credential_mint)
            payload, code = result.public_dict(), result.exit_code
        elif args.action == "status":
            report = read_identity_status(TARGET, state=state, budget=budget, now=datetime.now(UTC))
            payload = report.public_dict()
            code = 1 if report.stale or report.state.hold is not None else 0
        else:
            if not args.hold_id:
                raise ValueError("an exact hold id is required")
            with lease(args.root):
                cleared = state.clear_hold(TARGET, args.hold_id)
            payload = {"target": TARGET, "hold_id": args.hold_id, "cleared": cleared}
            code = 0 if cleared else 1
    except Exception:
        # Never print account identifiers, references, snapshots, remote text or
        # exception details from an unattended credential-bearing command.
        payload = {
            "target": TARGET,
            "outcome": "infrastructure_error",
            "reason": "configuration",
            "needs_attention": True,
        }
        code = 1
    print(json.dumps(payload, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
