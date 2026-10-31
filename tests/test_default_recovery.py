"""Native default recovery uses synthetic profiles and never contacts Google."""

import json
import multiprocessing
import os
import socket
import threading
import time
from datetime import UTC, datetime

import pytest
from signin_window.state import atomic_private_write
from starlette.testclient import TestClient

from photos_login import window
from photos_maintenance.cli import lease, stores
from photos_maintenance.human import Config, DefaultSignIn
from photos_maintenance.human import build_app as human_app
from photos_maintenance.human import main as human_main
from photos_maintenance.human_state import FILES, NativePromotion, target_store
from photos_maintenance.outcomes import BlockReason, HoldReason
from photos_mcp.connection_controls import ConnectionAdmission, publication_pending
from photos_mcp.connections_auth import BrowserStore
from photos_mcp.errors import PhotosError
from photos_mcp.login_client import LoginUnavailable, RoutedLoginClient
from photos_mcp.session import parse_cookies_txt, read_snapshot
from photos_mcp.session_export import build_meta, publish


def snapshot(folder, generation, email="owner@example.com"):
    meta = build_meta(
        generation=generation, email=email, user_agent="Chrome/142.0.0.0", headers=None, verified_at=datetime.now(UTC)
    )
    cookies = parse_cookies_txt(".google.com\tTRUE\t/\tTRUE\t0\tSID\tfake-cookie\n")
    publish(folder, cookies, meta)


@pytest.fixture
def native(tmp_path):
    config = Config("main", tmp_path / "maintenance", tmp_path / "export", tmp_path / "controls", "owner@example.com")
    control = BrowserStore(config.controls, track_location=False)
    control.set_control("main", 0)
    promotion = NativePromotion(config.root, config.export, config.guard())
    promotion.profile.mkdir(mode=0o700, parents=True)
    (promotion.profile / "identity").write_text("accepted native profile")
    config.export.mkdir(mode=0o700)
    snapshot(config.export, 7)
    state, budget = stores(config.root)
    state.record_hold("google", HoldReason.SIGN_IN_BLOCKED)
    state.record_block("google", BlockReason.CAPTCHA)
    assert budget.reserve("google", limit=3)
    return config, control, promotion


class Login:
    def __init__(self, config):
        self.params = dict(epoch=config.guard().check(), taken=[])
        self.layout, self.deadline = "desktop", time.monotonic() + 30
        self.cancelled = threading.Event()
        self.state = "starting"

    def env(self):
        return {}

    def at(self, host):
        pass

    def waiting(self):
        self.state = "waiting"

    def saving(self):
        self.state = "saving"

    def report(self, **kwargs):
        pass

    def finish(self, outcome):
        self.state = outcome


def accepted_bytes(promotion):
    return {name: (promotion.export / name).read_bytes() for name in FILES if (promotion.export / name).exists()}


def verifier(*, profile, session_dir, email, min_generation, on_state, **kwargs):
    (profile / "identity").write_text("manually verified candidate")
    on_state("waiting", "accounts.google.com")
    on_state("saving", "photos.google.com")
    snapshot(session_dir, min_generation + 1, email)
    return {"email": email, "generation": min_generation + 1}


def test_manual_default_success_preserves_accounting_and_native_paths(native):
    config, _, promotion = native
    before = target_store(config.root).load("google")
    ledger = config.root / "session-keeper" / "google.json"
    consumed = ledger.read_bytes()
    login = Login(config)
    DefaultSignIn(config, sign_in=verifier, require_policy=False)(login)
    assert login.state == "connected"
    assert (promotion.profile / "identity").read_text() == "manually verified candidate"
    assert read_snapshot(config.export).generation == 8
    after = target_store(config.root).load("google")
    assert after.hold is None and after.last_generation == 8
    assert (after.cooldown, after.consecutive_blocks) == (before.cooldown, before.consecutive_blocks)
    assert ledger.read_bytes() == consumed
    assert not promotion.journal.exists() and not publication_pending(config.guard().store, "main")


@pytest.mark.parametrize("outcome", ["wrong", "cancel", "cancel_signal", "error", "disconnect", "new_epoch"])
def test_failed_manual_attempt_never_changes_accepted_credentials_or_holds(native, outcome):
    config, control, promotion = native
    original = accepted_bytes(promotion)
    before = target_store(config.root).load("google")

    def attempt(**kwargs):
        result = verifier(**kwargs)
        if outcome == "wrong":
            snapshot(kwargs["session_dir"], 8, "different@example.com")
            result["email"] = "different@example.com"
        elif outcome == "cancel":
            raise window.Cancelled()
        elif outcome == "cancel_signal":
            login.cancelled.set()
        elif outcome == "error":
            raise RuntimeError("synthetic verifier failure")
        else:
            control.set_control("main", 1 if outcome == "disconnect" else 0)
        return result

    login = Login(config)
    DefaultSignIn(config, sign_in=attempt, require_policy=False)(login)
    assert login.state != "connected"
    assert (promotion.profile / "identity").read_text() == "accepted native profile"
    assert accepted_bytes(promotion) == original
    assert target_store(config.root).load("google") == before


def test_manual_proof_cannot_clear_a_replacement_hold(native):
    config, _, _ = native
    state = target_store(config.root)

    def attempt(**kwargs):
        state.record_hold("google", HoldReason.ACCOUNT_MISMATCH)
        return verifier(**kwargs)

    login = Login(config)
    DefaultSignIn(config, sign_in=attempt, require_policy=False)(login)
    assert login.state == "connected"
    assert state.load("google").hold.reason == HoldReason.ACCOUNT_MISMATCH


@pytest.mark.parametrize("host", [None, "unverified-host"])
def test_native_profile_must_be_proven_cold_before_copying(native, host):
    config, _, promotion = native
    (promotion.profile / "SingletonLock").symlink_to(f"{host or socket.gethostname()}-{os.getpid()}")
    calls = []
    login = Login(config)
    DefaultSignIn(config, sign_in=lambda **kw: calls.append(kw), require_policy=False)(login)
    assert login.state == "busy" and not calls
    assert (promotion.profile / "identity").read_text() == "accepted native profile"


def test_cold_clone_cannot_follow_links_into_another_profile(native):
    config, _, promotion = native
    other = config.root / "other-private-state"
    other.write_text("synthetic other account")
    (promotion.profile / "unexpected-link").symlink_to(other)
    calls = []
    login = Login(config)
    DefaultSignIn(config, sign_in=lambda **kw: calls.append(kw), require_policy=False)(login)
    assert login.state == "failed" and not calls
    assert other.read_text() == "synthetic other account"
    assert (promotion.profile / "identity").read_text() == "accepted native profile"


def crash_commit(config, point):
    promotion = NativePromotion(config.root, config.export, config.guard())
    with lease(config.root):
        promotion.stage()
        (promotion.candidate / "identity").write_text("new profile")
        snapshot(promotion.snapshot, 8)
        (promotion.snapshot / "keeper.json").write_text(
            json.dumps({"state": "signed_in", "email": config.expected_email})
        )

        def checkpoint(phase):
            if phase == point:
                os._exit(83)

        promotion.promote(
            config.guard(),
            config.guard().check(),
            generation=8,
            hold_id=target_store(config.root).load("google").hold.hold_id,
            checkpoint=checkpoint,
        )


@pytest.mark.parametrize(
    "point", ["prepared", "profile", "cookies.txt", "meta.json", "keeper.json", "accounting", "committed"]
)
def test_real_process_crash_recovers_both_native_paths_before_admitting_reader(native, point):
    config, control, promotion = native
    original = accepted_bytes(promotion)
    child = multiprocessing.get_context("fork").Process(target=crash_commit, args=(config, point))
    child.start()
    child.join(10)
    assert child.exitcode == 83
    assert publication_pending(control, "main")
    with pytest.raises(PhotosError, match="connection_disconnected"), ConnectionAdmission(control).operation("main"):
        pass
    # A later disconnect must never be undone by filesystem recovery.
    control.set_control("main", 1)
    with lease(config.root):
        promotion.recover()
    assert control.control("main")[1] == 1
    assert not publication_pending(control, "main")
    if point == "committed":
        assert (promotion.profile / "identity").read_text() == "new profile"
        assert read_snapshot(config.export).generation == 8
        assert target_store(config.root).load("google").last_generation == 8
    else:
        assert (promotion.profile / "identity").read_text() == "accepted native profile"
        assert accepted_bytes(promotion) == original
        assert target_store(config.root).load("google").last_generation == 0


def test_unknown_pending_marker_is_never_cleared_without_its_journal(native):
    config, control, promotion = native
    marker = control.path.parent / "native-publication-main.json"
    atomic_private_write(marker, json.dumps({"id": "unrecognized", "phase": "prepared"}).encode())
    with lease(config.root), pytest.raises(ValueError, match="no matching journal"):
        promotion.recover()
    assert marker.is_file()


def test_committed_cleanup_under_new_epoch_cannot_rewrite_proof_accounting_or_controls(native):
    config, control, promotion = native
    child = multiprocessing.get_context("fork").Process(target=crash_commit, args=(config, "committed"))
    child.start()
    child.join(10)
    assert child.exitcode == 83
    saved = read_snapshot(config.export)
    state = target_store(config.root)
    assert state.load("google").last_published_at.timestamp() == saved.verified_at
    state.record_hold("google", HoldReason.CHALLENGE)
    control.set_control("main", 1)
    target_bytes = promotion.target.read_bytes()
    snapshot_bytes = accepted_bytes(promotion)
    with lease(config.root):
        promotion.recover()
    assert promotion.target.read_bytes() == target_bytes
    assert accepted_bytes(promotion) == snapshot_bytes
    assert control.control("main")[1] == 1
    assert not publication_pending(control, "main")


def test_committed_recovery_keeps_reader_closed_if_saved_snapshot_is_corrupt(native):
    config, control, promotion = native
    child = multiprocessing.get_context("fork").Process(target=crash_commit, args=(config, "committed"))
    child.start()
    child.join(10)
    assert child.exitcode == 83
    (config.export / "cookies.txt").write_text("corrupt synthetic snapshot")
    with lease(config.root), pytest.raises(ValueError, match="cannot be verified"):
        promotion.recover()
    assert publication_pending(control, "main") and promotion.journal.is_file()


def test_pending_crash_recovers_before_a_disconnected_native_keeper_can_warm(native, monkeypatch):
    from photos_maintenance.cli import build_keeper
    from photos_maintenance.outcomes import IdentityOutcome

    config, control, promotion = native
    child = multiprocessing.get_context("fork").Process(target=crash_commit, args=(config, "meta.json"))
    child.start()
    child.join(10)
    assert child.exitcode == 83
    control.set_control("main", 1)
    monkeypatch.setenv("PHOTOS_CONNECTIONS_CONTROLS_DIR", str(config.controls))
    monkeypatch.setenv("PHOTOS_CONNECTIONS_ACCOUNT", "main")
    keeper = build_keeper(config.root, config.export, config.expected_email, "")
    assert keeper.run(allow_credential_mint=False).outcome == IdentityOutcome.DISCONNECTED
    assert (promotion.profile / "identity").read_text() == "accepted native profile"
    assert read_snapshot(config.export).generation == 7
    assert not publication_pending(control, "main")


def test_native_positive_signout_updates_owner_evidence_without_changing_snapshot_or_budget(native):
    from contextlib import nullcontext

    from photos_maintenance.keeper import IdentityKeeper
    from photos_maintenance.models import ProviderReadiness
    from photos_maintenance.outcomes import IdentityOutcome, SessionDead

    config, _, promotion = native
    state, budget = stores(config.root)
    state.clear_hold("google", state.load("google").hold.hold_id)
    state.record_publish("google", 7)
    accepted = accepted_bytes(promotion)
    ledger = (config.root / "session-keeper" / "google.json").read_bytes()

    class Browser:
        def warm(self):
            raise SessionDead()

    keeper = IdentityKeeper(
        target="google",
        expected_email=config.expected_email,
        export_dir=config.export,
        state=state,
        budget=budget,
        readiness=ProviderReadiness,
        open_browser=lambda: nullcontext(Browser()),
        lease=lambda: lease(config.root),
        utc_now=lambda: datetime.now(UTC),
        connection_guard=config.guard(),
    )
    result = keeper.run(allow_credential_mint=False)
    assert result.outcome == IdentityOutcome.MINT_NOT_PERMITTED
    report = json.loads((config.export / "keeper.json").read_text())
    assert report["state"] == "signed_out" and report["generation"] == 7
    assert all((config.export / name).read_bytes() == payload for name, payload in accepted.items())
    assert (config.root / "session-keeper" / "google.json").read_bytes() == ledger


def test_private_human_api_fixes_account_and_retains_profile_on_forget(native, monkeypatch):
    from signin_window.service import Windows

    config, control, promotion = native
    monkeypatch.setattr(Windows, "start_display", lambda self, login: None)
    monkeypatch.setattr(Windows, "stop_display", lambda self, login: None)
    token = "synthetic-service-token" * 3
    app = human_app(config, token=token, novnc_dir=config.root, sign_in=verifier, require_policy=False)
    body = dict(account="main", email=config.expected_email, taken=[], epoch=control.control("main")[0])
    auth = {"Authorization": "Bearer " + token}
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/logins", json=body).status_code == 401
        for mutation in ({"account": "other"}, {"email": "other@example.com"}, {"epoch": "old"}):
            assert client.post("/logins", json={**body, **mutation}, headers=auth).status_code == 400
        created = client.post("/logins", json={**body, "root": "/untrusted-override"}, headers=auth)
        assert created.status_code == 201
        identifier = created.json()["id"]
        deadline = time.monotonic() + 5
        while (state := client.get("/logins/" + identifier, headers=auth).json())["state"] in (
            "starting",
            "waiting",
            "saving",
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert state["state"] == "connected", state
        assert "fake-cookie" not in json.dumps(state)
        accepted = accepted_bytes(promotion)
        assert client.delete("/accounts/main", headers=auth).status_code == 204
        assert client.delete("/accounts/other", headers=auth).status_code == 404
        assert accepted_bytes(promotion) == accepted
        assert (promotion.profile / "identity").read_text() == "manually verified candidate"


def test_recover_only_cli_uses_fixed_contract_without_opening_a_listener(native, monkeypatch, capsys):
    config, _, _ = native
    values = dict(
        ACCOUNT=config.account,
        ROOT=str(config.root),
        EXPORT_DIR=str(config.export),
        CONTROLS_DIR=str(config.controls),
        EXPECTED_EMAIL=config.expected_email,
    )
    for name, value in values.items():
        monkeypatch.setenv("PHOTOS_DEFAULT_LOGIN_" + name, value)
    monkeypatch.delenv("PHOTOS_LOGIN_TOKEN", raising=False)
    from test_deployment import provision

    provision(config.root, "maintenance")
    human_main(["--recover-only"])
    assert json.loads(capsys.readouterr().out) == {"status": "recovered"}


def test_native_acknowledge_recovers_interrupted_accounting_before_clearing_exact_hold(native, monkeypatch, capsys):
    from photos_maintenance.cli import main

    config, control, promotion = native
    from test_deployment import provision

    provision(config.root, "maintenance")
    hold_id = target_store(config.root).load("google").hold.hold_id
    consumed = (config.root / "session-keeper" / "google.json").read_bytes()
    child = multiprocessing.get_context("fork").Process(target=crash_commit, args=(config, "accounting"))
    child.start()
    child.join(10)
    assert child.exitcode == 83
    monkeypatch.setenv("PHOTOS_CONNECTIONS_CONTROLS_DIR", str(config.controls))
    monkeypatch.setenv("PHOTOS_CONNECTIONS_ACCOUNT", "main")
    monkeypatch.delenv("PHOTOS_MAINTENANCE_EXPORT_DIR", raising=False)
    assert main(["acknowledge", "--root", str(config.root), "--hold-id", hold_id]) == 1
    capsys.readouterr()
    assert publication_pending(control, "main")
    assert (
        main(["acknowledge", "--root", str(config.root), "--export-dir", str(config.export), "--hold-id", hold_id]) == 0
    )
    assert json.loads(capsys.readouterr().out)["cleared"] is True
    assert read_snapshot(config.export).generation == 7
    assert (promotion.profile / "identity").read_text() == "accepted native profile"
    state = target_store(config.root).load("google")
    assert state.last_generation == 0 and state.hold is None
    assert state.cooldown is not None
    assert (config.root / "session-keeper" / "google.json").read_bytes() == consumed
    assert not publication_pending(control, "main")


class Backend:
    def __init__(self, identifier):
        self.identifier, self.calls = identifier, []

    def start(self, layout, **params):
        self.calls.append(("start", params))
        return {"id": self.identifier, "view_token": "synthetic-view-token"}

    def state(self, identifier):
        self.calls.append(("state", identifier))
        return {"id": identifier, "state": "waiting"}

    def close(self, identifier):
        self.calls.append(("close", identifier))

    def static(self, path, identifier):
        self.calls.append(("static", path, identifier))
        return 200, b"synthetic", "text/plain"

    def result(self, identifier):
        self.calls.append(("result", identifier))
        return {"must_never_reach_reader": True}

    def forget(self, account):
        self.calls.append(("forget", account))

    def refresh(self, account):
        self.calls.append(("refresh", account))

    async def bridge(self, identifier, token, ws):
        self.calls.append(("bridge", identifier, token))


def test_routing_namespaces_cannot_collide_and_kept_default_never_hands_over_a_session():
    import asyncio

    linked, default = Backend("m_" + "a" * 22), Backend("b" * 24)
    routed = RoutedLoginClient(linked, default, "main")
    native_id = routed.start(account="main")["id"]
    linked_id = routed.start(account="other")["id"]
    assert native_id == "m_" + default.identifier and linked_id == "l_" + linked.identifier
    assert len(native_id) <= 40 and len(linked_id) <= 40
    for identifier, backend in ((native_id, default), (linked_id, linked)):
        assert routed.state(identifier)["id"] == identifier
        routed.close(identifier)
        routed.static("vnc.html", identifier)
        asyncio.run(routed.bridge(identifier, "fake-view-token", None))
        assert ("state", backend.identifier) in backend.calls
        assert ("static", "vnc.html", backend.identifier) in backend.calls
    assert routed.result(native_id) is None
    assert not any(call[0] == "result" for call in default.calls)
    routed.forget("main")
    routed.refresh("other")
    assert ("forget", "main") in default.calls and ("refresh", "other") in linked.calls
    for invalid in ("m_" + "a" * 14, "unknown-namespace-000000", "../untrusted"):
        with pytest.raises(LoginUnavailable):
            routed.state(invalid)
