"""Application-owned fencing for independent credential publishers."""

from contextlib import contextmanager


class ConnectionSuspended(Exception):
    """The owner disconnected the account or replaced this attempt's generation."""


class PublicationGuard:
    def __init__(self, store, account):
        self.store, self.account = store, account

    def check(self):
        epoch, suspended = self.store.control(self.account)
        if suspended:
            raise ConnectionSuspended()
        return epoch

    @contextmanager
    def publish(self, epoch):
        # Disconnect and final publication serialize in the same database.
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT epoch,suspended FROM controls WHERE account=?", (self.account,)).fetchone()
            if (row or ("initial", 0)) != (epoch, 0):
                raise ConnectionSuspended()
            yield


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


def pending_path(store, account):
    from .accounts import valid_id

    if not valid_id(account):
        raise ValueError("Invalid account")
    return store.path.parent / ("native-publication-" + account + ".json")


def publication_pending(store, account):
    path = pending_path(store, account)
    return path.exists() or path.is_symlink()
