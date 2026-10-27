"""The one way to the login service (``photos_login``) beside this server: signin-window's client (the service's API
with its bearer, noVNC's files and a window's live view) plus the service's own account routes. It never reaches
Google; every URL it builds is ``PHOTOS_LOGIN_URL`` plus a fixed path and validated ids."""

from __future__ import annotations

from signin_window.client import WINDOW_ID, LoginBusy, LoginUnavailable
from signin_window.client import LoginClient as _Client

from .accounts import valid_id

__all__ = ["LoginBusy", "LoginClient", "LoginUnavailable"]


class LoginClient(_Client):
    @staticmethod
    def _account(account: str) -> str:
        if not valid_id(account):
            raise LoginUnavailable("bad account")
        return account

    def refresh(self, account: str) -> None:
        """Ask the keeper to look at this account's browser profile soon (Google ended a generation)."""
        status, _ = self.request("POST", f"/accounts/{self._account(account)}/refresh")
        if status not in (202, 404):
            raise LoginUnavailable(f"refresh {status}")

    def forget(self, account: str) -> None:
        """Delete this account's browser profile and snapshot (the account is removed)."""
        status, _ = self.request("DELETE", f"/accounts/{self._account(account)}")
        if status not in (204, 404):
            raise LoginUnavailable(f"forget {status}")


class RoutedLoginClient:
    """Keep the native default profile with its existing custodian."""

    def __init__(self, linked, default, account):
        self.linked, self.default, self.account = linked, default, account

    def _route(self, window_id):
        if not isinstance(window_id, str) or not WINDOW_ID.fullmatch(window_id):
            raise LoginUnavailable("bad id")
        if window_id.startswith("m_"):
            identifier = window_id[2:]
            if not WINDOW_ID.fullmatch(identifier):
                raise LoginUnavailable("bad id")
            return self.default, identifier
        if window_id.startswith("l_") and WINDOW_ID.fullmatch(window_id[2:]):
            return self.linked, window_id[2:]
        raise LoginUnavailable("unknown id namespace")

    def start(self, layout="desktop", **params):
        native = params.get("account") == self.account
        result = (self.default if native else self.linked).start(layout, **params)
        identifier = result.get("id")
        if not isinstance(identifier, str) or len(identifier) > 38 or not WINDOW_ID.fullmatch(identifier):
            raise LoginUnavailable("bad id")
        return {**result, "id": ("m_" if native else "l_") + identifier}

    def state(self, window_id):
        backend, identifier = self._route(window_id)
        result = backend.state(identifier)
        return {**result, "id": window_id} if "id" in result else result

    def result(self, window_id):
        backend, identifier = self._route(window_id)
        return None if backend is self.default else backend.result(identifier)

    def close(self, window_id):
        backend, identifier = self._route(window_id)
        backend.close(identifier)

    def static(self, path, window_id=None):
        backend, identifier = self._route(window_id) if window_id else (self.linked, None)
        return backend.static(path, identifier)

    async def bridge(self, window_id, view_token, ws):
        backend, identifier = self._route(window_id)
        await backend.bridge(identifier, view_token, ws)

    def refresh(self, account):
        (self.default if account == self.account else self.linked).refresh(account)

    def forget(self, account):
        (self.default if account == self.account else self.linked).forget(account)
