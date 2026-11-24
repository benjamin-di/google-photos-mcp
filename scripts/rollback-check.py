"""Prove rollback to the previous release on disposable synthetic state.

The previous release's code and locked dependencies are exported from its tag
into an isolated virtualenv and run in a separate interpreter against state
written by this source, and the other way round. The receipt is
`.artifacts/rollback-check.json` (see fleet release_compatibility).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / ".artifacts"
RECEIPT = ARTIFACTS / "rollback-check.json"
EVIDENCE = ARTIFACTS / "rollback-check" / "evidence.json"

SID_ACTIVE = "A" * 43
SID_PENDING = "P" * 43
SID_REVOKED = "R" * 43
SID_ROLLBACK = "B" * 43
SID_NEXT = "N" * 43
FIXED_NOW = 1_800_000_000.0

SIDE = r"""
import importlib.metadata
import json
import sqlite3
import sys
from pathlib import Path

import photos_mcp
from photos_mcp.connections_auth import BrowserStore
from photos_mcp.session import read_snapshot

op, state_dir, expected_root = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]).resolve()
if not Path(photos_mcp.__file__).resolve().is_relative_to(expected_root):
    raise SystemExit(f"unexpected module path: {photos_mcp.__file__}")
sw_version = importlib.metadata.version("signin-window")
now = float(sys.argv[4])

def store(require_existing=False):
    return BrowserStore(state_dir / "connections", clock=lambda: now, require_existing=require_existing)

def tables(s):
    with sqlite3.connect(s.path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))

out = {"signin_window": sw_version}
if op == "seed-sessions":
    s = store()
    s.save(sys.argv[5], {"issuer": "https://id.example", "sub": "owner-a", "expires": now + 3600})
    s.save(
        sys.argv[6],
        {"_state_abc": {"nonce": "n1"}, "next": "/connections/", "pending_expires": now + 600},
    )
    out["tables"] = tables(s)
elif op == "seed-with-revocation":
    s = store()
    s.save_if_not_revoked(
        sys.argv[5],
        {"issuer": "https://id.example", "sub": "owner-a", "expires": now + 3600, "_family": "fam-active"},
    )
    s.save_if_not_revoked(
        sys.argv[6],
        {"_state_xyz": {"nonce": "n2"}, "next": "/connections/", "pending_expires": now + 600},
    )
    s.save_if_not_revoked(
        sys.argv[7],
        {"issuer": "https://id.example", "sub": "owner-b", "expires": now + 3600, "_family": "fam-revoked"},
    )
    s.revoke(sys.argv[7])
    out["tables"] = tables(s)
    with sqlite3.connect(s.path) as conn:
        out["revocations"] = conn.execute("SELECT COUNT(*) FROM revocations").fetchone()[0]
elif op == "read-sessions":
    s = store()
    out["tables"] = tables(s)
    out["loaded"] = {sid: s.load(sid) for sid in sys.argv[5:]}
elif op == "write-and-delete":
    s = store()
    s.save(sys.argv[5], {"issuer": "https://id.example", "sub": "owner-prev", "expires": now + 3600})
    s.delete(sys.argv[6])
    out["loaded"] = s.load(sys.argv[5])
    out["deleted"] = s.load(sys.argv[6])
elif op == "rotate-and-revoke":
    s = store()
    ok = s.save_if_not_revoked(
        sys.argv[6],
        {"issuer": "https://id.example", "sub": "owner-prev", "expires": now + 3600, "_family": "fam-rot"},
        old_sid=sys.argv[5],
    )
    out["rotated"] = ok
    out["loaded_new"] = s.load(sys.argv[6])
    out["loaded_old"] = s.load(sys.argv[5])
    s.revoke(sys.argv[6])
    out["after_revoke"] = s.load(sys.argv[6])
elif op == "controls-init":
    s = store(require_existing=False)
    s.claim_owner("first", "https://id.example", sys.argv[5])
    out["control"] = {"control": list(s.control("first")), "owner": list(s.owner("first") or ())}
elif op == "controls-read":
    s = store(require_existing=True)
    out["control"] = {"control": list(s.control("first")), "owner": list(s.owner("first") or ())}
elif op == "controls-update":
    s = store(require_existing=True)
    s.set_control("first", 1)
    out["control"] = {"control": list(s.control("first")), "owner": list(s.owner("first") or ())}
elif op == "app-read":
    snap = read_snapshot(state_dir / "session")
    out["email"] = snap.email
    out["generation"] = snap.generation
else:
    raise SystemExit(f"unknown op: {op}")

print(json.dumps(out, sort_keys=True))
"""


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def require(condition: bool, message: object = "rollback check failed") -> None:
    if not condition:
        raise AssertionError(str(message))


class Check:
    def __init__(self, previous_root: Path, previous_py: Path, state: Path):
        self.sides = {
            "previous": (previous_py, previous_root),
            "current": (Path(sys.executable), ROOT),
        }
        self.state = state
        self.log: list[dict] = []

    def run(self, side: str, op: str, *args: str) -> dict:
        python_bin, root = self.sides[side]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(python_bin), "-B", "-c", SIDE, op, str(self.state), str(root), str(FIXED_NOW), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.log.append(
            {
                "side": side,
                "op": op,
                "args": list(args),
                "exit": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        )
        if result.returncode:
            raise AssertionError(f"{side} {op} failed: {result.stderr.strip()[-600:]}")
        return json.loads(result.stdout)


def preserved_files(state: Path) -> dict[str, str]:
    targets = (state / "session" / "meta.json", state / "session" / "cookies.txt")
    return {str(t.relative_to(state)): hashlib.sha256(t.read_bytes()).hexdigest() for t in targets}


def fresh(base: Path) -> Path:
    state = Path(tempfile.mkdtemp(dir=base))
    state.chmod(0o700)
    session_dir = state / "session"
    session_dir.mkdir(mode=0o700)
    meta = {
        "schema": 1,
        "generation": 1,
        "email": "example@example.invalid",
        "account_index": 0,
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "chromium_major": 145,
        "verified_at": FIXED_NOW,
    }
    meta_path = session_dir / "meta.json"
    meta_path.write_text(json.dumps(meta))
    meta_path.chmod(0o600)
    cookies_path = session_dir / "cookies.txt"
    cookies_path.write_text(".google.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-cookie\n")
    cookies_path.chmod(0o600)
    return state


def scenarios(previous_root: Path, previous_py: Path, base: Path):
    results: dict[str, str] = {}
    checks: list[tuple[str, Check]] = []

    def scenario(name: str, body):
        check = Check(previous_root, previous_py, fresh(base))
        checks.append((name, check))
        try:
            body(check)
        except Exception as error:
            check.log.append({"failure": f"{type(error).__name__}: {error}"})
            results[name] = "failed"
        else:
            results[name] = "passed"

    def upgrade(c: Check):
        seeded = c.run("previous", "seed-sessions", SID_ACTIVE, SID_PENDING)
        require(seeded["signin_window"] == "0.8.4", seeded)
        require(set(seeded["tables"]) == {"controls", "owners", "sessions"}, seeded["tables"])
        upgraded = c.run("current", "read-sessions", SID_ACTIVE, SID_PENDING)
        require(upgraded["signin_window"] == "0.10.0", upgraded)
        require(
            set(upgraded["tables"]) == {"controls", "owners", "sessions", "revocations", "session_families"},
            upgraded["tables"],
        )
        require(upgraded["loaded"][SID_ACTIVE]["sub"] == "owner-a", upgraded["loaded"])
        require(upgraded["loaded"][SID_PENDING]["_state_abc"] == {"nonce": "n1"}, upgraded["loaded"])

    def previous_read(c: Check):
        seeded = c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        require({"sessions", "revocations", "session_families"} <= set(seeded["tables"]), seeded["tables"])
        require(seeded["revocations"] >= 1, seeded)
        seen = c.run("previous", "read-sessions", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        require(seen["signin_window"] == "0.8.4", seen)
        require(seen["loaded"][SID_ACTIVE]["sub"] == "owner-a", seen["loaded"])
        require(seen["loaded"][SID_PENDING]["_state_xyz"] == {"nonce": "n2"}, seen["loaded"])
        require(seen["loaded"][SID_REVOKED] == {}, seen["loaded"])

    def previous_write(c: Check):
        c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        written = c.run("previous", "write-and-delete", SID_ROLLBACK, SID_ACTIVE)
        require(written["loaded"]["sub"] == "owner-prev", written)
        require(written["deleted"] == {}, written)
        seen = c.run("current", "read-sessions", SID_ROLLBACK, SID_ACTIVE, SID_PENDING)
        require(seen["loaded"][SID_ROLLBACK]["sub"] == "owner-prev", seen["loaded"])
        require(seen["loaded"][SID_ACTIVE] == {}, seen["loaded"])
        require(seen["loaded"][SID_PENDING]["_state_xyz"] == {"nonce": "n2"}, seen["loaded"])
        rotated = c.run("current", "rotate-and-revoke", SID_ROLLBACK, SID_NEXT)
        require(rotated["rotated"] is True, rotated)
        require(rotated["loaded_old"] == {}, rotated)
        require(rotated["loaded_new"]["sub"] == "owner-prev", rotated)
        require(rotated["after_revoke"] == {}, rotated)

    def controls(c: Check):
        created = c.run("current", "controls-init", "owner-a")["control"]
        seen = c.run("previous", "controls-read")["control"]
        require(seen == created, (seen, created))
        updated = c.run("previous", "controls-update")["control"]
        require(updated["control"][1] == 1, updated)
        after = c.run("current", "controls-read")["control"]
        require(after == updated, (after, updated))

    def journal(c: Check):
        before = preserved_files(c.state)
        prev_app = c.run("previous", "app-read")
        require(prev_app["email"] == "example@example.invalid" and prev_app["generation"] == 1, prev_app)
        c.run("previous", "seed-sessions", SID_ACTIVE, SID_PENDING)
        c.run("current", "read-sessions", SID_ACTIVE, SID_PENDING)
        curr_app = c.run("current", "app-read")
        require(curr_app["email"] == prev_app["email"] and curr_app["generation"] == prev_app["generation"], curr_app)
        c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        c.run("previous", "write-and-delete", SID_ROLLBACK, SID_ACTIVE)
        c.run("current", "read-sessions", SID_ROLLBACK, SID_PENDING)
        after_app = c.run("previous", "app-read")
        require(
            after_app["email"] == prev_app["email"] and after_app["generation"] == prev_app["generation"], after_app
        )
        require(preserved_files(c.state) == before, "application stores changed across upgrade and rollback")

    scenario("upgrade", upgrade)
    scenario("previous-read", previous_read)
    scenario("previous-write", previous_write)
    scenario("controls-preserved", controls)
    scenario("delivery-journal-preserved", journal)
    return results, {name: check.log for name, check in checks}


def main() -> int:
    started = datetime.now(UTC)
    source = git("rev-parse", "HEAD")
    compatibility = json.loads((ROOT / "release" / "compatibility.json").read_text(encoding="utf-8"))
    rollback_versions = (compatibility.get("state") or {}).get("rollback_versions") or []
    if not rollback_versions:
        raise RuntimeError("release/compatibility.json must declare state.rollback_versions")
    previous = str(rollback_versions[-1])
    tag = f"v{previous}"
    ARTIFACTS.mkdir(exist_ok=True)
    RECEIPT.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="rollback-check-") as temporary:
        base = Path(temporary)
        previous_root = base / "previous"
        previous_root.mkdir(mode=0o700)
        archive = subprocess.run(
            ["git", "-C", str(ROOT), "archive", tag, "pyproject.toml", "uv.lock", "README.md", "src"],
            check=True,
            capture_output=True,
        )
        with tarfile.open(fileobj=BytesIO(archive.stdout)) as bundle:
            bundle.extractall(previous_root, filter="data")
        sync_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(previous_root / ".venv")}
        subprocess.run(["uv", "sync", "--locked", "--no-dev", "--quiet"], cwd=previous_root, env=sync_env, check=True)
        previous_py = previous_root / ".venv" / "bin" / "python"
        os.chmod(base, 0o700)
        results, log = scenarios(previous_root, previous_py, base)
    EVIDENCE.parent.mkdir(exist_ok=True)
    EVIDENCE.write_text(json.dumps({"previous_tag": tag, "source_sha": source, "scenarios": log}, indent=1) + "\n")
    passed = all(value == "passed" for value in results.values())
    receipt = {
        "schema": 1,
        "source_sha": source,
        "previous_version": previous,
        "status": "passed" if passed else "failed",
        "scenarios": results,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "artifacts": [
            {"path": str(EVIDENCE.relative_to(ROOT)), "sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()}
        ],
    }
    RECEIPT.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(json.dumps(results, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
