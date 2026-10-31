"""Local, cooperative deployment holds. Never opens account or provider state.

The owner must provision the three private lock files and control record before
starting a supporting image. There is deliberately no initialization command.
All work keeps a shared kernel lease; a hold blocks new work and waits without
cancelling existing work. Process death releases leases, never the durable hold.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import inspect
import json
import os
import stat
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from signin_window.state import atomic_private_write

ROOTS = {"reader": "/state", "login": "/profiles", "maintenance": "/maintenance"}


class Busy(RuntimeError):
    pass


def identity(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class Gate:
    def __init__(self, root: Path, scope: str):
        self.root, self.scope = root, scope
        if scope not in ROOTS or not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise ValueError("existing deployment root required")
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private deployment root required")
        self.boot_id = None
        self.owner = None
        with self.lock():
            self.read()

    def path(self, name):
        path = self.root / (".deployment-" + name)
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise ValueError("private existing deployment record required")
        return path

    def open(self, name):
        return self.path(name).open("r+")

    @contextlib.contextmanager
    def lock(self):
        with self.open("lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self):
        value = json.loads(self.path("control.json").read_text())
        if (
            set(value) != {"schema", "scope", "operation", "completed", "inflight", "frozen"}
            or value["schema"] != 1
            or value["scope"] != self.scope
            or (value["operation"] != "" and not identity(value["operation"]))
            or type(value["frozen"]) is not bool
            or (value["frozen"] and not value["operation"])
            or not isinstance(value["completed"], list)
            or len(value["completed"]) > 4096
            or len(set(value["completed"])) != len(value["completed"])
            or any(not identity(op) for op in value["completed"])
            or not isinstance(value["inflight"], list)
            or len(value["inflight"]) > 4096
            or len(set(value["inflight"])) != len(value["inflight"])
            or any(not identity(ticket) for ticket in value["inflight"])
            or value["operation"] in value["completed"]
        ):
            raise ValueError("deployment control damaged")
        return value

    def write(self, value):
        self.path("control.json")
        atomic_private_write(self.root / ".deployment-control.json", json.dumps(value).encode())
        # signin-window's atomic writer fsyncs the file; persist its rename too.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def start(self):
        """Only the serving process owns this lock; short worker commands do not."""
        self.owner = self.open("owner")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.boot_id = str(uuid.uuid4())
            self.owner.seek(0)
            self.owner.truncate()
            self.owner.write(self.boot_id)
            self.owner.flush()
            os.fsync(self.owner.fileno())
            return self
        except BaseException:
            self.owner.close()
            self.owner = None
            raise

    def stop(self):
        if self.owner is not None:
            self.owner.close()
            self.owner = None

    def ticket(self, *, continuation=False):
        with self.lock():
            value = self.read()
            if value["frozen"] or (value["operation"] and not continuation):
                raise Busy("deployment hold")
            if len(value["inflight"]) >= 4096:
                raise Busy("deployment work limit")
            stream = self.open("active")
            try:
                fcntl.flock(stream, fcntl.LOCK_SH)
                ticket = str(uuid.uuid4())
                value["inflight"].append(ticket)
                self.write(value)
                return Ticket(self, stream, ticket)
            except BaseException:
                stream.close()
                raise

    @contextlib.contextmanager
    def work(self):
        with self.ticket():
            yield

    def idle(self):
        # Kernel leases alone disappear when a worker dies, even when a browser
        # child survives. Unknown tickets are never inferred complete or cleared.
        if self.read()["inflight"]:
            return False
        with self.open("active") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    def status_locked(self, value):
        with self.open("owner") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                boot = owner.read().strip()
                if not identity(boot):
                    raise ValueError("serving identity missing") from None
            else:
                raise Busy("serving process absent")
        return dict(
            schema=1,
            scope=self.scope,
            boot_id=boot,
            operation=value["operation"],
            last_operation=value["completed"][-1] if value["completed"] else "",
            frozen=value["frozen"],
            idle=self.idle(),
        )

    def status(self):
        with self.lock():
            return self.status_locked(self.read())

    def hold(self, operation):
        if not identity(operation):
            raise ValueError("operation required")
        with self.lock():
            value = self.read()
            self.status_locked(value)
            if (
                value["operation"] not in ("", operation)
                or operation in value["completed"]
                or len(value["completed"]) >= 4096
            ):
                raise Busy("different or completed operation")
            value["operation"] = operation
            self.write(value)

    def drain(self, operation, timeout):
        if not 0 <= timeout <= 300:
            raise ValueError("bounded wait required")
        self.hold(operation)
        deadline = time.monotonic() + timeout
        while True:
            with self.lock():
                value = self.read()
                if value["operation"] != operation:
                    raise Busy("original hold changed")
                if self.idle():
                    value["frozen"] = True
                    self.write(value)
                    return self.status_locked(value)
            if time.monotonic() >= deadline:
                raise Busy("existing work retains hold")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def release(self, operation, boot):
        with self.lock():
            value = self.read()
            status = self.status_locked(value)
            if (
                not identity(operation)
                or value["operation"] != operation
                or not value["frozen"]
                or not status["idle"]
                or status["boot_id"] != boot
            ):
                raise Busy("exact idle serving hold required")
            value.update(operation="", frozen=False, completed=[*value["completed"], operation])
            self.write(value)
            return self.status_locked(value)


class Ticket:
    def __init__(self, gate, stream, identity):
        self.gate, self.stream, self.identity = gate, stream, identity
        self.closed = False

    def close(self):
        if self.closed:
            return
        with self.gate.lock():
            try:
                value = self.gate.read()
                if self.identity not in value["inflight"]:
                    raise ValueError("original work ticket missing")
                value["inflight"].remove(self.identity)
                self.gate.write(value)
            finally:
                self.closed = True
                self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Admission:
    """Count full ASGI lifetimes, including bodies, streams and WebSockets.

    Existing login viewers and cancellation can continue during the cooperative
    wait. Once all windows and requests finish, freeze closes every route.
    """

    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or scope.get("path") == "/health":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        continuation = (self.gate.scope != "reader" and path.startswith("/logins/")) or (
            self.gate.scope == "reader" and path.startswith("/connect/") and not path.endswith("/start")
        )
        try:
            ticket = self.gate.ticket(continuation=continuation)
        except (Busy, ValueError, OSError):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "deployment_hold"}, status_code=503)(scope, receive, send)
            return

        # ASGI cancellation does not terminate a provider thread. The admitted
        # application finishes under its original ticket, even if its caller left.
        async def admitted():
            try:
                await self.app(scope, receive, send)
            finally:
                ticket.close()

        await asyncio.shield(asyncio.create_task(admitted()))


class _WindowWork:
    """Only positive completion evidence may retire the original admission."""

    def __init__(self, ticket):
        self.ticket = ticket
        self.loop = asyncio.get_running_loop()
        self.finished = self.runner_pending = self.display_started = self.display_cleaned = False

    def settle(self):
        if self.finished and not self.runner_pending and self.display_cleaned:
            self.ticket.close()

    def task_done(self, _task=None):
        self.finished = True
        self.settle()

    def runner_done(self):
        self.runner_pending = False
        self.settle()


def guarded_windows(windows, gate):
    """Reserve before display creation, through queued runner and display cleanup.

    The pinned Windows can finish its task after repeated cancellation while its
    synchronous runner still writes. Observe the actual runner separately, without
    changing the service's cancellation or result handling. Context follows both
    its queued task and to_thread calls; an unobserved exit keeps a durable ticket.
    """
    original = windows.start
    runner, in_thread = windows.runner, windows._in_thread
    start_display, stop_display = windows.start_display, windows.stop_display
    current = ContextVar("deployment_window_work")

    async def admitted_thread(window):
        # Mark dispatch before submission: the worker may still be queued when
        # cancellation finishes the window task, or even cancels its worker future.
        current.get().runner_pending = True
        return await in_thread(window)

    if inspect.iscoroutinefunction(runner):

        async def admitted_runner(window):
            work = current.get()
            work.runner_pending = True
            try:
                return await runner(window)
            finally:
                work.runner_done()

    else:

        def admitted_runner(window):
            work = current.get()
            try:
                return runner(window)
            finally:
                # A cancelled asyncio worker future is not evidence that its
                # thread exited. Only this finally observes the runner's return.
                with contextlib.suppress(RuntimeError):  # Closed loop: retain unknown work.
                    work.loop.call_soon_threadsafe(work.runner_done)

    def admitted_display(window):
        current.get().display_started = True
        return start_display(window)

    def cleaned_display(window):
        work = current.get()
        procs = list(window.procs)
        result = stop_display(window)
        # The pinned stop_display clears handles even after a kill wait times out.
        # Retain those handles until exit has actually been confirmed.
        with contextlib.suppress(Exception):
            work.display_cleaned = all(proc.poll() is not None for proc in [*procs, *window.procs])
        return result

    async def start(*args, **kwargs):
        from signin_window.service import Busy as WindowBusy

        try:
            ticket = gate.ticket()
        except Busy:
            raise WindowBusy() from None

        async def admitted():
            work = _WindowWork(ticket)
            token = current.set(work)
            try:
                window = await original(*args, **kwargs)
                window.task.add_done_callback(work.task_done)
                return window
            except BaseException as exc:
                # Validation/slot refusal never dispatched a display. Cancellation
                # while display creation is queued cannot establish that fact.
                if not work.display_started and not isinstance(exc, asyncio.CancelledError):
                    work.display_cleaned = True
                work.task_done()
                raise
            finally:
                current.reset(token)

        return await asyncio.shield(asyncio.create_task(admitted()))

    windows.start = start
    windows.runner = admitted_runner
    windows._in_thread = admitted_thread
    windows.start_display = admitted_display
    windows.stop_display = cleaned_display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=ROOTS)
    parser.add_argument("action", choices=("status", "hold", "drain", "release"))
    parser.add_argument("--operation", default="")
    parser.add_argument("--boot", default="")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        gate = Gate(Path(ROOTS[args.scope]), args.scope)
        if args.action == "hold":
            gate.hold(args.operation)
            result = gate.status()
        elif args.action == "drain":
            result = gate.drain(args.operation, args.timeout)
        elif args.action == "release":
            result = gate.release(args.operation, args.boot)
        else:
            result = gate.status()
        print(json.dumps(result))
        return 0
    except (Busy, ValueError, OSError, KeyError, TypeError):
        print("Deployment hold unresolved; preserve original work", file=__import__("sys").stderr)
        return 73


# Relay's pinned implementation follows windows in a background thread and
# retires expired links in separate threads. HTTP request counts alone cannot
# cover the final owner/account binding. Keep its whole attempt under a lease.
from signin_window.relay import Relay  # noqa: E402


class DeploymentRelay(Relay):
    def __init__(self, *args, deployment, **kwargs):
        import threading

        super().__init__(*args, **kwargs)
        self.deployment = deployment
        self._deployment_mutex = threading.RLock()
        self._deployment_tickets = {}

    def _deployment_done(self, link):
        if link.window_id is None and (link.closed or link.state == "connected"):
            ticket = self._deployment_tickets.pop(link.id, None)
            if ticket is not None:
                ticket.close()

    def create(self, *args, **kwargs):
        ticket = self.deployment.ticket()
        with self._deployment_mutex:
            try:
                link = super().create(*args, **kwargs)
                self._deployment_tickets[link.id] = ticket
                return link
            except BaseException:
                ticket.close()
                raise

    def start(self, link, layout):
        with self._deployment_mutex:
            if link.id not in self._deployment_tickets:
                return  # An attempt without its original admission is never resumed.
            super().start(link, layout)
            self._deployment_done(link)

    def advance(self, link):
        with self._deployment_mutex:
            super().advance(link)
            self._deployment_done(link)

    def cancel(self, link):
        with self._deployment_mutex:
            super().cancel(link)
            self._deployment_done(link)

    def _retire(self, link):
        with self._deployment_mutex:
            super()._retire(link)
            self._deployment_done(link)


if __name__ == "__main__":
    raise SystemExit(main())
