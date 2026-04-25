"""Unit tests for ``logpose.enrichers.cache``."""

from __future__ import annotations

import pytest

from logpose.enrichers.cache import InProcessTTLCache, PrincipalCache


class FakeClock:
    """Manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ---------------------------------------------------------------------------
# Construction / contract
# ---------------------------------------------------------------------------


def test_in_process_cache_is_a_principal_cache() -> None:
    cache = InProcessTTLCache()
    assert isinstance(cache, PrincipalCache)


def test_invalid_construction_raises() -> None:
    with pytest.raises(ValueError):
        InProcessTTLCache(max_size=0)
    with pytest.raises(ValueError):
        InProcessTTLCache(default_ttl=0)


def test_set_with_non_positive_ttl_raises() -> None:
    cache = InProcessTTLCache()
    with pytest.raises(ValueError):
        cache.set("k", "ns", "v", ttl=0)
    with pytest.raises(ValueError):
        cache.set("k", "ns", "v", ttl=-5)


# ---------------------------------------------------------------------------
# Hit / miss
# ---------------------------------------------------------------------------


def test_set_then_get_returns_value() -> None:
    cache = InProcessTTLCache()
    cache.set("aws::arn:role/Foo", "history", [{"e": 1}], ttl=60)
    assert cache.get("aws::arn:role/Foo", "history") == [{"e": 1}]


def test_miss_returns_none() -> None:
    cache = InProcessTTLCache()
    assert cache.get("aws::missing", "history") is None


def test_set_overwrites_existing_value() -> None:
    cache = InProcessTTLCache()
    cache.set("k", "ns", "first", ttl=60)
    cache.set("k", "ns", "second", ttl=60)
    assert cache.get("k", "ns") == "second"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_ttl_expiry_returns_none_after_clock_advance() -> None:
    clock = FakeClock()
    cache = InProcessTTLCache(clock=clock)
    cache.set("k", "ns", "v", ttl=10)

    clock.advance(9.999)
    assert cache.get("k", "ns") == "v"  # not yet expired

    clock.advance(0.002)  # now past the 10s TTL
    assert cache.get("k", "ns") is None


def test_expired_entry_is_purged_from_store() -> None:
    clock = FakeClock()
    cache = InProcessTTLCache(clock=clock)
    cache.set("k", "ns", "v", ttl=5)
    clock.advance(10)
    cache.get("k", "ns")  # triggers lazy purge
    # Subsequent stats.size confirms the expired entry was dropped, not evicted.
    assert cache.stats()["size"] == 0
    assert cache.stats()["evictions"] == 0


def test_per_set_ttl_overrides_default() -> None:
    clock = FakeClock()
    cache = InProcessTTLCache(default_ttl=60, clock=clock)
    cache.set("short", "ns", 1, ttl=5)
    cache.set("long", "ns", 2, ttl=600)
    clock.advance(10)
    assert cache.get("short", "ns") is None
    assert cache.get("long", "ns") == 2


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_at_max_size() -> None:
    cache = InProcessTTLCache(max_size=3)
    cache.set("a", "ns", 1, ttl=60)
    cache.set("b", "ns", 2, ttl=60)
    cache.set("c", "ns", 3, ttl=60)
    cache.set("d", "ns", 4, ttl=60)  # should evict "a" (oldest)
    assert cache.get("a", "ns") is None
    assert cache.get("b", "ns") == 2
    assert cache.get("c", "ns") == 3
    assert cache.get("d", "ns") == 4
    assert cache.stats()["evictions"] == 1


def test_get_touches_lru_order() -> None:
    cache = InProcessTTLCache(max_size=3)
    cache.set("a", "ns", 1, ttl=60)
    cache.set("b", "ns", 2, ttl=60)
    cache.set("c", "ns", 3, ttl=60)
    # Touch "a" so it becomes most-recently used; "b" becomes the oldest.
    cache.get("a", "ns")
    cache.set("d", "ns", 4, ttl=60)  # should evict "b"
    assert cache.get("a", "ns") == 1
    assert cache.get("b", "ns") is None
    assert cache.get("c", "ns") == 3
    assert cache.get("d", "ns") == 4


def test_set_overwrite_does_not_count_as_eviction() -> None:
    cache = InProcessTTLCache(max_size=2)
    cache.set("a", "ns", 1, ttl=60)
    cache.set("a", "ns", 2, ttl=60)  # overwrite — same key
    assert cache.stats()["evictions"] == 0
    assert cache.stats()["size"] == 1


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------


def test_namespace_isolation() -> None:
    cache = InProcessTTLCache()
    cache.set("aws::role/Foo", "history", "hist-data", ttl=60)
    cache.set("aws::role/Foo", "objects", "obj-data", ttl=60)
    assert cache.get("aws::role/Foo", "history") == "hist-data"
    assert cache.get("aws::role/Foo", "objects") == "obj-data"


def test_namespace_miss_is_independent_of_other_namespaces() -> None:
    cache = InProcessTTLCache()
    cache.set("k", "ns_a", "v", ttl=60)
    assert cache.get("k", "ns_b") is None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_increments_correctly() -> None:
    clock = FakeClock()
    cache = InProcessTTLCache(max_size=2, clock=clock)
    cache.set("a", "ns", 1, ttl=60)
    cache.set("b", "ns", 2, ttl=60)

    cache.get("a", "ns")  # hit
    cache.get("a", "ns")  # hit
    cache.get("missing", "ns")  # miss

    cache.set("c", "ns", 3, ttl=60)  # evicts "b" (LRU)

    clock.advance(120)
    cache.get("a", "ns")  # miss — expired

    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 2  # "missing" + expired "a"
    assert s["evictions"] == 1
    assert s["size"] == 1  # only "c" left ("a" was lazily purged on the expired get)


def test_stats_size_reflects_current_count() -> None:
    cache = InProcessTTLCache()
    assert cache.stats()["size"] == 0
    cache.set("a", "ns", 1, ttl=60)
    assert cache.stats()["size"] == 1
    cache.set("b", "ns", 2, ttl=60)
    assert cache.stats()["size"] == 2
