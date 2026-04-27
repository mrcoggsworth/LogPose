# Forwarder

The `logpose.forwarder` package is the **egress layer** of the LogPose SOAR platform. It consumes enriched alerts and dead-letter queue entries from RabbitMQ and delivers them to their final logging destination — by default Splunk, or optionally an arbitrary HTTP endpoint.

The forwarder is the Phase III implementation: every alert, whether successfully enriched or not, reaches Splunk for operator review.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [SplunkHECClient](#splunkhecclient)
3. [UniversalHTTPClient](#universalhttpclient)
4. [EnrichedAlertForwarder](#enrichedalertforwarder)
5. [DLQForwarder](#dlqforwarder)
6. [Forwarder Pod — forwarder_main.py](#forwarder-pod)
7. [Environment Variables Reference](#environment-variables-reference)
8. [Retry and Failure Behavior](#retry-and-failure-behavior)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        FORWARDER POD                                 │
│                                                                      │
│  Thread 1: EnrichedAlertForwarder                                    │
│    [enriched queue] ──► deserialize EnrichedAlert                    │
│                         │                                            │
│                         ├── destination="splunk"  ──► SplunkHECClient│
│                         └── destination="universal" ► UniversalHTTPClient
│                                                                      │
│  Thread 2: DLQForwarder                                              │
│    [alerts.dlq queue] ──► parse DLQ envelope ──► SplunkHECClient    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
                │                         │
                ▼                         ▼
       Splunk HEC endpoint        Universal HTTP endpoint
       (sourcetype=              (optional — only active when
        logpose:enriched_alert)   UNIVERSAL_FORWARDER_URL is set)
       (sourcetype=
        logpose:dlq_alert)
```

The forwarder pod runs two consumer threads. Each thread gets its own `SplunkHECClient` instance — `SplunkHECClient` maintains a mutable event buffer and is not thread-safe to share.

---

## SplunkHECClient

**File:** `logpose/forwarder/splunk_client.py`

The primary delivery client. Sends events to Splunk using the **HTTP Event Collector (HEC)** endpoint — the industry-standard approach for programmatic Splunk ingestion. HEC is a lightweight endpoint separate from Splunk's management REST API; it accepts newline-delimited JSON `POST` requests.

### Industry best practices implemented

- **Batch mode:** Events are buffered in memory and flushed when the buffer reaches `SPLUNK_BATCH_SIZE` (default 50) or when `flush()` is called explicitly.
- **Newline-delimited JSON format:** The HEC batch spec requires each event on a separate line. `flush()` joins buffered events with `"\n"` before posting.
- **Authorization header:** `Authorization: Splunk <token>`.
- **Retry with exponential backoff:** On `429 Too Many Requests` or any `5xx` error, retries up to 3 times with delays of 1s, 2s, 4s. Permanent `4xx` errors (misconfigured token, wrong index, etc.) are not retried.
- **Context manager:** `flush()` is called on `__exit__` to guarantee the final partial batch is delivered.

### Event structure

```python
client.build_event(
    event_data={"user": "deploy-bot", "event_name": "PutObject", ...},
    source="cloud.aws.cloudtrail",
    sourcetype="logpose:enriched_alert",
    timestamp=1745678925.123,  # Unix epoch float; defaults to now
    host="logpose",
)
```

This produces a well-formed HEC event dict:

```json
{
  "time": 1745678925.123,
  "host": "logpose",
  "source": "cloud.aws.cloudtrail",
  "sourcetype": "logpose:enriched_alert",
  "index": "main",
  "event": { "user": "deploy-bot", "event_name": "PutObject", ... }
}
```

### Usage pattern

Always use as a context manager so the final partial batch is not lost:

```python
with SplunkHECClient() as client:
    for enriched in alerts:
        event = client.build_event(
            event_data=json.loads(enriched.model_dump_json()),
            source=enriched.runbook,
            sourcetype="logpose:enriched_alert",
        )
        client.send(event)
# flush() called automatically on __exit__
```

Or call `flush()` manually after each event when low-latency delivery is more important than throughput:

```python
client.send(event)
client.flush()  # force immediate POST
```

### Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SPLUNK_HEC_URL` | Yes | — | HEC endpoint, e.g. `https://splunk.example.com:8088/services/collector` |
| `SPLUNK_HEC_TOKEN` | Yes | — | HEC token |
| `SPLUNK_INDEX` | No | `main` | Splunk index to write to |
| `SPLUNK_BATCH_SIZE` | No | `50` | Max events per POST |

---

## UniversalHTTPClient

**File:** `logpose/forwarder/universal_client.py`

A generic HTTP forwarder for when the destination is not Splunk. This is the client used when a runbook sets `destination = "universal"` on its class.

### Design

The public surface mirrors `SplunkHECClient` (`build_event`, `send`, `flush`, `close`, context manager) so `EnrichedAlertForwarder` can branch on `enriched.destination` without knowing anything about the client's shape.

**Key difference from `SplunkHECClient`:** No batching. Each `send()` call results in one immediate `POST`. Remote HTTP endpoints cannot be assumed to understand Splunk's newline-delimited batch format.

### Event envelope

```python
client.build_event(
    event_data={...},
    source="cloud.aws.cloudtrail",
    sourcetype="logpose:enriched_alert",
    timestamp=1745678925.123,
)
```

Produces:

```json
{
  "time": 1745678925.123,
  "host": "logpose",
  "source": "cloud.aws.cloudtrail",
  "sourcetype": "logpose:enriched_alert",
  "event": { ... }
}
```

The envelope intentionally mirrors the Splunk HEC format so both clients can be used interchangeably by the forwarder.

### `flush()` is a no-op

`UniversalHTTPClient.flush()` does nothing — events are sent immediately. It exists only so `EnrichedAlertForwarder` can call `client.send(event)` and `client.flush()` without branching on client type.

### Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `UNIVERSAL_FORWARDER_URL` | Yes | — | Destination URL |
| `UNIVERSAL_FORWARDER_AUTH_HEADER` | No | — | Full `Authorization` header value, e.g. `Bearer abc123` |
| `UNIVERSAL_FORWARDER_TIMEOUT_SECONDS` | No | `10` | Per-request timeout in seconds |

---

## EnrichedAlertForwarder

**File:** `logpose/forwarder/enriched_forwarder.py`

Consumes from `QUEUE_ENRICHED` and delivers each `EnrichedAlert` to the appropriate client based on `enriched.destination`.

### How it works

1. Connects to RabbitMQ with `prefetch_count=1` and declares `QUEUE_ENRICHED`.
2. Enters a blocking consume loop in `run()`.
3. For each message:
   - Deserializes the body as an `EnrichedAlert` via `EnrichedAlert.model_validate_json(body)`.
   - Calls `_forward(enriched)`.
   - Acks on success; nacks with `requeue=False` on failure.

### Destination routing in `_forward()`

```python
def _forward(self, enriched: EnrichedAlert) -> None:
    if enriched.destination == "universal":
        event = self._universal.build_event(...)
        self._universal.send(event)
        self._universal.flush()
    else:
        event = self._splunk.build_event(...)
        self._splunk.send(event)
        self._splunk.flush()
```

`flush()` is called after each `send()` — the forwarder does not hold events in the buffer waiting for a batch. This prioritizes delivery latency over throughput. For high-volume deployments, consider removing the per-event `flush()` and letting the batch size threshold trigger flushes instead.

### Failure behavior

If delivery to Splunk fails (including after retries), the `EnrichedAlert` is nacked with `requeue=False`. The alert is lost from the `enriched` queue. This is intentional: re-queuing a delivery failure causes a busy retry loop if Splunk is down. Operators should monitor the forwarder logs for delivery failures and replay from RabbitMQ management if needed.

---

## DLQForwarder

**File:** `logpose/forwarder/dlq_forwarder.py`

Consumes from `QUEUE_DLQ` (`alerts.dlq`) and forwards each entry to Splunk with `sourcetype=logpose:dlq_alert`. This ensures alerts that failed routing or enrichment still reach Splunk for operator review.

### DLQ message format

The router publishes DLQ messages as:

```json
{
  "alert": {
    "id": "3f2e...",
    "source": "kafka",
    "received_at": "2026-04-26T14:32:05+00:00",
    "raw_payload": { ... },
    "metadata": { ... }
  },
  "dlq_reason": "no_route_matched",
  "dlq_at": "2026-04-26T14:32:05+00:00",
  "original_queue": "alerts",
  "error_detail": "No matcher returned True for payload keys: [...]"
}
```

`DLQForwarder._forward()` wraps this entire envelope as the `event` field in a Splunk HEC event with:
- `source` = `alert.source` (the original ingest source)
- `sourcetype` = `"logpose:dlq_alert"`

### Searching DLQ entries in Splunk

```
sourcetype=logpose:dlq_alert
| table _time, dlq_reason, error_detail, alert.id, alert.source
```

The two `dlq_reason` values currently in use:
- `no_route_matched` — no route matcher returned `True` for the payload. Add or fix the route.
- `publish_failed` — router matched a route but could not publish to the runbook queue. Investigate RabbitMQ connectivity.

---

## Forwarder Pod

**File:** `logpose/forwarder_main.py`

The forwarder pod runs both `EnrichedAlertForwarder` and `DLQForwarder` in the same process on separate threads.

```
forwarder_main.py
  │
  ├── SplunkHECClient()  (for enriched alerts)
  ├── SplunkHECClient()  (for DLQ — separate instance, not thread-safe to share)
  ├── UniversalHTTPClient()  (only if UNIVERSAL_FORWARDER_URL is set)
  │
  ├── Thread 1: enriched-forwarder → EnrichedAlertForwarder.run()
  └── Thread 2: dlq-forwarder      → DLQForwarder.run()
```

**Why two SplunkHECClient instances?** `SplunkHECClient` maintains a mutable event buffer (`self._buffer`). Sharing one instance across threads would require locking and could cause one thread's flush to deliver the other thread's buffered events in an interleaved batch. Two separate instances avoid this entirely.

**UniversalHTTPClient is optional.** It is only instantiated when `UNIVERSAL_FORWARDER_URL` is set. Deployments that only forward to Splunk do not need to set any universal forwarder environment variables.

### Starting the pod

```bash
python -m logpose.forwarder_main
```

Or with Docker Compose:

```yaml
forwarder:
  command: ["python", "-m", "logpose.forwarder_main"]
  environment:
    RABBITMQ_URL: "amqp://guest:guest@rabbitmq:5672/"
    SPLUNK_HEC_URL: "https://splunk.example.com:8088/services/collector"
    SPLUNK_HEC_TOKEN: "abc123"
    SPLUNK_INDEX: "security"
```

### Graceful shutdown

On `SIGTERM`/`KeyboardInterrupt`:
1. `enriched_forwarder.stop()` and `dlq_forwarder.stop()` signal the consume loops to exit.
2. Both threads finish their current in-flight message.
3. `enriched_splunk.close()` and `dlq_splunk.close()` flush remaining buffered events to Splunk.
4. Both forwarders disconnect from RabbitMQ.

The `close()` calls in the `finally` block are critical — they ensure the final partial Splunk batch is delivered even if the pod receives a shutdown signal between `send()` and the next automatic `flush()`.

---

## Environment Variables Reference

| Variable | Component | Required | Default | Description |
|----------|-----------|----------|---------|-------------|
| `RABBITMQ_URL` | All | Yes | — | RabbitMQ connection URL |
| `SPLUNK_HEC_URL` | SplunkHECClient | Yes | — | Splunk HEC endpoint |
| `SPLUNK_HEC_TOKEN` | SplunkHECClient | Yes | — | Splunk HEC token |
| `SPLUNK_INDEX` | SplunkHECClient | No | `main` | Target Splunk index |
| `SPLUNK_BATCH_SIZE` | SplunkHECClient | No | `50` | Max events per HEC POST |
| `UNIVERSAL_FORWARDER_URL` | UniversalHTTPClient | No | — | Destination URL (enables universal forwarding) |
| `UNIVERSAL_FORWARDER_AUTH_HEADER` | UniversalHTTPClient | No | — | Full `Authorization` header value |
| `UNIVERSAL_FORWARDER_TIMEOUT_SECONDS` | UniversalHTTPClient | No | `10` | Per-request timeout |

---

## Retry and Failure Behavior

Both `SplunkHECClient` and `UniversalHTTPClient` use the same retry policy:

| Status | Behavior |
|--------|----------|
| `200` | Success — no retry |
| `429` | Retry with exponential backoff (1s → 2s → 4s, up to 3 attempts) |
| `5xx` | Retry with exponential backoff |
| `4xx` (except 429) | Permanent error — raise `RuntimeError` immediately, no retry |
| Network error | Retry with exponential backoff |

After 3 failed attempts, `RuntimeError` is raised. `EnrichedAlertForwarder._forward()` lets this propagate, causing the on-message handler to nack the delivery with `requeue=False`. The alert exits the `enriched` queue permanently — check forwarder logs for the failure, then replay from RabbitMQ management or re-inject from the source if needed.

A `4xx` response (e.g. `401 Unauthorized`, `400 Bad Request`) indicates a configuration problem: wrong token, wrong index name, or malformed event. These are not transient — retrying would produce the same result — so the error is surfaced immediately without burning retry budget.
