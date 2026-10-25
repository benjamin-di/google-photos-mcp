"""Real supervised stop: preserve the profile, interrupted hold and attempt."""

import subprocess
import sys
import time

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)
pytest.importorskip("patchright.sync_api")

from signin_window.automation.runtime import open_browser, virtual_display

from photos_maintenance.cli import stores

pytestmark = pytest.mark.browser

PROGRAM = """
import sys, time
from pathlib import Path
from signin_window.automation.runtime import open_browser, virtual_display
from photos_maintenance import cli
from photos_maintenance.outcomes import HoldReason
root=Path(sys.argv[1])
def hold_open():
    state,budget=cli.stores(root)
    with cli.lease(root):
        assert budget.reserve('google', limit=1)
        state.record_hold('google', HoldReason.SIGN_IN_INTERRUPTED)
        profile = root/'browser-profiles'/'google'/'default'
        with virtual_display() as display, open_browser(profile, display=display) as browser:
            browser.context.add_cookies([{'name':'synthetic','value':'retained','url':'https://auth.example.com','expires':time.time()+3600}])
            (root/'ready').write_text('ready')
            while True:
                time.sleep(1)
cli.main=hold_open
cli.run()
"""


def test_terminated_maintenance_closes_owned_browser_without_refunding(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", PROGRAM, str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (tmp_path / "ready").exists(), "the synthetic maintenance browser did not start"
        process.terminate()
        _, error = process.communicate(timeout=25)
        assert process.returncode == 143, error.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    state, budget = stores(tmp_path)
    assert state.load("google").hold.reason.value == "sign_in_interrupted"
    assert not budget.reserve("google", limit=1)
    profile = tmp_path / "browser-profiles" / "google" / "default"
    assert not (profile / "SingletonLock").exists()
    with virtual_display() as display, open_browser(profile, display=display) as browser:
        assert any(cookie["name"] == "synthetic" and cookie["value"] == "retained" for cookie in browser.cookies())
