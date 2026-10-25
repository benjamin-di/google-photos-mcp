"""Durable per-target holds, site cooldowns and generation watermark.

Holds and cooldowns are brakes: losing them silently re-permits an action
that was meant to stop. They use the same private-state durability as the
provider guard and the keeper's credential budget, but live in their own
directory because an identity target is not a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field
from signin_window.automation.maintenance import MaintenanceStateStore

from .models import FrozenModel, PersistenceError
from .outcomes import BlockReason, HoldReason

IDENTITY_TARGET_KEYS = frozenset({"google"})

#: Successive ``blocked`` answers back off 2 h, then 8 h, then 24 h (capped).
BLOCK_COOLDOWNS = (timedelta(hours=2), timedelta(hours=8), timedelta(hours=24))


class TargetHold(FrozenModel):
    hold_id: str
    reason: HoldReason
    recorded_at: AwareDatetime


class TargetCooldown(FrozenModel):
    reason: BlockReason
    until: AwareDatetime


class TargetState(FrozenModel):
    schema_version: Literal[1] = 1
    hold: TargetHold | None = None
    cooldown: TargetCooldown | None = None
    consecutive_blocks: int = Field(default=0, ge=0)
    last_generation: int = Field(default=0, ge=0)
    last_published_at: AwareDatetime | None = None


class TargetStateStore(MaintenanceStateStore):
    """Application reasons, schema and cooldown policy over shared durability."""

    def __init__(self, root: Path, *, utc_now: Callable[[], datetime], lock_timeout_seconds: float = 5):
        super().__init__(
            root,
            state_type=TargetState,
            hold_type=TargetHold,
            cooldown_type=TargetCooldown,
            valid_keys=IDENTITY_TARGET_KEYS,
            block_cooldowns=BLOCK_COOLDOWNS,
            utc_now=utc_now,
            error_type=PersistenceError,
            label="identity target state",
            lock_timeout_seconds=lock_timeout_seconds,
        )


__all__ = ["BLOCK_COOLDOWNS", "TargetCooldown", "TargetHold", "TargetState", "TargetStateStore"]
