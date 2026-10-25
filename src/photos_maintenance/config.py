"""Site controls are public; account references and launch location are runtime configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from signin_window.automation.auth import AuthObservationSpec
from signin_window.automation.google import GoogleIdentityConfig

from .models import FrozenModel


class BrowserLaunchSpec(FrozenModel):
    locale: str = "en-GB"
    timezone_id: str = "UTC"
    accept_language: str = "en-GB,en;q=0.9"
    args: tuple[str, ...] = ()


BrowserLaunchConfig = BrowserLaunchSpec


class GoogleIdentitySection(FrozenModel):
    controls: dict[str, tuple[str, ...]]
    observations: dict[str, AuthObservationSpec]


def _https_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("identity URLs must be absolute https URLs")
    return f"https://{parts.hostname}"


class IdentityTargetSpec(FrozenModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    secret_ref: str = Field(default="", max_length=2048, pattern=r"^[^\r\n\x00{}]*$")
    login_url: str
    allowed_origins: frozenset[str] = Field(min_length=1)
    identity_hosts: tuple[str, ...] = Field(min_length=1)
    warm_url: str
    image_host: str
    library_tile_selectors: tuple[str, ...] = Field(min_length=1)
    interstitial_path_prefixes: tuple[str, ...] = Field(min_length=1)
    interstitial_skip_selectors: tuple[str, ...] = Field(min_length=1)
    export_hosts: frozenset[str] = Field(min_length=1)
    export_names: frozenset[str] = Field(min_length=1)
    launch: BrowserLaunchSpec
    google_identity: GoogleIdentitySection

    @model_validator(mode="after")
    def validate_surface(self) -> IdentityTargetSpec:
        for origin in self.allowed_origins:
            if _https_origin(origin) != origin:
                raise ValueError("allowed origins must be bare https origins")
        for url in (self.login_url, self.warm_url):
            if _https_origin(url) not in self.allowed_origins:
                raise ValueError("identity URLs must use an allowed origin")
        if self.image_host not in self.export_hosts:
            raise ValueError("the image host must be an export host")
        for host in (*self.identity_hosts, *self.export_hosts):
            if not host or host != host.lower() or host.startswith("."):
                raise ValueError("hosts must be lowercase names without a dot")
        prefixes = self.interstitial_path_prefixes
        if any(not prefix.startswith("/") for prefix in prefixes):
            raise ValueError("interstitial paths must begin with a slash")
        selectors = (*self.library_tile_selectors, *self.interstitial_skip_selectors)
        names = tuple(self.export_names)
        if any(not value.strip() for value in (*selectors, *names)):
            raise ValueError("identity selectors and cookie names cannot be blank")
        # Constructing the driver config enforces the driver's exact contract.
        self.identity_config()
        return self

    @property
    def warm_host(self) -> str:
        return urlsplit(self.warm_url).hostname or ""

    def identity_config(self) -> GoogleIdentityConfig:
        return GoogleIdentityConfig(
            provider_key=self.key,
            controls=dict(self.google_identity.controls),
            observations=dict(self.google_identity.observations),
            secret_ref=self.secret_ref or "unconfigured",
            allowed_origins=self.allowed_origins,
            identity_hosts=self.identity_hosts,
        )

    def launch_config(self) -> BrowserLaunchConfig:
        return BrowserLaunchConfig(
            locale=self.launch.locale,
            timezone_id=self.launch.timezone_id,
            accept_language=self.launch.accept_language,
            args=self.launch.args,
        )


def load_identity_target(key: str, *, credential_reference: str = "", timezone_id: str | None = None):
    if key != "google":
        raise ValueError("unknown maintenance target")
    path = Path(__file__).parent / "definitions" / (key + ".toml")
    data = tomllib.loads(path.read_text())
    data["secret_ref"] = credential_reference
    if timezone_id is not None:
        data["launch"]["timezone_id"] = timezone_id
    return IdentityTargetSpec.model_validate(data)
