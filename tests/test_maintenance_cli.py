"""Adoption retains authority brakes without contacting a site or secret backend."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from photos_maintenance import cli
from photos_maintenance.outcomes import HoldReason


def test_cli_adopts_hold_and_spent_budget_without_browser(tmp_path, monkeypatch, capsys):
    state, budget = cli.stores(tmp_path)
    hold = state.record_hold("google", HoldReason.SIGN_IN_INTERRUPTED)
    state.record_publish("google", 17)
    assert budget.reserve("google", limit=1)
    ledger = tmp_path / "session-keeper" / ("google" + ".json")
    before = ledger.read_bytes()
    profile = tmp_path / "browser-profiles" / "google" / "default"
    profile.mkdir(parents=True)
    marker = profile / "synthetic-profile"
    marker.write_text("retained")

    def forbidden(*args, **kwargs):
        pytest.fail("a held account must not open a browser")

    monkeypatch.setattr(cli, "open_google_browser", forbidden)
    code = cli.main(
        [
            "keep",
            "--root",
            str(tmp_path),
            "--export-dir",
            str(tmp_path / "export"),
            "--expected-email",
            "owner@example.com",
            "--allow-credential-mint",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["outcome"] == "held"
    assert result["hold_id"] == hold.hold_id
    assert ledger.read_bytes() == before
    assert state.load("google").last_generation == 17
    assert marker.read_text() == "retained"

    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    status = json.loads(capsys.readouterr().out)
    assert status["last_generation"] == 17 and status["next_sign_in_at"]
    assert cli.main(["acknowledge", "--root", str(tmp_path), "--hold-id", "hold-other"]) == 1
    capsys.readouterr()
    assert state.load("google").hold == hold
    assert cli.main(["acknowledge", "--root", str(tmp_path), "--hold-id", hold.hold_id]) == 0
    capsys.readouterr()
    assert state.load("google").hold is None
    assert not budget.reserve("google", limit=1)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("nonregular", ["fifo", "symlink", "malformed", "negative-generation"])
def test_cli_state_failure_is_closed_and_redacted(tmp_path, capsys, nonregular):
    root = tmp_path / "identity-targets"
    root.mkdir()
    path = root / ("google" + ".json")
    if nonregular == "fifo":
        os.mkfifo(path)
    elif nonregular == "symlink":
        path.symlink_to(tmp_path / "private-target")
    elif nonregular == "negative-generation":
        path.write_text('{"last_generation": -1}')
    else:
        path.write_text("owner@example.com private diagnostic text")
    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out) == {
        "target": "google",
        "outcome": "infrastructure_error",
        "reason": "configuration",
        "needs_attention": True,
    }
    assert "owner@example.com" not in captured.out


def test_cli_adopts_existing_attempt_file_without_refund(tmp_path, capsys):
    root = tmp_path / "session-keeper"
    root.mkdir()
    now = datetime.now(UTC)
    path = root / ("google" + ".json")
    path.write_text(json.dumps({"attempts": [(now - timedelta(minutes=5)).isoformat()]}))
    before = path.read_bytes()
    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert datetime.fromisoformat(report["next_sign_in_at"]) > now + timedelta(hours=23)
    assert path.read_bytes() == before
