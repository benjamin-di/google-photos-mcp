"""Application-owned fencing for independent credential publishers."""

from contextlib import contextmanager
from hashlib import sha256
from hmac import compare_digest
from secrets import token_hex


class ConnectionSuspended(Exception):
    """The owner disconnected the account or replaced this attempt's generation."""


class PublicationGuard:
    def __init__(self, store, account):
        self.store, self.account = store, account

    def require_active(self):
        epoch, suspended = self.store.control(self.account)
        if suspended:
            raise ConnectionSuspended()
        return epoch

    def attempt(self, epoch):
        """A current owner attempt may verify while access remains suspended."""
        with self.store.db() as db:
            self._check_epoch(db, epoch, allow_suspended=True)
        return epoch

    @contextmanager
    def activate(self, epoch):
        """Bind verified further-account proof and open admission in one transaction."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_epoch(db, epoch, allow_suspended=True)
            if publication_pending(self.store, self.account):
                raise ConnectionSuspended()
            yield
            db.execute("UPDATE controls SET suspended=0 WHERE account=? AND epoch=?", (self.account, epoch))

    @contextmanager
    def publish(self, epoch, *, accept_snapshot=False, activate=False):
        # Persist the hold BEFORE touching external files. Rolling back a failed
        # file publication must not restore an apparently accepted binding.
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_epoch(db, epoch, allow_suspended=activate)
            db.execute(
                "CREATE TABLE IF NOT EXISTS accepted_publications "
                "(account TEXT PRIMARY KEY,epoch TEXT NOT NULL,binding TEXT NOT NULL,ready INT NOT NULL)"
            )
            # Every publication invalidates older captures, including refreshes:
            # the shared guard cannot distinguish a refresh from a replacement.
            binding = token_hex(24)
            db.execute("INSERT OR REPLACE INTO accepted_publications VALUES (?,?,?,0)", (self.account, epoch, binding))
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_epoch(db, epoch, allow_suspended=activate)
            row = db.execute(
                "SELECT epoch,binding,ready FROM accepted_publications WHERE account=?", (self.account,)
            ).fetchone()
            if row != (epoch, binding, 0):
                raise ConnectionSuspended()
            yield
            if accept_snapshot:
                db.execute("UPDATE accepted_publications SET ready=1 WHERE account=?", (self.account,))
            if activate:
                db.execute("UPDATE controls SET suspended=0 WHERE account=? AND epoch=?", (self.account, epoch))

    def snapshot(self, epoch, *, activate=False):
        """Only a complete verified snapshot publisher may establish acceptance."""
        return self.publish(epoch, accept_snapshot=True, activate=activate)

    @contextmanager
    def status(self, epoch):
        """Fence a status-only write without creating or clearing snapshot receipts."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_epoch(db, epoch)
            yield

    def _check_epoch(self, db, epoch, *, allow_suspended=False):
        row = db.execute("SELECT epoch,suspended FROM controls WHERE account=?", (self.account,)).fetchone()
        current, suspended = row or ("initial", 0)
        if current != epoch or suspended not in ((0, 1) if allow_suspended else (0,)):
            raise ConnectionSuspended()

    @contextmanager
    def observe(self, epoch):
        """Hold the accepted lifecycle through a reader/mutator, without publishing it."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_epoch(db, epoch)
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accepted_publications'"
            ).fetchone()
            row = (
                db.execute(
                    "SELECT epoch,binding,ready FROM accepted_publications WHERE account=?", (self.account,)
                ).fetchone()
                if table
                else None
            )
            if row is None or row[0] != epoch or row[2] != 1:
                raise ConnectionSuspended()
            yield row[1]


class ConnectionAdmission:
    """Drain admitted account operations before confirming disconnect.

    Shared file locks cover multiple reader processes. The durable SQLite barrier
    is written first, so waiting for an old call cannot admit another one.
    """

    def __init__(self, store):
        self.store = store

    @contextmanager
    def lock(self, account, *, exclusive=False, timeout=30, lifecycle=False):
        import fcntl
        import os
        import time

        from .accounts import valid_id

        if not valid_id(account):
            raise ValueError("Invalid account")
        path = self.store.path.parent / (("lifecycle-" if lifecycle else "admission-") + account + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if exclusive:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Account operations are still draining") from None
                        time.sleep(0.02)
            else:
                fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def operation(self, account):
        from .errors import PhotosError

        with self.lock(account):
            if self.store.control(account)[1] or publication_pending(self.store, account):
                raise PhotosError("connection_disconnected", log_detail="connection suspended")
            yield

    def drain(self, account, *, timeout=30):
        with self.lock(account, exclusive=True, timeout=timeout):
            pass

    @contextmanager
    def bound_operation(self, account, scope, expected=None):
        """Fence a managed native call with the credential publishers' actual transaction.

        Only callers with guarded publication custody may use this context. A native
        snapshot generation is deliberately not part of the connection identity.
        """
        from .errors import PhotosError

        with self.operation(account):
            epoch, suspended = self.store.control(account)
            if suspended or epoch == "initial" or not self.store.adopted(account):
                raise PhotosError("connection_changed")
            try:
                with PublicationGuard(self.store, account).observe(epoch) as binding:
                    token = sha256((account + "\0" + epoch + "\0" + binding + "\0" + scope).encode()).hexdigest()
                    if expected is not None and not compare_digest(token, expected):
                        raise PhotosError("connection_changed")
                    if publication_pending(self.store, account):
                        raise PhotosError("connection_changed")
                    yield token
                    if publication_pending(self.store, account):
                        raise PhotosError("connection_changed")
            except ConnectionSuspended:
                raise PhotosError("connection_changed") from None


def pending_path(store, account):
    from .accounts import valid_id

    if not valid_id(account):
        raise ValueError("Invalid account")
    return store.path.parent / ("native-publication-" + account + ".json")


def recovery_path(store, account):
    return pending_path(store, account).with_suffix(".recovery-required")


def recovery_required(store, account):
    path = recovery_path(store, account)
    return path.exists() or path.is_symlink()


def publication_pending(store, account):
    path = pending_path(store, account)
    return path.exists() or path.is_symlink() or recovery_required(store, account)
