"""Authority activation is separate from permission to attempt a sign-in."""

import pytest

from photos_mcp.connection_controls import ConnectionSuspended, PublicationGuard
from photos_mcp.connections_auth import BrowserStore


@pytest.mark.parametrize("activate", [False, True])
def test_only_verified_activation_can_publish_from_suspension(tmp_path, activate):
    controls = BrowserStore(tmp_path / "controls")
    controls.set_control("main", 1)
    epoch = controls.control("main")[0]
    guard = PublicationGuard(controls, "main")
    assert guard.attempt(epoch) == epoch
    with pytest.raises(ConnectionSuspended):
        guard.require_active()
    if not activate:
        with pytest.raises(ConnectionSuspended), guard.snapshot(epoch):
            pytest.fail("ordinary maintenance activated a reconnect")
        assert controls.control("main") == (epoch, 1)
    else:
        with guard.snapshot(epoch, activate=True):
            assert controls.control("main") == (epoch, 1)
        assert controls.control("main") == (epoch, 0)
        with guard.observe(epoch) as binding:
            assert binding


def test_failed_publication_and_stale_owner_cannot_activate(tmp_path):
    controls = BrowserStore(tmp_path / "controls")
    controls.set_control("main", 1)
    epoch = controls.control("main")[0]
    guard = PublicationGuard(controls, "main")
    with pytest.raises(OSError), guard.snapshot(epoch, activate=True):
        raise OSError("synthetic publication failure")
    assert controls.control("main") == (epoch, 1)
    with pytest.raises(ConnectionSuspended), guard.observe(epoch):
        pass
    controls.set_control("main", 1)
    current = controls.control("main")
    with pytest.raises(ConnectionSuspended), guard.snapshot(epoch, activate=True):
        pytest.fail("stale publisher activated the replacement attempt")
    assert controls.control("main") == current
    controls.set_control("main", 2)
    epoch = controls.control("main")[0]
    with pytest.raises(ConnectionSuspended):
        guard.attempt(epoch)
