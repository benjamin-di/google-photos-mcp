"""Durable per-target holds, site cooldowns and generation watermark.

Holds and cooldowns are brakes: losing them silently re-permits an action
that was meant to stop. They use the same private-state durability as the
provider guard and the keeper's credential budget, but live in their own
directory because an identity target is not a provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field
from signin_window.state import PrivateStateError, atomic_private_write, exclusive_lock, read_private_file

from .models import FrozenModel, PersistenceError
from .outcomes import BlockReason, HoldReason

IDENTITY_TARGET_KEYS = frozenset({"google"})

#: Successive ``blocked`` answers back off 2 h, then 8 h, then 24 h (capped).
BLOCK_COOLDOWNS = (timedelta(hours=2), timedelta(hours=8), timedelta(hours=24))
_LOCK_TIMEOUT_SECONDS = 5.0
_MAX_STATE_BYTES = 64 * 1024


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


class TargetStateStore:
    def __init__(
        self,
        root: Path,
        *,
        utc_now: Callable[[], datetime],
        lock_timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.root = Path(root)
        self._utc_now = utc_now
        self._lock_timeout_seconds = lock_timeout_seconds

    def load(self, target: str) -> TargetState:
        with self._locked(target):
            return self._load(target)

    def record_hold(self, target: str, reason: HoldReason) -> TargetHold:
        hold = TargetHold(
            hold_id=f"hold-{uuid4().hex[:16]}",
            reason=reason,
            recorded_at=self._utc_now(),
        )
        with self._locked(target):
            state = self._load(target)
            self._write(target, state.model_copy(update={"hold": hold}))
        return hold

    def replace_hold(self, target: str, hold_id: str, reason: HoldReason) -> TargetHold:
        """Narrow a provisional hold to its final reason, keeping its id.

        If the provisional hold changed meanwhile (an operator acknowledged
        it mid-attempt), the final reason is still recorded, as a fresh hold:
        losing it would re-permit the sign-in it is meant to stop.
        """

        with self._locked(target):
            state = self._load(target)
            if state.hold is not None and state.hold.hold_id == hold_id:
                hold = state.hold.model_copy(update={"reason": reason})
            else:
                hold = TargetHold(
                    hold_id=f"hold-{uuid4().hex[:16]}",
                    reason=reason,
                    recorded_at=self._utc_now(),
                )
            self._write(target, state.model_copy(update={"hold": hold}))
        return hold

    def clear_hold(self, target: str, hold_id: str) -> bool:
        """Compare-and-clear: only the named hold is removed."""

        with self._locked(target):
            state = self._load(target)
            if state.hold is None or state.hold.hold_id != hold_id:
                return False
            self._write(target, state.model_copy(update={"hold": None}))
        return True

    def record_block(self, target: str, reason: BlockReason) -> TargetCooldown:
        with self._locked(target):
            state = self._load(target)
            consecutive = state.consecutive_blocks + 1
            delay = BLOCK_COOLDOWNS[min(consecutive, len(BLOCK_COOLDOWNS)) - 1]
            cooldown = TargetCooldown(reason=reason, until=self._utc_now() + delay)
            self._write(
                target,
                state.model_copy(update={"cooldown": cooldown, "consecutive_blocks": consecutive}),
            )
        return cooldown

    def record_publish(self, target: str, generation: int) -> None:
        with self._locked(target):
            state = self._load(target)
            if generation <= state.last_generation:
                raise PersistenceError("identity generation must increase")
            self._write(
                target,
                state.model_copy(
                    update={
                        "last_generation": generation,
                        "last_published_at": self._utc_now(),
                        "cooldown": None,
                        "consecutive_blocks": 0,
                    }
                ),
            )

    @contextmanager
    def _locked(self, target: str) -> Iterator[None]:
        try:
            with exclusive_lock(
                self._path(target).with_suffix(".lock"),
                timeout_seconds=self._lock_timeout_seconds,
            ):
                yield
        except PrivateStateError as error:
            raise PersistenceError("identity target state is unavailable") from error

    def _path(self, target: str) -> Path:
        if target not in IDENTITY_TARGET_KEYS:
            raise PersistenceError("identity target key is invalid")
        return self.root / f"{target}.json"

    def _load(self, target: str) -> TargetState:
        path = self._path(target)
        if path.is_symlink():
            raise PersistenceError("identity target state must be a regular file")
        try:
            raw = read_private_file(path, max_bytes=_MAX_STATE_BYTES)
        except FileNotFoundError:
            return TargetState()
        except OSError as error:
            raise PersistenceError("identity target state is unreadable") from error
        if len(raw) > _MAX_STATE_BYTES:
            raise PersistenceError("identity target state is oversized")
        try:
            return TargetState.model_validate(json.loads(raw))
        except ValueError as error:
            raise PersistenceError("identity target state is malformed") from error

    def _write(self, target: str, state: TargetState) -> None:
        payload = json.dumps(state.model_dump(mode="json"), sort_keys=True).encode()
        try:
            atomic_private_write(self._path(target), payload)
        except PrivateStateError as error:
            raise PersistenceError("identity target state write failed") from error


__all__ = [
    "BLOCK_COOLDOWNS",
    "TargetCooldown",
    "TargetHold",
    "TargetState",
    "TargetStateStore",
]
