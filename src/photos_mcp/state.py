"""State that survives restarts, in ``PHOTOS_STATE_DIR/state.json``: breaker, daily budgets,
the generation known to be dead, and the last error. Written atomically on every change."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from .errors import PhotosError

log = logging.getLogger(__name__)

BREAKER_STEPS = (30 * 60, 2 * 3600, 8 * 3600)
# A block within this long after the previous breaker closed escalates to the next step.
ESCALATE_WINDOW = 24 * 3600


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


class StateStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        rpc_budget: int,
        thumb_budget: int,
        budget_tz: tzinfo = UTC,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(state_dir) / "state.json"
        self.rpc_budget = rpc_budget
        self.thumb_budget = thumb_budget
        # Daily budgets reset at midnight in this zone (PHOTOS_BUDGET_TZ).
        self.budget_tz = budget_tz
        self._clock = clock
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    # -- persistence --------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("state.json unreadable (%s); starting from defaults", type(exc).__name__)
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(self._data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # The in-memory state still applies; only persistence across restarts is lost.
            log.error("could not persist state.json (%s)", type(exc).__name__)

    # -- budgets ------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), self.budget_tz).date().isoformat()

    def _next_reset(self) -> float:
        today = datetime.fromtimestamp(self._clock(), self.budget_tz).date()
        midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), self.budget_tz)
        return midnight.timestamp()

    def _budget(self) -> dict[str, Any]:
        budget = self._data.get("budget")
        today = self._today()
        if not isinstance(budget, dict) or budget.get("day") != today:
            budget = {"day": today, "rpc": 0, "thumb": 0}
            self._data["budget"] = budget
        return budget

    def take(self, kind: str, count: int = 1) -> None:
        """Spend ``count`` units of the ``rpc`` or ``thumb`` budget, or raise budget_exhausted."""
        limit = self.rpc_budget if kind == "rpc" else self.thumb_budget
        with self._lock:
            budget = self._budget()
            used = int(budget.get(kind) or 0)
            if used + count > limit:
                raise PhotosError("budget_exhausted", log_detail=f"{kind} budget {limit} spent")
            budget[kind] = used + count
            self._save()

    # -- breaker ------------------------------------------------------------------------

    def check_breaker(self) -> None:
        with self._lock:
            breaker = self._data.get("breaker") or {}
            until = breaker.get("until") or 0
            if until > self._clock():
                raise PhotosError("blocked", log_detail="breaker open")

    def trip_breaker(self) -> float:
        """Open the breaker after Google pushed back; returns when it closes."""
        now = self._clock()
        with self._lock:
            breaker = self._data.get("breaker") or {}
            level = int(breaker.get("level") or 0)
            previous_until = breaker.get("until") or 0
            if previous_until > now:
                return float(previous_until)  # already open
            level = min(level + 1, len(BREAKER_STEPS)) if now - previous_until < ESCALATE_WINDOW else 1
            until = now + BREAKER_STEPS[level - 1]
            self._data["breaker"] = {"level": level, "until": until, "tripped_at": now}
            self._save()
            log.warning("breaker open for %d s (level %d)", BREAKER_STEPS[level - 1], level)
            return until

    # -- dead generations ---------------------------------------------------------------

    def mark_dead(self, generation: int, code: str) -> None:
        with self._lock:
            self._data["dead"] = {"generation": generation, "code": code, "at": self._clock()}
            self._save()

    def dead_code(self, generation: int) -> str | None:
        """The error a generation died with, if Google already rejected it (no re-hit)."""
        dead = self._data.get("dead") or {}
        if isinstance(dead.get("generation"), int) and dead["generation"] >= generation:
            return str(dead.get("code") or "session_expired")
        return None

    # -- last error ---------------------------------------------------------------------

    def record_error(self, code: str) -> None:
        with self._lock:
            self._data["last_error"] = {"code": code, "at": self._clock()}
            self._save()

    # -- status -------------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            budget = dict(self._budget())
            breaker = self._data.get("breaker") or {}
            until = breaker.get("until") or 0
            last = self._data.get("last_error") or {}
            dead = self._data.get("dead") or {}
            return {
                "breaker": {
                    "open": until > now,
                    "closes_at": _iso(until) if until > now else None,
                    "level": breaker.get("level") or 0,
                },
                "budgets": {
                    "day": budget["day"],
                    "timezone": str(self.budget_tz),
                    "resets_at": _iso(self._next_reset()),
                    "rpc_left": max(0, self.rpc_budget - int(budget.get("rpc") or 0)),
                    "rpc_daily": self.rpc_budget,
                    "thumb_left": max(0, self.thumb_budget - int(budget.get("thumb") or 0)),
                    "thumb_daily": self.thumb_budget,
                },
                "last_error": {"code": last.get("code"), "at": _iso(last.get("at"))} if last else None,
                "dead_generation": dead.get("generation"),
            }
