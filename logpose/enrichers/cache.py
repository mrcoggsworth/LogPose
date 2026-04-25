"""Per-principal cache used by enrichers.

Two surfaces:

- ``PrincipalCache``     — abstract interface every enricher depends on
- ``InProcessTTLCache``  — in-process TTL+LRU implementation, used per pod

The ABC exists so a Redis-backed implementation can be swapped in later
without enricher code changes. For now, each runbook pod holds its own
in-process cache; a pod restart loses its cache (acceptable — the only cost
is one extra round of API calls until it warms up again).

The TTL cache stores values under a ``(namespace, key)`` pair: the
``key`` is typically ``Principal.cache_key()`` (so all enrichers looking
at the same principal share a namespace) and the ``namespace`` distinguishes
which enricher's data is stored (``"history"``, ``"objects"``, etc.).

Design choices documented in the Phase B plan:

- Hand-rolled on top of ``collections.OrderedDict`` + ``time.monotonic`` —
  no new dependency.
- Clock is injectable via the ``clock`` constructor argument so unit tests
  can advance time deterministically without ``freezegun``.
- ``None`` is the miss sentinel — callers should never store ``None``.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Callable


class PrincipalCache(ABC):
    """Abstract cache keyed by ``(namespace, key)`` with per-entry TTL.

    Concrete implementations:

    - ``InProcessTTLCache`` — in-memory LRU, this module
    - (future) Redis-backed implementation

    The contract is intentionally narrow: ``get``, ``set``, ``stats``.
    Anything more (force-refresh, bulk ops) is added when a real caller
    needs it, not preemptively.
    """

    @abstractmethod
    def get(self, key: str, namespace: str) -> Any | None:
        """Return the cached value or ``None`` if absent or expired."""

    @abstractmethod
    def set(self, key: str, namespace: str, value: Any, ttl: int) -> None:
        """Store ``value`` under ``(namespace, key)``; expires after ``ttl`` seconds."""

    @abstractmethod
    def stats(self) -> dict[str, int]:
        """Return cumulative counters: ``hits``, ``misses``, ``evictions``, ``size``."""


class InProcessTTLCache(PrincipalCache):
    """In-process TTL+LRU cache backed by an ``OrderedDict``.

    - Each entry has its own ``expires_at`` (per-call ``ttl`` argument).
    - When ``max_size`` is reached, the least-recently *used* entry is
      evicted. ``get`` counts as a use; ``set`` of an existing key counts
      as a use.
    - Expired entries are evicted lazily on the next ``get`` for that key.
    """

    def __init__(
        self,
        max_size: int = 5000,
        default_ttl: int = 900,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if default_ttl <= 0:
            raise ValueError("default_ttl must be positive")
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._clock = clock
        self._store: OrderedDict[tuple[str, str], tuple[Any, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def default_ttl(self) -> int:
        return self._default_ttl

    def get(self, key: str, namespace: str) -> Any | None:
        ck = (namespace, key)
        entry = self._store.get(ck)
        if entry is None:
            self._misses += 1
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            # Expired — drop it and report a miss; do NOT count as eviction.
            del self._store[ck]
            self._misses += 1
            return None
        self._store.move_to_end(ck)
        self._hits += 1
        return value

    def set(self, key: str, namespace: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        ck = (namespace, key)
        expires_at = self._clock() + ttl
        if ck in self._store:
            # Update in place; touch LRU.
            self._store[ck] = (value, expires_at)
            self._store.move_to_end(ck)
            return

        # New entry — make room first if needed.
        if len(self._store) >= self._max_size:
            self._store.popitem(last=False)
            self._evictions += 1
        self._store[ck] = (value, expires_at)

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "size": len(self._store),
        }
