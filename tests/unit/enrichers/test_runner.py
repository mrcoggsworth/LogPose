"""Unit tests for ``logpose.enrichers.runner``.

Tests use plain pytest + ``asyncio.run`` (no pytest-asyncio plugin needed)
since ``EnricherPipeline.run_sync`` is the production sync wrapper anyway.
Fake enrichers do real ``time.sleep`` to exercise the thread pool, kept to
small intervals so the suite runs in well under a second.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from logpose.enrichers.context import EnricherContext
from logpose.enrichers.runner import EnricherPipeline
from logpose.models.alert import Alert

# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


def _ctx() -> EnricherContext:
    return EnricherContext(alert=Alert(source="test", raw_payload={}))


class RecordingEnricher:
    """Sync enricher that appends its name to ctx.extracted['order'] when run."""

    def __init__(self, name: str, sleep: float = 0.0) -> None:
        self.name = name
        self.cache_ttl: int | None = None
        self.timeout: float = 3.0
        self._sleep = sleep

    def run(self, ctx: EnricherContext) -> None:
        if self._sleep:
            time.sleep(self._sleep)
        ctx.extracted.setdefault("order", []).append(self.name)


class TimingOutEnricher:
    """Enricher that sleeps longer than its timeout, forcing per-enricher timeout."""

    def __init__(
        self,
        name: str = "slow",
        sleep: float = 0.2,
        timeout: float = 0.05,
    ) -> None:
        self.name = name
        self.cache_ttl: int | None = None
        self.timeout = timeout
        self._sleep = sleep

    def run(self, ctx: EnricherContext) -> None:
        time.sleep(self._sleep)
        ctx.extracted["should_not_appear"] = True


class RaisingEnricher:
    """Enricher that raises a typed exception synchronously."""

    def __init__(self, name: str = "boom", exc: Exception | None = None) -> None:
        self.name = name
        self.cache_ttl: int | None = None
        self.timeout: float = 3.0
        self._exc = exc or RuntimeError("kaboom")

    def run(self, ctx: EnricherContext) -> None:
        raise self._exc


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_invalid_total_budget_raises(executor: Any) -> None:
    with pytest.raises(ValueError):
        EnricherPipeline(stages=[], executor=executor, total_budget_seconds=0)


def test_empty_stages_runs_cleanly(executor: Any) -> None:
    pipeline = EnricherPipeline(stages=[], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    assert ctx.errors == []
    assert ctx.extracted == {}


def test_empty_stage_in_middle_is_skipped(executor: Any) -> None:
    a = RecordingEnricher("a")
    b = RecordingEnricher("b")
    pipeline = EnricherPipeline(stages=[[a], [], [b]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    assert ctx.extracted["order"] == ["a", "b"]
    assert ctx.errors == []


# ---------------------------------------------------------------------------
# Ordering + parallelism
# ---------------------------------------------------------------------------


def test_runs_stages_in_order(executor: Any) -> None:
    a = RecordingEnricher("a")
    b = RecordingEnricher("b")
    c = RecordingEnricher("c")
    pipeline = EnricherPipeline(stages=[[a], [b], [c]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    assert ctx.extracted["order"] == ["a", "b", "c"]
    assert ctx.errors == []


def test_runs_within_stage_in_parallel(executor: Any) -> None:
    # Two enrichers each sleeping 0.1s; if run sequentially total ≥ 0.2s.
    e1 = RecordingEnricher("e1", sleep=0.1)
    e2 = RecordingEnricher("e2", sleep=0.1)
    pipeline = EnricherPipeline(stages=[[e1, e2]], executor=executor)
    ctx = _ctx()
    started = time.monotonic()
    pipeline.run_sync(ctx)
    elapsed = time.monotonic() - started
    # Generous bound: parallelism should give us comfortably < 0.18s total.
    assert elapsed < 0.18, f"stage ran sequentially (elapsed={elapsed:.3f}s)"
    assert sorted(ctx.extracted["order"]) == ["e1", "e2"]
    assert ctx.errors == []


# ---------------------------------------------------------------------------
# Per-enricher timeout
# ---------------------------------------------------------------------------


def test_per_enricher_timeout_records_error_does_not_raise(executor: Any) -> None:
    slow = TimingOutEnricher(name="slow", sleep=0.2, timeout=0.05)
    fast = RecordingEnricher("fast")
    pipeline = EnricherPipeline(stages=[[slow, fast]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    # Fast one ran fine; slow one recorded a timeout error.
    assert "fast" in ctx.extracted["order"]
    assert "should_not_appear" not in ctx.extracted
    timeouts = [e for e in ctx.errors if e["type"] == "TimeoutError"]
    assert len(timeouts) == 1
    assert timeouts[0]["enricher"] == "slow"
    assert "0.05" in timeouts[0]["error"]


# ---------------------------------------------------------------------------
# Total budget timeout
# ---------------------------------------------------------------------------


def test_total_budget_timeout_cancels_remaining_stages(executor: Any) -> None:
    # Stage 0 sleeps long enough to bust the total budget; stage 1 should
    # not run, and the pipeline should record a budget-exceeded error.
    slow = RecordingEnricher("slow", sleep=0.3)
    slow.timeout = 5.0  # don't trip per-enricher timeout
    later = RecordingEnricher("later")
    pipeline = EnricherPipeline(
        stages=[[slow], [later]],
        executor=executor,
        total_budget_seconds=0.05,
    )
    ctx = _ctx()
    pipeline.run_sync(ctx)
    # Pipeline-level error recorded.
    pipeline_errs = [e for e in ctx.errors if e["enricher"] == "_pipeline_"]
    assert len(pipeline_errs) == 1
    assert pipeline_errs[0]["type"] == "PipelineBudgetExceeded"
    # The "later" enricher in stage 1 never ran.
    assert "order" not in ctx.extracted or "later" not in ctx.extracted.get("order", [])


def test_in_flight_enricher_records_cancelled_error(executor: Any) -> None:
    slow = RecordingEnricher("slow", sleep=0.3)
    slow.timeout = 5.0
    pipeline = EnricherPipeline(
        stages=[[slow]],
        executor=executor,
        total_budget_seconds=0.05,
    )
    ctx = _ctx()
    pipeline.run_sync(ctx)
    cancelled = [e for e in ctx.errors if e["type"] == "CancelledError"]
    assert len(cancelled) == 1
    assert cancelled[0]["enricher"] == "slow"


# ---------------------------------------------------------------------------
# Unexpected exceptions
# ---------------------------------------------------------------------------


def test_unexpected_exception_in_enricher_records_error_does_not_raise(
    executor: Any,
) -> None:
    boom = RaisingEnricher(name="boom", exc=ValueError("bad input"))
    after = RecordingEnricher("after")
    pipeline = EnricherPipeline(stages=[[boom], [after]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)  # must not raise
    errs = [e for e in ctx.errors if e["enricher"] == "boom"]
    assert len(errs) == 1
    assert errs[0]["type"] == "ValueError"
    assert errs[0]["error"] == "bad input"
    # Subsequent stage still ran — one enricher's failure does not block the rest.
    assert ctx.extracted["order"] == ["after"]


def test_multiple_failures_in_one_stage_all_recorded(executor: Any) -> None:
    a = RaisingEnricher(name="a", exc=KeyError("missing"))
    b = RaisingEnricher(name="b", exc=RuntimeError("oops"))
    c = RecordingEnricher("c")
    pipeline = EnricherPipeline(stages=[[a, b, c]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    err_types = {e["enricher"]: e["type"] for e in ctx.errors}
    assert err_types == {"a": "KeyError", "b": "RuntimeError"}
    assert ctx.extracted["order"] == ["c"]


# ---------------------------------------------------------------------------
# run_sync wrapper
# ---------------------------------------------------------------------------


def test_run_sync_works_from_sync_caller(executor: Any) -> None:
    a = RecordingEnricher("a")
    pipeline = EnricherPipeline(stages=[[a]], executor=executor)
    ctx = _ctx()
    pipeline.run_sync(ctx)
    assert ctx.extracted["order"] == ["a"]


def test_async_run_works_when_called_directly(executor: Any) -> None:
    a = RecordingEnricher("a")
    pipeline = EnricherPipeline(stages=[[a]], executor=executor)
    ctx = _ctx()
    asyncio.run(pipeline.run(ctx))
    assert ctx.extracted["order"] == ["a"]
