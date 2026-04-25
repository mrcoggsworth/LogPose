# Enricher Pipeline Testing Walkthrough

This document covers how to test the Phase C surface of the
`logpose.enrichers` package:

- The `EnricherPipeline` async runner with stage-list execution
- Per-enricher timeouts (`asyncio.wait_for` over `loop.run_in_executor`)
- Total per-alert wall-clock budget (`asyncio.wait_for` around the whole pipeline)
- Structured error capture into `ctx.errors`

Phase C is **pure orchestration**: no AWS calls, no boto3, no runbook
integration. Tests use plain `pytest` + inline `asyncio.run` (no
`pytest-asyncio` plugin) since `EnricherPipeline.run_sync` is the
production entry point and `asyncio.run` covers the async path directly.

---

## Background: what the runner contributes

A runbook's `enrich()` method is sync — the underlying pika
`BlockingConnection` callback runs to completion before the next
message. Inside that sync method, the runner gives us:

```
ctx in
  ↓ pipeline.run_sync(ctx)
asyncio.run(pipeline.run(ctx))
  └─ for each stage:
       gather(*[run_one(e, ctx) for e in stage])  ← parallel within stage
            └─ asyncio.wait_for(
                  loop.run_in_executor(executor, e.run, ctx),
                  timeout=e.timeout,
              )                                    ← per-enricher timeout
       ← total wrapped in wait_for(_, total_budget_seconds)
ctx out (errors recorded inline)
```

Two timeouts protect the queue throughput envelope:

- **Per-enricher** — bounds the impact of a misbehaving downstream
  (e.g. a slow `LookupEvents` call).
- **Total budget** — last-resort cap so even if every individual
  enricher stays under its own timeout, the pipeline cannot exceed the
  overall budget.

The runner **never raises out to the caller**. Every failure path —
per-enricher timeout, total-budget cancellation, or unexpected exception
— turns into a structured entry in `ctx.errors`.

See [`docs/enrichers/README.md`](../../enrichers/README.md) for the
architecture overview.

---

## Part 1: Unit Testing the Pipeline

Unit tests live in `tests/unit/enrichers/test_runner.py`. They use:

- A real `ThreadPoolExecutor` from `concurrent.futures` (per-test, torn
  down via fixture)
- Three small fake enrichers — `RecordingEnricher`, `TimingOutEnricher`,
  `RaisingEnricher` — that exercise the success, timeout, and exception
  paths without any AWS or networking dependency
- `time.sleep` inside fakes (small intervals, ≤ 0.3 s) to make timing
  semantics observable

### The fixture

```python
@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)
```

The fixture mirrors the production lifecycle (executor owned by the
runbook pod, shared across alerts) — though the suite tears it down per
test for isolation.

### The fakes

```python
class RecordingEnricher:
    def __init__(self, name: str, sleep: float = 0.0) -> None:
        self.name = name
        self.cache_ttl: int | None = None
        self.timeout: float = 3.0
        self._sleep = sleep

    def run(self, ctx: EnricherContext) -> None:
        if self._sleep:
            time.sleep(self._sleep)
        ctx.extracted.setdefault("order", []).append(self.name)
```

`TimingOutEnricher` sleeps longer than its `.timeout`. `RaisingEnricher`
raises a typed exception synchronously. Every fake satisfies the
`Enricher` Protocol structurally — no inheritance.

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/enrichers/test_runner.py -v
```

Expected:

```
tests/unit/enrichers/test_runner.py::test_invalid_total_budget_raises PASSED
tests/unit/enrichers/test_runner.py::test_empty_stages_runs_cleanly PASSED
tests/unit/enrichers/test_runner.py::test_empty_stage_in_middle_is_skipped PASSED
tests/unit/enrichers/test_runner.py::test_runs_stages_in_order PASSED
tests/unit/enrichers/test_runner.py::test_runs_within_stage_in_parallel PASSED
tests/unit/enrichers/test_runner.py::test_per_enricher_timeout_records_error_does_not_raise PASSED
tests/unit/enrichers/test_runner.py::test_total_budget_timeout_cancels_remaining_stages PASSED
tests/unit/enrichers/test_runner.py::test_in_flight_enricher_records_cancelled_error PASSED
tests/unit/enrichers/test_runner.py::test_unexpected_exception_in_enricher_records_error_does_not_raise PASSED
tests/unit/enrichers/test_runner.py::test_multiple_failures_in_one_stage_all_recorded PASSED
tests/unit/enrichers/test_runner.py::test_run_sync_works_from_sync_caller PASSED
tests/unit/enrichers/test_runner.py::test_async_run_works_when_called_directly PASSED

12 passed in ~1s
```

The suite typically runs in well under 2 seconds. The longest single
test is `test_total_budget_timeout_cancels_remaining_stages` which
sleeps ~0.3 s by design.

### What each test covers

#### Construction

| Test | What it verifies |
|---|---|
| `test_invalid_total_budget_raises` | `total_budget_seconds=0` raises `ValueError` at construction time |
| `test_empty_stages_runs_cleanly` | An empty `stages` list is a no-op — no errors, no side effects |
| `test_empty_stage_in_middle_is_skipped` | An empty inner list mid-pipeline is skipped without breaking ordering |

#### Ordering and parallelism

| Test | What it verifies |
|---|---|
| `test_runs_stages_in_order` | Three single-enricher stages produce `order = ["a", "b", "c"]` deterministically |
| `test_runs_within_stage_in_parallel` | Two enrichers in one stage, each sleeping 0.1 s, complete together in < 0.18 s — proving they ran concurrently in the executor |

#### Per-enricher timeout

| Test | What it verifies |
|---|---|
| `test_per_enricher_timeout_records_error_does_not_raise` | A slow enricher (sleep 0.2 s, timeout 0.05 s) records a `TimeoutError` entry in `ctx.errors` while a sibling in the same stage runs to completion. The timeout message includes the configured timeout value. |

#### Total budget

| Test | What it verifies |
|---|---|
| `test_total_budget_timeout_cancels_remaining_stages` | A stage-0 enricher that runs longer than `total_budget_seconds=0.05` causes a stage-1 enricher to never run, and a `_pipeline_` error of type `PipelineBudgetExceeded` is recorded |
| `test_in_flight_enricher_records_cancelled_error` | The in-flight enricher whose work is cut short by the total budget gets a `CancelledError` entry in `ctx.errors` |

#### Unexpected exceptions

| Test | What it verifies |
|---|---|
| `test_unexpected_exception_in_enricher_records_error_does_not_raise` | A `ValueError` raised inside an enricher is captured (`type=ValueError`, `error="bad input"`); subsequent stages still run; the pipeline does not raise |
| `test_multiple_failures_in_one_stage_all_recorded` | Two enrichers in one stage raising different exception types both get individual entries in `ctx.errors`; an unrelated successful enricher still completes |

#### Sync wrapper

| Test | What it verifies |
|---|---|
| `test_run_sync_works_from_sync_caller` | `pipeline.run_sync(ctx)` works as called from sync code — the production path |
| `test_async_run_works_when_called_directly` | `await pipeline.run(ctx)` works from an async context for completeness |

### Adding a new test

The pattern: build the fake enrichers you need, construct a pipeline with
the relevant `stages` and budgets, call `pipeline.run_sync(ctx)`, then
assert on `ctx.extracted` (success path) and `ctx.errors` (failure
path). Always assert **both** — a passing pipeline should leave `errors`
empty, and a failing one should leave the extracted state untouched
where the failure occurred.

```python
def test_my_new_behaviour(executor: Any) -> None:
    a = RecordingEnricher("a")
    pipeline = EnricherPipeline(stages=[[a]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    assert ctx.extracted["order"] == ["a"]
    assert ctx.errors == []
```

### Caveats to remember when writing new tests

- **Threads cannot be cancelled.** A timeout cancels the `wait_for`, not
  the underlying thread. The thread keeps running until the sync call
  completes. Tests that assert "no side effect after timeout" must use
  short fake sleeps and a short pool teardown, or assert only on
  `ctx.errors` rather than absence of side effects.
- **Wall-clock dependent.** The runner uses real `asyncio` time, not an
  injected clock. Keep timeouts short (≤ 0.3 s) so the suite stays
  fast, but generous enough that CI jitter does not produce flakes.
- **One `asyncio.run` per call.** `run_sync` creates a fresh event loop
  per call. Do not nest pipeline calls inside an existing event loop.

---

## Part 2: Verification loop

```sh
# 1. Tests
pytest tests/unit/enrichers/test_runner.py -v

# 2. Type checking
python -m mypy logpose/enrichers/

# 3. Format check (line-length 100)
python -m black --check logpose/enrichers/ tests/unit/enrichers/

# 4. Lint
python -m flake8 logpose/enrichers/ tests/unit/enrichers/
```

All four must be clean. The full unit suite (`pytest tests/unit/`)
should also pass — Phase C is purely additive and must not regress any
existing test.

---

## Why no integration test in Phase C?

`EnricherPipeline` does not interact with RabbitMQ, boto3, or any
external service. Its dependencies are: a list of enrichers, a
`ThreadPoolExecutor`, and the `asyncio` event loop. The first
integration test arrives in Phase E, where the pipeline is wired into
the runbook and exercised end-to-end against `moto`-backed AWS.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/enrichers/runner.py`](../../../logpose/enrichers/runner.py) | `EnricherPipeline` async runner with per-enricher timeout + total budget |
| [`logpose/enrichers/__init__.py`](../../../logpose/enrichers/__init__.py) | Re-exports `EnricherPipeline` |
| [`tests/unit/enrichers/test_runner.py`](../../../tests/unit/enrichers/test_runner.py) | 12 unit tests covering construction, ordering, intra-stage parallelism, both timeout layers, exception capture, and both entry points |
| [`docs/enrichers/README.md`](../../enrichers/README.md) | Architecture overview — see the `EnricherPipeline` section |
