"""Cooperative holds use synthetic authority and never contact a site."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from signin_window.service import Busy as WindowBusy
from signin_window.service import Windows

from photos_mcp.deployment import Admission, Busy, Gate, guarded_windows


def provision(root, scope):
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    value = dict(schema=1, scope=scope, operation="", completed=[], inflight=[], frozen=False)
    for name, data in (("control.json", json.dumps(value)), ("lock", ""), ("active", ""), ("owner", "")):
        path = root / (".deployment-" + name)
        path.write_text(data)
        path.chmod(0o600)
    return root


@pytest.fixture
def gate(tmp_path):
    value = Gate(provision(tmp_path, "login"), "login").start()
    yield value
    value.stop()


def test_busy_timeout_retains_original_hold_without_cancelling_work(gate):
    operation = str(uuid.uuid4())
    with gate.work():
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        assert gate.status()["operation"] == operation
        assert not gate.status()["idle"]
        with pytest.raises(Busy):
            gate.ticket()
        # An existing viewer can still finish while the window owns its ticket.
        with gate.ticket(continuation=True):
            pass
    frozen = gate.drain(operation, 0)
    assert frozen["idle"] and frozen["frozen"]
    with pytest.raises(Busy):
        gate.ticket(continuation=True)
    with pytest.raises(Busy):
        gate.release(operation, str(uuid.uuid4()))
    gate.release(operation, frozen["boot_id"])
    with pytest.raises(Busy):
        gate.hold(operation)
    with pytest.raises(Busy):
        gate.release(operation, frozen["boot_id"])


def test_successor_inherits_hold_and_requires_new_boot(gate):
    operation = str(uuid.uuid4())
    old = gate.drain(operation, 0)
    gate.stop()
    with pytest.raises(Busy):
        gate.status()
    successor = Gate(gate.root, "login").start()
    try:
        assert successor.status()["boot_id"] != old["boot_id"]
        assert successor.status()["operation"] == operation
        with pytest.raises(Busy):
            successor.ticket()
        with pytest.raises(Busy):
            successor.release(operation, old["boot_id"])
    finally:
        successor.stop()


def test_cross_process_work_and_original_operation_binding(gate):
    script = """import sys,time
from pathlib import Path
from photos_mcp.deployment import Gate
with Gate(Path(sys.argv[1]), "login").work():
 print("admitted", flush=True)
 sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(gate.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ,
    )
    try:
        assert child.stdout.readline().strip() == "admitted"
        operation = str(uuid.uuid4())
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        with pytest.raises(Busy):
            gate.hold(str(uuid.uuid4()))
        assert child.poll() is None
        child.communicate("finish\n", timeout=10)
        assert gate.drain(operation, 0)["idle"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_missing_or_corrupt_protocol_never_initializes_state(tmp_path):
    before = list(tmp_path.iterdir())
    with pytest.raises(OSError):
        Gate(tmp_path, "reader")
    assert list(tmp_path.iterdir()) == before
    provision(tmp_path, "reader")
    path = tmp_path / ".deployment-control.json"
    path.write_text("damaged")
    saved = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        Gate(tmp_path, "reader")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == saved


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_asgi_lifetime_is_held_until_original_work_finishes(gate, kind):
    async def scenario():
        arrived, finish = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            arrived.set()
            await finish.wait()

        async def receive():
            return {}

        async def send(message):
            pass

        task = asyncio.create_task(Admission(app, gate)({"type": kind, "path": "/uploads/fake"}, receive, send))
        await arrived.wait()
        # A disconnected client cannot remove a live provider worker's lease.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operation = str(uuid.uuid4())
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        finish.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if gate.status()["idle"]:
                break
        assert gate.drain(operation, 0)["frozen"]

    asyncio.run(scenario())


def test_windows_cover_queued_runner_through_display_cleanup(gate):
    arrived, finish = threading.Event(), threading.Event()

    def runner(window):
        arrived.set()
        assert finish.wait(10)
        window.finish("connected")

    windows = Windows(runner)
    windows.start_display = lambda window: None
    windows.stop_display = lambda window: None
    guarded_windows(windows, gate)

    async def scenario():
        window = await windows.start(params={"key": "fake"})
        assert await asyncio.to_thread(arrived.wait, 5)
        with pytest.raises(WindowBusy):
            await windows.start(params={"key": "other"})
        operation = str(uuid.uuid4())
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        assert not window.cancelled.is_set()
        finish.set()
        await window.task
        await asyncio.sleep(0)
        assert gate.drain(operation, 0)["idle"]

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["reader", "login", "maintenance"])
@pytest.mark.parametrize("outcome", ["published", "failed"])
def test_double_cancellation_cannot_drain_a_surviving_window_writer(tmp_path, scope, outcome):
    gate = Gate(provision(tmp_path / "control", scope), scope).start()
    saving, cleanup, cleaned, publication, published, exit_runner, ended = (threading.Event() for _ in range(7))
    snapshot = tmp_path / "fake-snapshot"
    scratch = tmp_path / "fake-scratch"
    display_stopped = threading.Event()

    def runner(window):
        try:
            scratch.write_text("fake-temporary-state")
            window.saving()
            saving.set()
            assert cleanup.wait(5)
            assert window.cancelled.is_set()
            scratch.unlink()
            cleaned.set()
            assert publication.wait(5)
            snapshot.write_text("fake-snapshot" if outcome == "published" else "fake-recovery")
            published.set()
            assert exit_runner.wait(5)
            if outcome == "failed":
                raise RuntimeError("fake-publication-failure")
            window.finish("connected")
        finally:
            ended.set()

    # Exercise the locked dependency's actual watchdog, thread runner and shutdown.
    windows = Windows(runner, save_seconds=0.01)
    windows.start_display = lambda window: None
    windows.stop_display = lambda window: display_stopped.set()
    guarded_windows(windows, gate)
    operation = str(uuid.uuid4())

    def held():
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        status = gate.status()
        assert status["operation"] == operation and not status["idle"] and not status["frozen"]
        assert len(gate.read()["inflight"]) == 1
        with pytest.raises(Busy):
            gate.ticket()

    async def scenario():
        try:
            window = await windows.start(params={"key": "fake"})
            assert await asyncio.to_thread(saving.wait, 5)
            window.saving_at = time.monotonic() - windows.save_seconds - 1
            with windows._guard:
                windows._reap()
            await asyncio.sleep(0)  # First cancel enters _in_thread's wait for the writer.
            assert window.cancelled.is_set() and not window.task.done()
            held()
            await asyncio.wait_for(windows.close_all(), 2)  # Lifespan sends the second cancel.
            assert window.task.done() and window.task.cancelling() == 2
            assert window.state == "cancelled"
            assert display_stopped.is_set() and not ended.is_set()
            assert scratch.exists() and not snapshot.exists()
            held()
            cleanup.set()
            assert await asyncio.to_thread(cleaned.wait, 5)
            assert not scratch.exists() and not snapshot.exists()
            held()
            publication.set()
            assert await asyncio.to_thread(published.wait, 5)
            assert snapshot.read_text() == ("fake-snapshot" if outcome == "published" else "fake-recovery")
            assert not ended.is_set()
            held()  # Even completed publication does not prove the writer has exited.
            exit_runner.set()
            assert await asyncio.to_thread(ended.wait, 5)
            async with asyncio.timeout(2):
                while not gate.status()["idle"]:
                    await asyncio.sleep(0)
            assert window.state == "cancelled"  # Admission does not rewrite the service's outcome.
            assert gate.drain(operation, 0)["frozen"]
        finally:
            cleanup.set()
            publication.set()
            exit_runner.set()

    try:
        asyncio.run(scenario())
    finally:
        gate.stop()


def test_cancelled_window_retains_a_runner_queued_in_the_executor(gate):
    unblock_executor, executor_busy, runner_started, exit_runner = (threading.Event() for _ in range(4))

    def runner(window):
        runner_started.set()
        assert window.cancelled.is_set()
        assert exit_runner.wait(5)

    async def scenario():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        dispatched = asyncio.Event()

        def occupy_executor():
            executor_busy.set()
            assert unblock_executor.wait(5)

        windows = Windows(runner)
        in_thread = windows._in_thread

        async def observe_dispatch(window):
            dispatched.set()
            return await in_thread(window)

        windows._in_thread = observe_dispatch
        windows.start_display = lambda window: executor.submit(occupy_executor)
        windows.stop_display = lambda window: None
        guarded_windows(windows, gate)
        operation = str(uuid.uuid4())
        try:
            window = await windows.start()
            await asyncio.wait_for(dispatched.wait(), 2)
            await asyncio.sleep(0)
            assert executor_busy.wait(2) and not runner_started.is_set()
            windows._cancel(window)
            await asyncio.sleep(0)
            window.task.cancel()
            await window.task
            await asyncio.sleep(0)
            assert window.task.done() and not runner_started.is_set()
            with pytest.raises(Busy):
                gate.drain(operation, 0)
            unblock_executor.set()
            # The only executor thread now enters the original queued runner.
            async with asyncio.timeout(2):
                while not runner_started.is_set():
                    await asyncio.sleep(0)
            with pytest.raises(Busy):
                gate.drain(operation, 0)
            exit_runner.set()
        finally:
            unblock_executor.set()
            exit_runner.set()

    asyncio.run(scenario())
    # asyncio.run joins the executor and processes the actual exit notification.
    assert gate.drain(gate.status()["operation"], 0)["frozen"]


def test_runner_exit_after_loop_shutdown_keeps_durable_unknown_work(gate):
    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    saving = asyncio.Event()
    exit_runner = threading.Event()

    def runner(window):
        window.saving()
        loop.call_soon_threadsafe(saving.set)
        assert exit_runner.wait(5)
        window.finish("connected")

    windows = Windows(runner, save_seconds=0.01)
    windows.start_display = lambda window: None
    windows.stop_display = lambda window: None
    guarded_windows(windows, gate)
    operation = str(uuid.uuid4())

    async def scenario():
        window = await windows.start()
        await asyncio.wait_for(saving.wait(), 2)
        window.saving_at = time.monotonic() - windows.save_seconds - 1
        with windows._guard:
            windows._reap()
        await asyncio.sleep(0)
        await asyncio.wait_for(windows.close_all(), 2)
        assert window.task.done()
        # Event-loop shutdown can also cancel the worker future. It cannot stop
        # the synchronous runner or establish that its publication has ended.
        workers = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert workers
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        with pytest.raises(Busy):
            gate.drain(operation, 0)

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
        exit_runner.set()
        executor.shutdown(wait=True)
    # Even the runner's later return cannot fabricate a completion receipt on
    # a closed loop. A successor must inherit the unresolved original ticket.
    ticket = gate.read()["inflight"]
    assert len(ticket) == 1
    gate.stop()
    successor = Gate(gate.root, gate.scope).start()
    try:
        with pytest.raises(Busy):
            successor.drain(operation, 0)
        assert successor.read()["inflight"] == ticket
        assert not successor.status()["idle"] and not successor.status()["frozen"]
    finally:
        successor.stop()


@pytest.mark.parametrize("kind", ["async", "busy"])
def test_known_window_completion_without_a_synchronous_runner_retires_ticket(gate, kind):
    async def runner(window):
        window.finish("connected")

    lock = threading.Lock()
    if kind == "busy":
        lock.acquire()
    windows = Windows(runner, lock=lock, lock_wait=0.01)
    windows.start_display = lambda window: None
    windows.stop_display = lambda window: None
    guarded_windows(windows, gate)

    async def scenario():
        window = await windows.start()
        await window.task
        await asyncio.sleep(0)
        assert window.state == ("busy" if kind == "busy" else "connected")
        assert gate.drain(str(uuid.uuid4()), 0)["frozen"]

    try:
        asyncio.run(scenario())
    finally:
        if kind == "busy":
            lock.release()


@pytest.mark.parametrize("cleanup", ["confirmed", "raises", "unconfirmed"])
def test_window_requires_confirmed_display_exit_even_after_runner_return(gate, cleanup):
    runner_returned = threading.Event()

    class Display:
        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout):
            assert runner_returned.is_set()
            if cleanup == "raises":
                raise RuntimeError("fake-display-cleanup-failure")
            if cleanup == "unconfirmed":
                raise subprocess.TimeoutExpired("fake-display", timeout)
            return 0

        def poll(self):
            return 0 if cleanup == "confirmed" else None

    def runner(window):
        window.finish("connected")
        runner_returned.set()

    windows = Windows(runner)
    windows.start_display = lambda window: window.procs.append(Display())
    # Keep the pinned stop_display: it can clear handles after both waits time out.
    guarded_windows(windows, gate)

    async def scenario():
        window = await windows.start()
        await window.task
        await asyncio.sleep(0)
        assert runner_returned.is_set()
        if cleanup != "raises":
            assert not window.procs

    asyncio.run(scenario())
    operation = str(uuid.uuid4())
    if cleanup == "confirmed":
        assert gate.drain(operation, 0)["frozen"]
    else:
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        ticket = gate.read()["inflight"]
        assert len(ticket) == 1
        gate.stop()
        successor = Gate(gate.root, gate.scope).start()
        try:
            with pytest.raises(Busy):
                successor.drain(operation, 0)
            assert successor.read()["inflight"] == ticket
            assert not successor.status()["idle"] and not successor.status()["frozen"]
        finally:
            successor.stop()


def test_maintenance_hold_rejects_keep_and_acknowledge_before_loading_state(tmp_path, capsys):
    from photos_maintenance import cli

    gate = Gate(provision(tmp_path, "maintenance"), "maintenance").start()
    try:
        gate.drain(str(uuid.uuid4()), 0)
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        for action in ("keep", "acknowledge"):
            assert cli.main([action, "--root", str(tmp_path)]) == 73
        assert "deployment_hold" in capsys.readouterr().out
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    finally:
        gate.stop()


def test_reader_relay_lease_covers_background_final_binding(tmp_path):
    from photos_mcp.deployment import DeploymentRelay

    gate = Gate(provision(tmp_path, "reader"), "reader").start()
    arrived, finish = threading.Event(), threading.Event()

    class Backend:
        def start(self, layout, **params):
            return {"id": "fake-window", "view_token": "fake-view"}

        def state(self, window):
            return {"state": "connected"}

        def close(self, window):
            pytest.fail("deployment must not close an admitted window")

    def bound(link, result):
        arrived.set()
        assert finish.wait(10)
        return "connected"

    relay = DeploymentRelay(Backend(), "https://photos.example/connect", deployment=gate, on_ended=bound)
    try:
        link = relay.create("fake-account")
        relay.start(link, "desktop")
        worker = threading.Thread(target=relay.advance, args=(link,))
        worker.start()
        assert arrived.wait(5)
        operation = str(uuid.uuid4())
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        assert link.state == "starting"
        finish.set()
        worker.join(5)
        assert not worker.is_alive() and link.state == "connected"
        assert gate.drain(operation, 0)["frozen"]
    finally:
        finish.set()
        gate.stop()


def test_ready_relay_attempt_is_retained_until_natural_expiry(tmp_path):
    from photos_mcp.deployment import DeploymentRelay

    gate = Gate(provision(tmp_path, "reader"), "reader").start()
    now = [0]
    relay = DeploymentRelay(
        object(), "https://photos.example/connect", deployment=gate, clock=lambda: now[0], link_ttl=5
    )
    try:
        relay.create("fake-account")
        relay.tick()
        operation = str(uuid.uuid4())
        with pytest.raises(Busy):
            gate.drain(operation, 0)
        now[0] = 6
        relay.tick()
        # Original expiry retires in a thread; the drain never forces it.
        import time

        deadline = time.monotonic() + 5
        while not gate.status()["idle"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.drain(operation, 0)["frozen"]
    finally:
        gate.stop()


def test_worker_death_never_turns_unknown_browser_work_into_idle(gate):
    script = """import sys,os
from pathlib import Path
from photos_mcp.deployment import Gate
ticket=Gate(Path(sys.argv[1]), "login").ticket()
os._exit(83)
"""
    result = subprocess.run([sys.executable, "-c", script, str(gate.root)], check=False)
    assert result.returncode == 83
    operation = str(uuid.uuid4())
    with pytest.raises(Busy):
        gate.drain(operation, 0)
    gate.stop()
    successor = Gate(gate.root, "login").start()
    try:
        assert not successor.status()["idle"]
        with pytest.raises(Busy):
            successor.drain(operation, 0)
        assert len(json.loads((gate.root / ".deployment-control.json").read_text())["inflight"]) == 1
    finally:
        successor.stop()
