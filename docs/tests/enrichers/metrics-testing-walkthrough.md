# Enricher Observability Metrics Testing Walkthrough

This document covers how to test the Phase F observability surface:
the four metric events emitted by `CloudTrailRunbook` when the
`EnricherPipeline` runs.

Phase F is **side-effect plumbing** — the runner stamps per-enricher
durations into `ctx.timings`, the cache already exposes `stats()` from
Phase B, and the runbook emits all of it through the existing
`MetricsEmitter`. No new dependencies, no new types — just one new
method (`CloudTrailRunbook._emit_metrics`) and a small extension to
`EnricherContext`.

---

## Background: what gets emitted

When the pipeline is enabled and a `MetricsEmitter` is injected:

| Event | Cardinality | Payload | When |
|---|---|---|---|
| `enricher_duration_ms` | per enricher | `{enricher, duration_ms, runbook}` | After every enricher finishes (success or failure) |
| `enricher_error` | per failure | `{enricher, type, runbook}` | Once per entry in `ctx.errors` |
| `enricher_pipeline_duration_ms` | per alert | `{runbook, duration_ms, stages_completed}` | After the pipeline returns |
| `principal_cache_stats` | every N alerts | `{hits, misses, evictions, size, runbook}` | After every Nth alert (default N=50; tunable via `LOGPOSE_CACHE_STATS_INTERVAL`) |

Architecture:

```
EnricherPipeline.run_one          → ctx.timings.append({enricher, duration_ms})
EnricherPipeline.run_all_stages   → ctx.stages_completed += 1 per stage

CloudTrailRunbook.enrich
  ├─ pipeline.run_sync(ctx)
  ├─ pipeline_ms = (now - started) * 1000
  └─ self._emit_metrics(ctx, pipeline_ms)
        ├─ for t in ctx.timings:  emit("enricher_duration_ms", t + {runbook})
        ├─ for e in ctx.errors:   emit("enricher_error",       e + {runbook})
        ├─ emit("enricher_pipeline_duration_ms", {duration, stages_completed, runbook})
        └─ if alerts_processed % interval == 0:
              emit("principal_cache_stats", cache.stats() + {runbook})
```

---

## Part 1: Unit Testing

Tests live in `tests/unit/test_cloudtrail_runbook_metrics.py`. They use
a `RecordingEmitter` — a `MetricsEmitter` subclass that captures
`emit()` calls into a list instead of touching RabbitMQ:

```python
class RecordingEmitter(MetricsEmitter):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event, data or {}))

    def close(self) -> None:
        pass
```

Subclassing keeps `isinstance(emitter, MetricsEmitter)` true (so the
runbook accepts it without type complaints) and bypasses the parent
`__init__` (so no broker connection is attempted).

### Running

```sh
pytest tests/unit/test_cloudtrail_runbook_metrics.py -v
```

Expected:

```
test_emits_enricher_duration_ms_per_enricher PASSED
test_emits_enricher_error_per_failure PASSED
test_no_error_events_on_happy_path PASSED
test_emits_one_pipeline_duration_per_alert PASSED
test_emits_cache_stats_at_configured_interval PASSED
test_no_emitter_means_no_emit_calls PASSED
test_legacy_path_emits_nothing PASSED

7 passed
```

### What each test covers

| Test | What it verifies |
|---|---|
| `test_emits_enricher_duration_ms_per_enricher` | All four enrichers emit a duration event with the expected payload shape; `duration_ms` is a non-negative int |
| `test_emits_enricher_error_per_failure` | A failed `lookup_events` produces exactly one `enricher_error` for `principal_history`; the type matches the underlying exception |
| `test_no_error_events_on_happy_path` | Zero error events when nothing fails — error metrics aren't a permanent fixture |
| `test_emits_one_pipeline_duration_per_alert` | Two alerts → exactly two pipeline-duration events; `stages_completed=3` on the happy path |
| `test_emits_cache_stats_at_configured_interval` | With `LOGPOSE_CACHE_STATS_INTERVAL=2`, three alerts produce exactly one `principal_cache_stats` event (at the second alert); the payload includes `hits/misses/evictions/size` and `runbook` |
| `test_no_emitter_means_no_emit_calls` | A runbook with `emitter=None` does not raise when the pipeline runs — the metric path silently no-ops |
| `test_legacy_path_emits_nothing` | When the feature flag is off, the pipeline doesn't run, so none of the four new events are emitted — flag-off pods produce zero new metrics traffic |

---

## Part 2: Verification loop

```sh
pytest tests/unit/test_cloudtrail_runbook_metrics.py -v
python -m mypy logpose/enrichers/ logpose/runbooks/cloud/aws/cloudtrail.py
python -m black --check logpose/runbooks/cloud/aws/ tests/unit/test_cloudtrail_runbook_metrics.py
python -m flake8 logpose/runbooks/cloud/aws/ tests/unit/test_cloudtrail_runbook_metrics.py
```

All four must be clean.

---

## Why no integration test in Phase F?

The metric path is a side effect on top of the existing pipeline. The
moto-backed `tests/integration/test_cloudtrail_runbook_pipeline.py`
already exercises the runbook end-to-end; adding metric assertions
there would just duplicate what the unit tests cover. If we ever wire
the metrics into a Splunk dashboard or a Grafana scrape, that wiring
gets its own integration test then.

---

## Operator notes

When the runbook is deployed with the flag on:

- Each alert produces one `enricher_pipeline_duration_ms` plus one
  `enricher_duration_ms` per enricher (4) plus zero or more
  `enricher_error` events. So an idle pipeline is **5 events per alert**.
- `principal_cache_stats` fires every `LOGPOSE_CACHE_STATS_INTERVAL`
  alerts. The default of 50 is conservative — 1 stats event per ~250
  duration events.
- A fast-running pipeline can be a high-traffic stream (5+ events ×
  many alerts/second). If that becomes a problem, drop the per-enricher
  duration event and keep only the pipeline-level one — the change is
  one line in `_emit_metrics`.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/enrichers/context.py`](../../../logpose/enrichers/context.py) | `EnricherContext` — `timings` and `stages_completed` populated by the runner |
| [`logpose/enrichers/runner.py`](../../../logpose/enrichers/runner.py) | `EnricherPipeline._run_one` records per-enricher timings; `_run_all_stages` increments `stages_completed` |
| [`logpose/runbooks/cloud/aws/cloudtrail.py`](../../../logpose/runbooks/cloud/aws/cloudtrail.py) | `_emit_metrics` — the actual emission of all four event types |
| [`logpose/metrics/emitter.py`](../../../logpose/metrics/emitter.py) | Existing `MetricsEmitter` (unchanged in Phase F) |
| [`tests/unit/test_cloudtrail_runbook_metrics.py`](../../../tests/unit/test_cloudtrail_runbook_metrics.py) | 7 unit tests covering all four event types and the no-emitter / flag-off no-op paths |
