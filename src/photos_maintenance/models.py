"""Maintenance state contracts; runtime account values never enter source."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PersistenceError(OSError):
    """A required maintenance record could not be used durably."""


class Requirement(StrEnum):
    CREDENTIAL_REFERENCE = "credential_reference"
    OP_CLI = "op_cli"
    OP_SERVICE_TOKEN = "op_service_token"
    NATIVE_INPUT = "native_input"
    VISION_PLANNER = "vision_planner"
    MAIL_GATEWAY = "mail_gateway"


class ProviderReadiness(FrozenModel):
    provider: str = "google"
    missing: tuple[Requirement, ...] = ()
