"""Server-side, scope-bound cursors with a 1 h TTL.

A cursor string is ``c1.<scope>.<delivered>.<id>``. The state behind ``<id>`` (Google's
page token, the offset into that page, the tokens already seen) stays in memory. When the
state is gone (expired, or the server restarted), the scope and the delivered count are
still in the string, so the listing can be *recovered*: re-read from the start, skip the
items already delivered, and flag ``cursor_recovered`` / ``dedupe_required`` because the
library may have changed in between.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .errors import PhotosError

TTL = 3600.0
MAX_ENTRIES = 2048


def scope_of(kind: str, ident: str = "") -> str:
    return hashlib.sha256(f"{kind}\x00{ident}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class CursorState:
    scope: str
    server_token: str | None
    offset: int
    delivered: int
    seen: tuple[str, ...] = ()
    created: float = 0.0


class CursorStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, CursorState] = OrderedDict()

    def issue(self, state: CursorState) -> str:
        ident = secrets.token_urlsafe(9)
        now = self._clock()
        with self._lock:
            self._entries[ident] = CursorState(
                state.scope, state.server_token, state.offset, state.delivered, state.seen, now
            )
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
        return f"c1.{state.scope}.{state.delivered}.{ident}"

    def resolve(self, cursor: str, scope: str) -> tuple[CursorState | None, int]:
        """(state, delivered). ``state`` is None when the cursor must be recovered."""
        parts = cursor.split(".") if isinstance(cursor, str) else []
        if len(parts) != 4 or parts[0] != "c1" or not parts[2].isdigit() or len(cursor) > 200:
            raise PhotosError("invalid_cursor", log_detail="malformed cursor")
        if parts[1] != scope:
            raise PhotosError("invalid_cursor", log_detail="cursor scope mismatch")
        delivered = int(parts[2])
        with self._lock:
            state = self._entries.get(parts[3])
            if state is not None and self._clock() - state.created > self._ttl:
                del self._entries[parts[3]]
                state = None
        if state is not None and state.scope != scope:
            raise PhotosError("invalid_cursor", log_detail="cursor scope mismatch")
        return state, delivered
