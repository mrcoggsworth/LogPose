# Metrics

The `logpose.metrics` package provides `MetricsEmitter` — a lightweight, fire-and-forget publisher that every LogPose pod uses to report pipeline activity to the dashboard. It is intentionally designed to be **invisible to the main pipeline**: it never raises, never blocks for more than a single non-blocking pika call, and degrades silently if RabbitMQ is unreachable.

---

## Table of Contents

1. [What MetricsEmitter Does](#what-metricsemitter-does)
2. [Event Schema](#event-schema)
3. [Recognized Event Names](#recognized-event-names)
4. [How It Works Internally](#how-it-works-internally)
5. [Using MetricsEmitter in Your Code](#using-metricsemitter-in-your-code)
6. [Configuration](#configuration)
7. [Fault Tolerance](#fault-tolerance)

---

## What MetricsEmitter Does

`MetricsEmitter` publishes small JSON events to the `logpose.metrics` RabbitMQ queue. The dashboard pod's `MetricsConsumer` drains that queue and increments in-memory counters that power the dashboard's stat cards.

Every significant action in the pipeline — an alert ingested, a route matched, an alert sent to the DLQ, a runbook succeeding or failing — results in a `MetricsEmitter.emit()` call. This gives the dashboard a real-time, source-of-truth view of what the pipeline is doing without any direct dependency between the pipeline components and the dashboard.

```
KafkaConsumer         SqsConsumer         Router
     │                    │                  │
     ▼                    ▼                  ▼
MetricsEmitter.emit("alert_ingested")    MetricsEmitter.emit("route_matched")
MetricsEmitter.emit("alert_ingested")    MetricsEmitter.emit("dlq_enqueued")
                                              │
                              CloudTrailRunbook
                                    │
                                    ▼
                        MetricsEmitter.emit("runbook_success")
                        MetricsEmitter.emit("enricher_duration_ms")
                        MetricsEmitter.emit("principal_cache_stats")
                                    │
                                    ▼ (all events → logpose.metrics queue)
                              Dashboard pod
                           MetricsConsumer
                           MetricsStore counters
```

---

## Event Schema

Every event published to `logpose.metrics` has this structure:

```json
{
  "event": "alert_ingested",
  "ts": "2026-04-26T14:32:05.123456+00:00",
  "data": {
    "source": "kafka"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | Event name (see recognized events below) |
| `ts` | ISO 8601 string | UTC timestamp of emission |
| `data` | object | Event-specific payload |

---

## Recognized Event Names

These are the event names the dashboard's `MetricsConsumer` knows how to count. Unknown events are ignored by the dashboard but still stored in the queue.

| Event Name | Emitted By | `data` Fields | Dashboard Counter |
|-----------|------------|--------------|-------------------|
| `alert_ingested` | All consumers | `{"source": "kafka"}` | Alerts ingested total |
| `route_matched` | Router | `{"route": "cloud.aws.cloudtrail"}` | Routes matched by name |
| `dlq_enqueued` | Router | `{"reason": "no_route_matched"}` | DLQ entries total |
| `runbook_success` | BaseRunbook | `{"runbook": "cloud.aws.cloudtrail"}` | Runbook successes by name |
| `runbook_error` | BaseRunbook | `{"runbook": "...", "error": "..."}` | Runbook errors by name |
| `enricher_duration_ms` | CloudTrailRunbook | `{"enricher": "...", "duration_ms": 42, "runbook": "..."}` | Per-enricher timing |
| `enricher_error` | CloudTrailRunbook | `{"enricher": "...", "type": "...", "runbook": "..."}` | Per-enricher error count |
| `enricher_pipeline_duration_ms` | CloudTrailRunbook | `{"runbook": "...", "duration_ms": 180, "stages_completed": 3}` | End-to-end pipeline timing |
| `principal_cache_stats` | CloudTrailRunbook | `{"hits": 5, "misses": 3, "size": 8, "runbook": "..."}` | Cache hit/miss ratio |

The `dlq_reason` values currently in use are:
- `"no_route_matched"` — router found no matching route for the alert's payload.
- `"publish_failed"` — router matched a route but the publish to the runbook queue raised.

---

## How It Works Internally

**File:** `logpose/metrics/emitter.py`

### Lazy connection

`MetricsEmitter` does not connect to RabbitMQ in `__init__`. The connection is established lazily on the first `emit()` call, via `_ensure_connected()`. This means creating a `MetricsEmitter` is always safe — even if RabbitMQ is not running at construction time.

`_ensure_connected()` returns `True` if the connection is healthy, `False` if it cannot connect. It reuses an existing open connection rather than reconnecting on every call.

### Non-persistent messages

Metric events are published with `delivery_mode=1` (non-persistent / transient). If the `logpose.metrics` queue fills up or RabbitMQ restarts, those metrics are lost. This is an intentional trade-off: metrics are observability data. Losing a few counters in a crash scenario is acceptable — blocking the main pipeline to guarantee metric delivery is not.

The queue itself is declared with `durable=True` so it survives RabbitMQ restarts, but individual messages are transient.

### Error swallowing

Every path inside `emit()` is wrapped in `try/except Exception`. On any failure:
1. The exception is logged at `DEBUG` level (not `WARNING` or `ERROR` — metrics failures are not pipeline alerts).
2. `_reset()` is called to clear the connection references, so the next `emit()` attempts a fresh reconnect.
3. The calling code continues normally — `emit()` always returns `None`.

This is the only place in the LogPose codebase where exceptions are intentionally swallowed. The justification: a metrics failure must never degrade the reliability of alert processing.

### Connection reset

`_reset()` sets both `_connection` and `_channel` to `None`. On the next `emit()`, `_ensure_connected()` detects the closed state and opens a new connection. There is no exponential backoff — if RabbitMQ is down, every `emit()` makes one quick connection attempt, fails silently, and returns.

---

## Using MetricsEmitter in Your Code

### Standard pattern

```python
from logpose.metrics.emitter import MetricsEmitter

emitter = MetricsEmitter()

# In a hot loop — always guard against None
if emitter is not None:
    emitter.emit("alert_ingested", {"source": "my_source"})

# At shutdown
emitter.close()
```

### Optional emitter pattern

Most classes in LogPose accept `emitter: MetricsEmitter | None = None` and guard every call:

```python
class MyConsumer(BaseConsumer):
    def __init__(self, emitter: MetricsEmitter | None = None) -> None:
        self._emitter = emitter

    def _handle_message(self, msg, callback):
        ...
        if self._emitter is not None:
            self._emitter.emit("alert_ingested", {"source": "my_source"})
        callback(alert)
```

This pattern makes the emitter optional for unit tests — tests that don't care about metrics don't need to mock `MetricsEmitter`.

### Closing cleanly

Always call `emitter.close()` at pod shutdown. This flushes the pika connection cleanly and prevents `ConnectionResetError` log noise from abrupt disconnects:

```python
emitter = MetricsEmitter()
try:
    main_loop()
finally:
    emitter.close()
```

---

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RABBITMQ_URL` | No | `amqp://guest:guest@localhost:5672/` | RabbitMQ connection URL |

Unlike other classes in the platform, `MetricsEmitter` has a default URL rather than raising `KeyError` when `RABBITMQ_URL` is unset. This makes it easy to instantiate in development without setting environment variables, at the cost of silently publishing nowhere if the default RabbitMQ is not running.

---

## Fault Tolerance

`MetricsEmitter` is designed for three failure modes:

**RabbitMQ not running:** `_ensure_connected()` returns `False`, `emit()` returns immediately, main pipeline unaffected.

**RabbitMQ drops the connection mid-operation:** The `basic_publish` call raises, `_reset()` clears the references, the next `emit()` reconnects.

**RabbitMQ running but overloaded:** Metric events are non-persistent and non-blocking. If the `logpose.metrics` queue fills up, RabbitMQ applies flow control. `MetricsEmitter` catches the resulting error, resets, and retries on the next call.

In all cases: the main pipeline continues, and metrics that could not be delivered are silently dropped.
