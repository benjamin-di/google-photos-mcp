"""Recoverable promotion of a manually verified native profile and snapshot.

Every entry is called under the existing durable native-profile lease. The
snapshot root may be a bind mount, so only its fixed files are replaced.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from signin_window.automation.profiles import prepare_profile
from signin_window.state import atomic_private_write, fsync_directory, read_private_file

from photos_mcp.connection_controls import ConnectionAdmission, pending_path
from photos_mcp.session import read_snapshot

from .state import TargetStateStore

FILES = ("cookies.txt", "meta.json", "keeper.json")
SINGLETONS = {"SingletonLock", "SingletonSocket", "SingletonCookie", ".signin-singleton-recovery"}


def target_store(root):
    return TargetStateStore(root / "identity-targets", utc_now=lambda: datetime.now(UTC))


def exchange(first: Path, second: Path) -> None:
    """Linux atomically exchanges two same-filesystem profile directories."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        raise OSError(ctypes.get_errno(), "profile exchange failed")
    fsync_directory(first.parent)
    fsync_directory(second.parent)


def identity(path):
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("profile must be a directory")
    return [metadata.st_dev, metadata.st_ino]


def private_profile_tree(profile):
    """A candidate must not inherit links into the accepted or another profile."""
    for directory, directories, files in os.walk(profile, followlinks=False):
        directories[:] = [name for name in directories if name not in SINGLETONS]
        for name in directories + files:
            if name not in SINGLETONS and (Path(directory) / name).is_symlink():
                raise ValueError("profile contains an unverified link")


class NativePromotion:
    def __init__(self, root: Path, export: Path, guard=None):
        self.root, self.export = root, export
        self.profile = root / "browser-profiles" / "google" / "default"
        self.work = root / "human-recovery"
        self.candidate = self.work / "profile"
        self.snapshot = self.work / "snapshot"
        self.backup = self.work / "accepted-snapshot"
        self.journal = self.work / "commit.json"
        self.target = root / "identity-targets" / "google.json"
        self.target_backup = self.work / "accepted-target.json"
        self.guard = guard
        self.pending = pending_path(guard.store, guard.account) if guard else None

    def _unmark(self, identifier=None):
        if self.pending is not None and (self.pending.exists() or self.pending.is_symlink()):
            marker = json.loads(read_private_file(self.pending, max_bytes=1024))
            if identifier is None or marker.get("id") != identifier:
                raise ValueError("native publication marker has no matching journal")
            self.pending.unlink(missing_ok=True)
            fsync_directory(self.pending.parent)

    def _record(self, data):
        atomic_private_write(self.journal, (json.dumps(data, sort_keys=True) + "\n").encode())

    def _verify_files(self, folder, hashes):
        for name in FILES:
            path = folder / name
            if name not in hashes:
                if path.exists() or path.is_symlink():
                    raise ValueError("native snapshot recovery cannot be verified")
            elif hashlib.sha256(read_private_file(path, max_bytes=1024**2)).hexdigest() != hashes[name]:
                raise ValueError("native snapshot recovery cannot be verified")

    def _cleanup(self, identifier=None):
        # The journal is removed last: interrupted cleanup remains recoverable.
        if self.candidate.exists():
            prepare_profile(self.candidate)
        for path in (self.candidate, self.snapshot, self.backup):
            if path.exists():
                shutil.rmtree(path)
        self.target_backup.unlink(missing_ok=True)
        self._unmark(identifier)
        self.journal.unlink(missing_ok=True)
        fsync_directory(self.work)

    def recover(self):
        if not self.journal.exists():
            if self.pending is not None and (self.pending.exists() or self.pending.is_symlink()):
                raise ValueError("native publication marker has no matching journal")
            return
        data = json.loads(read_private_file(self.journal, max_bytes=4096))
        if data.get("schema") != 1 or data.get("phase") not in ("prepared", "committed", "rolled_back"):
            raise ValueError("invalid native recovery journal")
        if self.guard is None or not isinstance(data.get("id"), str):
            raise ValueError("native recovery requires its connection controls")
        if self.pending.exists() or self.pending.is_symlink():
            marker = json.loads(read_private_file(self.pending, max_bytes=1024))
            if marker.get("id") != data["id"]:
                raise ValueError("native publication marker has no matching journal")
        if data["phase"] == "prepared":
            atomic_private_write(self.pending, json.dumps({"id": data["id"], "phase": "prepared"}).encode())
            with ConnectionAdmission(self.guard.store).lock(self.guard.account, exclusive=True):
                self._rollback(data)
        elif data["phase"] == "committed":
            if identity(self.profile) != data["candidate_identity"]:
                raise ValueError("native profile recovery cannot be verified")
            self._verify_files(self.export, data["candidate_files"])
            # Accounting was committed before this phase. A later epoch, hold or
            # disconnect must never be modified by cleanup of old proof.
        else:
            if identity(self.profile) != data["accepted_identity"]:
                raise ValueError("native profile recovery cannot be verified")
            self._verify_files(self.export, data["accepted_files"])
        self._cleanup(data["id"])

    def _rollback(self, data):
        self._verify_files(self.backup, data["accepted_files"])
        target_bytes = None
        if data["target_hash"] is not None:
            target_bytes = read_private_file(self.target_backup, max_bytes=64 * 1024)
            if hashlib.sha256(target_bytes).hexdigest() != data["target_hash"]:
                raise ValueError("native accounting recovery cannot be verified")
        current = identity(self.profile)
        if current == data["candidate_identity"]:
            if identity(self.candidate) != data["accepted_identity"]:
                raise ValueError("native profile recovery cannot be verified")
            exchange(self.profile, self.candidate)
        elif current != data["accepted_identity"]:
            raise ValueError("native profile recovery cannot be verified")
        for name in FILES:
            if name in data["existing"]:
                atomic_private_write(self.export / name, read_private_file(self.backup / name, max_bytes=1024**2))
            else:
                (self.export / name).unlink(missing_ok=True)
        fsync_directory(self.export)
        if target_bytes is not None:
            self.target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_private_write(self.target, target_bytes)
        else:
            self.target.unlink(missing_ok=True)
            if self.target.parent.exists():
                fsync_directory(self.target.parent)
        data["phase"] = "rolled_back"
        self._record(data)

    def stage(self):
        self.recover()
        if self.work.is_symlink():
            raise ValueError("native staging must be a private directory")
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A previous failed/cancelled attempt never touched the accepted paths.
        self._cleanup()
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity(self.profile)
        prepare_profile(self.profile)
        private_profile_tree(self.profile)
        shutil.copytree(
            self.profile,
            self.candidate,
            symlinks=True,
            ignore=shutil.ignore_patterns(*SINGLETONS),
        )
        self.snapshot.mkdir(mode=0o700)

    def promote(self, guard, epoch, *, generation, hold_id, checkpoint=lambda phase: None, validate=lambda: None):
        prepare_profile(self.candidate)  # The candidate browser must be closed, in this PID namespace.
        private_profile_tree(self.candidate)
        for directory, _, files in os.walk(self.candidate, followlinks=False):
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            fsync_directory(Path(directory))
        self.export.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backup.mkdir(mode=0o700)
        verified = read_snapshot(self.snapshot)
        if verified.generation != generation:
            raise ValueError("native snapshot generation changed")
        try:
            target_bytes = read_private_file(self.target, max_bytes=64 * 1024)
        except FileNotFoundError:
            target_bytes = None
        if target_bytes is not None:
            atomic_private_write(self.target_backup, target_bytes)
        existing, accepted_files = [], {}
        for name in FILES:
            try:
                payload = read_private_file(self.export / name, max_bytes=1024**2)
            except FileNotFoundError:
                continue
            atomic_private_write(self.backup / name, payload)
            existing.append(name)
            accepted_files[name] = hashlib.sha256(payload).hexdigest()
        data = dict(
            schema=1,
            id=uuid4().hex,
            phase="prepared",
            existing=existing,
            generation=generation,
            hold_id=hold_id,
            epoch=epoch,
            target_hash=hashlib.sha256(target_bytes).hexdigest() if target_bytes is not None else None,
            accepted_files=accepted_files,
            candidate_files={
                name: hashlib.sha256(read_private_file(self.snapshot / name, max_bytes=1024**2)).hexdigest()
                for name in FILES
            },
            accepted_identity=identity(self.profile),
            candidate_identity=identity(self.candidate),
        )
        admission = ConnectionAdmission(guard.store).lock(guard.account, exclusive=True)
        self._record(data)
        if self.pending is not None:
            atomic_private_write(self.pending, json.dumps({"id": data["id"], "phase": "prepared"}).encode())
        checkpoint("prepared")
        with admission, guard.publish(epoch):
            validate()
            exchange(self.profile, self.candidate)
            checkpoint("profile")
            for name in FILES:
                atomic_private_write(self.export / name, read_private_file(self.snapshot / name, max_bytes=1024**2))
                checkpoint(name)
            target_store(self.root).record_manual_publish(
                "google", generation, hold_id, verified_at=verified.verified_at
            )
            checkpoint("accounting")
            data["phase"] = "committed"
            self._record(data)
            checkpoint("committed")
        self.recover()

    def discard(self):
        if self.journal.exists():
            self.recover()
        elif self.work.exists():
            self._cleanup()
