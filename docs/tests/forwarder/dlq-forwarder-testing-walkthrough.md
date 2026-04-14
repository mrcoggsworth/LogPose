# DLQForwarder Testing Walkthrough

`DLQForwarder` is the second half of the Phase III forwarding stage. It consumes the `alerts.dlq` RabbitMQ queue and ships each DLQ message to Splunk with a distinct sourcetype so analysts can identify and triage unrouted or failed alerts. The unit tests in `tests/unit/test_dlq_forwarder.py` verify the `_forward()` method in isolation.

---

## How DLQForwarder Works

```
alerts.dlq queue (RabbitMQ)
        │
        ▼
DLQForwarder.run()                 ← blocking consume loop
        │
        └─ for each message:
               deserialize JSON → dict  (not Pydantic — raw dict)
               call _forward(message)
                   │
                   ├─ build HEC event:
                   │     sourcetype: "logpose:dlq_alert"
                   │     source: message["alert"]["source"]  (or "unknown")
                   │     event: full message dict (alert + dlq fields preserved)
                   │
                   ├─ splunk.send(event)
                   └─ splunk.flush()
```

Unlike `EnrichedAlertForwarder`, which works with typed `EnrichedAlert` Pydantic models, `DLQForwarder` receives plain dicts. The DLQ message envelope always contains these fields:

| Field | Description |
|-------|-------------|
| `alert` | The original alert dict that failed routing or processing |
| `dlq_reason` | String code: `"no_route_matched"`, `"publish_failed"`, etc. |
| `dlq_at` | ISO-8601 UTC timestamp when the message entered the DLQ |
| `original_queue` | The queue the alert was consumed from before failing |
| `error_detail` | Human-readable description of why the alert was DLQ'd |

---

## Running the Tests

No external services are needed.

```sh
pytest tests/unit/test_dlq_forwarder.py -v
```

Expected output: 7 tests passing.

---

## Test Fixtures

### `_dlq_message` helper

```python
def _dlq_message(reason: str = "no_route_matched") -> dict:
    return {
        "alert": {
            "id": "test-id-123",
            "source": "kafka",
            "raw_payload": {"unknown": "data"},
            "received_at": datetime.now(tz=timezone.utc).isoformat(),
            "metadata": {},
        },
        "dlq_reason": reason,
        "dlq_at": datetime.now(tz=timezone.utc).isoformat(),
        "original_queue": "alerts",
        "error_detail": "No matcher returned True for payload keys: ['unknown']",
    }
```

Builds a realistic DLQ envelope. The `reason` parameter lets tests verify behavior for different DLQ reasons (e.g., `"publish_failed"`).

### `forwarder` fixture — bypassing `__init__`

```python
@pytest.fixture()
def forwarder(splunk: SplunkHECClient) -> DLQForwarder:
    fwd = DLQForwarder.__new__(DLQForwarder)
    fwd._splunk = splunk
    fwd._url = "amqp://localhost/"
    fwd._connection = None
    fwd._channel = None
    return fwd
```

Same `__new__` bypass pattern used in the enriched forwarder tests. Avoids the RabbitMQ connection in `__init__` while injecting a real `SplunkHECClient`.

---

## What Each Test Covers

### sourcetype and source

| Test | What it asserts |
|------|----------------|
| `test_dlq_forward_sets_dlq_sourcetype` | `event["sourcetype"] == "logpose:dlq_alert"` |
| `test_dlq_forward_uses_alert_source_as_splunk_source` | `event["source"] == "kafka"` (from `message["alert"]["source"]`) |
| `test_dlq_forward_falls_back_to_unknown_source_when_alert_missing` | When `alert` dict is empty, `event["source"] == "unknown"` |

`logpose:dlq_alert` is the sourcetype that distinguishes DLQ events from enriched events in Splunk. Analysts use this to create a separate dashboard view for unhandled alerts.

The fallback to `"unknown"` handles the edge case where a DLQ message was constructed without a valid alert — for example, if a consumer received a malformed JSON payload that could not be parsed into an `Alert` at all.

### Event payload

| Test | What it asserts |
|------|----------------|
| `test_dlq_forward_includes_full_message_as_event` | `event["event"]["dlq_reason"]` and `event["event"]["alert"]["id"]` are present and correct |
| `test_dlq_forward_preserves_all_dlq_fields` | All five DLQ fields (`alert`, `dlq_reason`, `dlq_at`, `original_queue`, `error_detail`) are in `event["event"]` |

The entire DLQ message dict is forwarded as the Splunk event payload — nothing is stripped. This gives analysts full context: what the original alert was, why it failed, when it failed, and which queue it came from.

### Flush call

| Test | What it asserts |
|------|----------------|
| `test_dlq_forward_calls_flush_after_send` | `splunk.flush` is called exactly once after `splunk.send` |

Same flush-per-message behavior as `EnrichedAlertForwarder`. Every DLQ alert is flushed to Splunk before the next message is acknowledged.

---

## Differences from EnrichedAlertForwarder Tests

| Aspect | EnrichedAlertForwarder | DLQForwarder |
|--------|----------------------|--------------|
| Input type | `EnrichedAlert` (Pydantic model) | `dict` (raw) |
| sourcetype | `logpose:enriched_alert` | `logpose:dlq_alert` |
| Source field | `enriched.runbook` | `message["alert"]["source"]` |
| Event timestamp | `enriched.enriched_at` | current time (not set explicitly) |
| Event payload | structured (`alert`, `runbook`, `extracted`) | full raw DLQ dict |

Both forwarders share the same `__new__`-bypass fixture pattern and the same `splunk.send` / `splunk.flush` patching approach. If you understand one, the other is immediately readable.
