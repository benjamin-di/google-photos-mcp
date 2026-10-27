"""Private owner-driven recovery beside the sole native profile custodian.

Run in the maintenance container's hostname and PID namespace. No credential
reader, planner, provider token, or account filesystem is exposed over HTTP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from signin_window.automation.profiles import ProfileError
from signin_window.leases import LeaseBusyError
from signin_window.service import Windows
from signin_window.service import build_app as service_app
from signin_window.state import atomic_private_write
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from photos_login import window
from photos_mcp.accounts import valid_id
from photos_mcp.connection_controls import ConnectionSuspended, PublicationGuard
from photos_mcp.connections_auth import BrowserStore
from photos_mcp.session import read_snapshot
from photos_mcp.session_export import ExportError, published_generation

from .cli import lease
from .human_state import NativePromotion, target_store


@dataclass(frozen=True)
class Config:
    account: str
    root: Path
    export: Path
    controls: Path
    expected_email: str

    @classmethod
    def from_env(cls, env=os.environ):
        values = {
            key: env.get("PHOTOS_DEFAULT_LOGIN_" + key, "").strip()
            for key in ("ACCOUNT", "ROOT", "EXPORT_DIR", "CONTROLS_DIR", "EXPECTED_EMAIL")
        }
        if not valid_id(values["ACCOUNT"]) or "@" not in values["EXPECTED_EMAIL"]:
            raise ValueError("configure the fixed default account")
        paths = [Path(values[key]) for key in ("ROOT", "EXPORT_DIR", "CONTROLS_DIR")]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("configure absolute native paths")
        if not (paths[2] / "browser.sqlite3").is_file():
            raise ValueError("initialize the existing reader controls first")
        return cls(values["ACCOUNT"], *paths, values["EXPECTED_EMAIL"].lower())

    def guard(self):
        return PublicationGuard(BrowserStore(self.controls, track_location=False), self.account)


class DefaultSignIn:
    def __init__(self, config: Config, *, sign_in=window.sign_in, require_policy=True):
        self.config, self.sign_in, self.require_policy = config, sign_in, require_policy

    def __call__(self, login):
        config = self.config
        promotion = NativePromotion(config.root, config.export, config.guard())
        try:
            with lease(config.root):
                promotion.recover()
                epoch = login.params["epoch"]
                guard = config.guard()
                if guard.check() != epoch:
                    raise ConnectionSuspended()
                store = target_store(config.root)
                before = store.load("google")
                hold_id = before.hold.hold_id if before.hold else None
                promotion.stage()
                try:

                    def on_state(state, host):
                        if host:
                            login.at(host)
                        if state == "waiting":
                            login.waiting()
                        elif state == "saving":
                            login.saving()

                    result = self.sign_in(
                        profile=promotion.candidate,
                        session_dir=promotion.snapshot,
                        layout=login.layout,
                        env=login.env(),
                        email=config.expected_email,
                        taken=login.params["taken"],
                        min_generation=max(before.last_generation, published_generation(config.export)),
                        cancel=login.cancelled,
                        deadline=login.deadline,
                        on_state=on_state,
                        require_policy=self.require_policy,
                    )
                    # The runner's result is insufficient: re-read the independently verified snapshot.
                    saved = read_snapshot(promotion.snapshot)
                    if (
                        saved.email.lower() != config.expected_email
                        or result.get("email", "").lower() != saved.email.lower()
                    ):
                        raise window.Refused("wrong_account")
                    if saved.generation <= max(before.last_generation, published_generation(config.export)):
                        raise ValueError("stale manual publication")
                    if not -300 <= time.time() - saved.verified_at <= 300:
                        raise ValueError("manual verification is not fresh")
                    if login.cancelled.is_set():
                        raise window.Cancelled()
                    keeper = dict(
                        state="signed_in",
                        email=config.expected_email,
                        generation=saved.generation,
                        checked_at=int(time.time()),
                        error=None,
                    )
                    atomic_private_write(promotion.snapshot / "keeper.json", json.dumps(keeper).encode())

                    def validate():
                        if login.cancelled.is_set():
                            raise window.Cancelled()

                    promotion.promote(guard, epoch, generation=saved.generation, hold_id=hold_id, validate=validate)
                    login.report(account=config.account, email=config.expected_email)
                    login.finish("connected")
                finally:
                    promotion.discard()
        except (LeaseBusyError, ProfileError):
            login.finish("busy")
        except window.Refused as error:
            login.finish(error.code)
        except (window.Cancelled, window.WindowClosed, ConnectionSuspended):
            login.finish("cancelled")
        except ExportError as error:
            login.finish("empty_library" if error.code == "empty_library" else "failed")
        except Exception:
            login.finish("failed")


def build_app(config, *, token, novnc_dir, sign_in=window.sign_in, require_policy=True):
    guard = config.guard()

    def params(body):
        email = body.get("email")
        if (
            body.get("account") != config.account
            or not isinstance(email, str)
            or email.lower() != config.expected_email
        ):
            raise ValueError("invalid account")
        taken = body.get("taken", [])
        if (
            not isinstance(taken, list)
            or len(taken) > 100
            or any(not isinstance(email, str) or "@" not in email or len(email) > 254 for email in taken)
        ):
            raise ValueError("invalid account bindings")
        epoch = body.get("epoch")
        try:
            current = guard.check()
        except ConnectionSuspended:
            raise ValueError("disconnected connection") from None
        if not isinstance(epoch, str) or epoch != current:
            raise ValueError("stale connection")
        if require_policy and not window.policy_installed():
            raise ValueError("managed policy missing")
        return dict(
            account=config.account,
            key=config.account,
            epoch=epoch,
            taken=[email.strip().lower() for email in taken if email.strip().lower() != config.expected_email],
        )

    windows = Windows(
        DefaultSignIn(config, sign_in=sign_in, require_policy=require_policy),
        lock=threading.Lock(),
        slots=1,
        minutes=20,
        xvfb=os.environ.get("PHOTOS_LOGIN_XVFB", "Xvfb"),
        x11vnc=os.environ.get("PHOTOS_LOGIN_X11VNC", "x11vnc"),
    )

    async def forget(request):
        if request.path_params["account"] != config.account:
            return Response(status_code=404)
        await windows.close_key(config.account)

        def drain():
            with lease(config.root):
                NativePromotion(config.root, config.export, guard).recover()

        try:
            await asyncio.to_thread(drain)
        except LeaseBusyError:
            return JSONResponse({"error": "busy"}, status_code=503)
        return Response(status_code=204)  # Native accepted profile and snapshot are retained.

    app = service_app(
        windows,
        token=token,
        novnc_dir=novnc_dir,
        start_params=params,
        direct_view=False,
        routes=[Route("/accounts/{account}", forget, methods=["DELETE"])],
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await asyncio.to_thread(recover, config)
        async with original_lifespan(application) as state:
            yield state

    app.router.lifespan_context = lifespan
    return app


def recover(config):
    # Both supervised processes can start together; wait for the current lease owner.
    while True:
        try:
            with lease(config.root):
                NativePromotion(config.root, config.export, config.guard()).recover()
            return
        except LeaseBusyError:
            time.sleep(0.2)


def main(argv=None):
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--recover-only", action="store_true", help="recover the native journal and exit before serving"
    )
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.recover_only:
        recover(config)
        print('{"status":"recovered"}')
        return
    token = os.environ.get("PHOTOS_LOGIN_TOKEN", "")
    if len(token) < 32:
        raise ValueError("PHOTOS_LOGIN_TOKEN is required")
    app = build_app(config, token=token, novnc_dir=Path(os.environ.get("NOVNC_DIR", "/opt/novnc")))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PHOTOS_DEFAULT_LOGIN_PORT", "8081")),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
