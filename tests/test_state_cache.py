"""Persisted state (budgets reset at midnight in the budget zone, breaker steps) and the thumbnail
LRU cache."""

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from photos_mcp.cache import ThumbCache
from photos_mcp.errors import PhotosError
from photos_mcp.state import StateStore


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.parametrize("zone", [UTC, ZoneInfo("America/New_York"), ZoneInfo("Asia/Tokyo")])
def test_budget_resets_at_midnight_in_the_budget_zone(tmp_path, zone):
    clock = Clock(datetime(2026, 3, 14, 23, 59, tzinfo=zone).timestamp())
    store = StateStore(tmp_path, rpc_budget=2, thumb_budget=1, budget_tz=zone, clock=clock)
    store.take("rpc", 2)
    with pytest.raises(PhotosError) as caught:
        store.take("rpc")
    assert caught.value.code == "budget_exhausted"
    store.take("thumb")
    budgets = store.describe()["budgets"]
    assert budgets["day"] == "2026-03-14" and budgets["timezone"] == str(zone)
    assert budgets["resets_at"] == datetime(2026, 3, 15, tzinfo=zone).astimezone(UTC).isoformat(timespec="seconds")
    clock.now += 120  # 00:01 the next day in that zone
    store.take("rpc", 2)
    store.take("thumb")


def test_budget_is_persisted(tmp_path):
    clock = Clock(1_790_000_000.0)
    StateStore(tmp_path, rpc_budget=3, thumb_budget=3, clock=clock).take("rpc", 3)
    again = StateStore(tmp_path, rpc_budget=3, thumb_budget=3, clock=clock)
    with pytest.raises(PhotosError):
        again.take("rpc")
    assert again.describe()["budgets"]["rpc_left"] == 0


def test_breaker_steps_30m_2h_8h_and_resets_after_a_quiet_day(tmp_path):
    clock = Clock(1_790_000_000.0)
    store = StateStore(tmp_path, rpc_budget=1, thumb_budget=1, clock=clock)
    durations = []
    for _ in range(4):
        until = store.trip_breaker()
        durations.append(until - clock.now)
        with pytest.raises(PhotosError) as caught:
            store.check_breaker()
        assert caught.value.code == "blocked"
        clock.now = until + 1
        store.check_breaker()
    assert durations == [1800, 7200, 28800, 28800]
    clock.now += 25 * 3600
    assert store.trip_breaker() - clock.now == 1800


def test_corrupt_state_starts_from_defaults(tmp_path):
    (tmp_path / "state.json").write_text("{nope")
    store = StateStore(tmp_path, rpc_budget=1, thumb_budget=1)
    store.take("rpc")
    assert store.describe()["budgets"]["rpc_left"] == 0


def test_dead_generation(tmp_path):
    store = StateStore(tmp_path, rpc_budget=1, thumb_budget=1)
    store.mark_dead(7, "consent_required")
    assert store.dead_code(7) == "consent_required" and store.dead_code(6) == "consent_required"
    assert store.dead_code(8) is None


def test_cache_roundtrip_and_lru_eviction(tmp_path):
    cache = ThumbCache(tmp_path / "cache", max_bytes=16_000)
    keys = [f"AF1QipFAKE{i:04d}" for i in range(5)]
    for index, key in enumerate(keys):
        cache.put(key, 256, b"\xff\xd8\xff" + b"x" * 2_900, {"w": 1})
        jpg = tmp_path / "cache" / key / "256.jpg"
        os.utime(jpg, (1000 + index, 1000 + index))
    # Touch the oldest: a hit makes it most recently used.
    assert cache.get(keys[0], 256)[1] == {"w": 1}
    cache.put("AF1QipFAKE9999", 256, b"\xff\xd8\xff" + b"x" * 2_900, {"w": 2})
    assert cache.size() <= 16_000 * 0.9
    assert cache.get(keys[0], 256) is not None  # recently used survives
    assert cache.get(keys[1], 256) is None  # least recently used went first
    assert cache.get("AF1QipFAKE9999", 256) is not None


def test_cache_rejects_path_tricks(tmp_path):
    cache = ThumbCache(tmp_path / "cache", max_bytes=10_000)
    with pytest.raises(ValueError):
        cache.put("../../etc", 256, b"x", {})


def test_cache_disabled_with_zero_cap(tmp_path):
    cache = ThumbCache(tmp_path / "cache", max_bytes=0)
    cache.put("AF1QipFAKE0001", 256, b"x", {})
    assert cache.get("AF1QipFAKE0001", 256) is None
