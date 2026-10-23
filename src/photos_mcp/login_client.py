"""The one way to the login service (``photos_login``) beside this server: signin-window's client (the service's API
with its bearer, noVNC's files and a window's live view) plus the service's own account routes. It never reaches
Google; every URL it builds is ``PHOTOS_LOGIN_URL`` plus a fixed path and validated ids."""

from __future__ import annotations

from signin_window.client import LoginBusy, LoginUnavailable
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
