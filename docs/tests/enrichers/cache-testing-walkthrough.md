# Principal Cache Testing Walkthrough

This document covers how to test the Phase B surface of the
`logpose.enrichers` package:

- The `PrincipalCache` abstract base class
- The `InProcessTTLCache` in-memory implementation (TTL + LRU + namespace
  isolation)

Phase B is **pure data structure**: no AWS calls, no async, no runbook
integration. Tests therefore run with no Docker, no RabbitMQ, no boto3 —
just `pytest`.

---

## Background: what the cache contributes

When the CloudTrail enrichers (Phase D) start calling AWS APIs, the same
principal will appear in many alerts within minutes — and re-running
`LookupEvents` for every alert would be both slow and budget-bleeding.
The `PrincipalCache` interface gives every enricher a shared,
namespace-isolated, TTL-bounded cache so the second alert from the same
role is a cache hit.

```
Phase B surface
  ├─ PrincipalCache         ← abstract interface (swap-ready for Redis)
  └─ InProcessTTLCache      ← per-pod in-memory implementation
```

Cache entries are keyed by `(namespace, key)`:

- **`key`** is typically `Principal.cache_key()` — the same string for any
  alert involving the same actor.
- **`namespace`** distinguishes which enricher's data is stored
  (`"history"`, `"objects"`, …) so two enrichers caching unrelated data
  for the same principal cannot collide.

Each entry has its own per-call `ttl` (the default applies only when the
caller passes the cache's `default_ttl`). Expiry is lazy: an entry is
dropped on the next `get` after its `expires_at`. Lazy purge is *not*
counted as an eviction; only `max_size`-driven LRU removals are.

See [`docs/enrichers/README.md`](../../enrichers/README.md) for the
architecture overview.

---

## Part 1: Unit Testing the Cache

Unit tests live in `tests/unit/enrichers/test_cache.py`. Each test
constructs a fresh `InProcessTTLCache`, exercises one behaviour, and
asserts on the public surface (`get`, `set`, `stats`).

### The mock structure

There isn't one. The implementation is a pure data structure with an
**injectable clock** — tests pass a `FakeClock` they advance manually
instead of patching `time.monotonic` globally:

```python
class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds
```

Construction:

```python
clock = FakeClock()
cache = InProcessTTLCache(max_size=2, default_ttl=60, clock=clock)
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/enrichers/test_cache.py -v
```

Expected:

```
tests/unit/enrichers/test_cache.py::test_in_process_cache_is_a_principal_cache PASSED
tests/unit/enrichers/test_cache.py::test_invalid_construction_raises PASSED
tests/unit/enrichers/test_cache.py::test_set_with_non_positive_ttl_raises PASSED
tests/unit/enrichers/test_cache.py::test_set_then_get_returns_value PASSED
tests/unit/enrichers/test_cache.py::test_miss_returns_none PASSED
tests/unit/enrichers/test_cache.py::test_set_overwrites_existing_value PASSED
tests/unit/enrichers/test_cache.py::test_ttl_expiry_returns_none_after_clock_advance PASSED
tests/unit/enrichers/test_cache.py::test_expired_entry_is_purged_from_store PASSED
tests/unit/enrichers/test_cache.py::test_per_set_ttl_overrides_default PASSED
tests/unit/enrichers/test_cache.py::test_lru_eviction_at_max_size PASSED
tests/unit/enrichers/test_cache.py::test_get_touches_lru_order PASSED
tests/unit/enrichers/test_cache.py::test_set_overwrite_does_not_count_as_eviction PASSED
tests/unit/enrichers/test_cache.py::test_namespace_isolation PASSED
tests/unit/enrichers/test_cache.py::test_namespace_miss_is_independent_of_other_namespaces PASSED
tests/unit/enrichers/test_cache.py::test_stats_increments_correctly PASSED
tests/unit/enrichers/test_cache.py::test_stats_size_reflects_current_count PASSED

16 passed in 0.10s
```

### What each test covers

#### Construction / contract

| Test | What it verifies |
|---|---|
| `test_in_process_cache_is_a_principal_cache` | `InProcessTTLCache` is an instance of `PrincipalCache` — the ABC is honoured so callers can type against the interface |
| `test_invalid_construction_raises` | `max_size=0` and `default_ttl=0` both raise `ValueError` at construction time |
| `test_set_with_non_positive_ttl_raises` | A `set` with a non-positive TTL raises immediately rather than caching an entry that's already expired |

#### Hit / miss

| Test | What it verifies |
|---|---|
| `test_set_then_get_returns_value` | A simple round trip works |
| `test_miss_returns_none` | Unknown key returns `None` (the agreed miss sentinel) |
| `test_set_overwrites_existing_value` | Writing the same `(namespace, key)` twice keeps the second value |

#### TTL expiry

| Test | What it verifies |
|---|---|
| `test_ttl_expiry_returns_none_after_clock_advance` | An entry stops returning its value the instant the injected clock crosses its `expires_at` |
| `test_expired_entry_is_purged_from_store` | The expired entry is dropped from `_store` lazily on `get`, and **not** counted as an `eviction` |
| `test_per_set_ttl_overrides_default` | The per-call `ttl` argument controls expiry, not `default_ttl` |

#### LRU eviction

| Test | What it verifies |
|---|---|
| `test_lru_eviction_at_max_size` | Filling the cache past `max_size` evicts the least-recently-used entry and increments `evictions` |
| `test_get_touches_lru_order` | A `get` moves an entry to most-recently-used so it isn't the next victim |
| `test_set_overwrite_does_not_count_as_eviction` | Overwriting an existing key is *not* an eviction — `evictions` stays at 0 |

#### Namespace isolation

| Test | What it verifies |
|---|---|
| `test_namespace_isolation` | Same key under two namespaces stores two independent values |
| `test_namespace_miss_is_independent_of_other_namespaces` | A hit in `ns_a` does not leak into `ns_b` |

#### Stats

| Test | What it verifies |
|---|---|
| `test_stats_increments_correctly` | A scripted sequence of hits, misses, an eviction, and a TTL-expired miss yields exactly the expected counters and final size |
| `test_stats_size_reflects_current_count` | `size` matches the number of live entries at any point |

### Adding a new behaviour test

To verify a new branch (e.g. you add a `clear` method or a `peek` that
doesn't touch LRU), follow the existing pattern: build a fresh cache,
exercise the behaviour, assert on `stats()` and on `get()` for the
keys you care about. Always check **both** the value AND the cumulative
counters — the counters are the contract for Phase F's metrics.

```python
def test_new_clear_method_resets_size_but_keeps_counters() -> None:
    cache = InProcessTTLCache(max_size=2)
    cache.set("a", "ns", 1, ttl=60)
    cache.get("a", "ns")
    cache.clear()  # hypothetical
    assert cache.stats()["size"] == 0
    assert cache.stats()["hits"] == 1  # cumulative counters preserved
```

---

## Part 2: Verification loop

Phase B's verification loop is the project standard, restricted to the
new files:

```sh
# 1. Tests
pytest tests/unit/enrichers/test_cache.py -v

# 2. Type checking
python -m mypy logpose/enrichers/

# 3. Format check (line-length 100 — see pyproject.toml)
python -m black --check logpose/enrichers/ tests/unit/enrichers/

# 4. Lint
python -m flake8 logpose/enrichers/ tests/unit/enrichers/
```

All four must be clean. The full unit suite (`pytest tests/unit/`) should
also pass — Phase B is purely additive and must not regress any existing
test.

---

## Why no integration test in Phase B?

`InProcessTTLCache` does not interact with RabbitMQ, boto3, or any
external service — it's a pure data structure. The first integration
test arrives in Phase E
(`tests/integration/test_cloudtrail_runbook_pipeline.py`), which
exercises the full pipeline (cache included) end-to-end against
`moto`-backed AWS.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/enrichers/cache.py`](../../../logpose/enrichers/cache.py) | `PrincipalCache` ABC + `InProcessTTLCache` implementation |
| [`logpose/enrichers/__init__.py`](../../../logpose/enrichers/__init__.py) | Re-exports `PrincipalCache` and `InProcessTTLCache` |
| [`tests/unit/enrichers/test_cache.py`](../../../tests/unit/enrichers/test_cache.py) | 16 unit tests covering construction, hit/miss, TTL, LRU, namespace isolation, stats |
| [`docs/enrichers/README.md`](../../enrichers/README.md) | Architecture overview — see the `PrincipalCache` section |
